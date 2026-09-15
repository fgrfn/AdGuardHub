"""Central rule model — all three AdGuard entry points write here (spec §5)."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..deps import CurrentUser, SessionDep
from ..models import PayloadKind, Rule, RuleKind, RuleOrigin, utcnow
from ..schemas import BulkRulesRequest, DomainRuleRequest, RuleCreate, RuleOut, RuleUpdate
from ..services.rules import allow_rule_for_domain, block_rule_for_domain, classify
from ..services.sync import schedule_sync
from ..services.versions import record as _record

router = APIRouter(prefix="/api/rules", tags=["rules"])

RULE_KINDS = (PayloadKind.rules,)


async def _find_by_text(session: SessionDep, text: str) -> Rule | None:
    result = await session.execute(select(Rule).where(Rule.text == text))
    return result.scalars().first()


def _expiry(minutes: int | None) -> datetime | None:
    """A deadline from a duration, stored the way SQLite hands it back.

    Naive, because SQLite drops the offset on write: storing an aware value here
    and comparing it to a naive one read back is the kind of mismatch that
    reads correct and is wrong by whatever the offset happens to be.
    """
    if not minutes:
        return None
    return utcnow().replace(tzinfo=None) + timedelta(minutes=minutes)


async def _add_rule(
    session: SessionDep,
    text: str,
    origin: RuleOrigin,
    comment: str,
    enabled: bool = True,
    expires_in_minutes: int | None = None,
) -> tuple[Rule, bool]:
    """Insert a rule, or return the existing one. Second element is True when created."""
    existing = await _find_by_text(session, text)
    if existing is not None:
        changed = False
        if not existing.enabled and enabled:
            existing.enabled = True
            changed = True
        # Re-allowing a domain that is already allowed is how somebody extends a
        # countdown, and the natural reading of "allow this for 30 minutes" is
        # thirty minutes from now — not "you already did that". The same press
        # also un-expires a rule: asking for a permanent one when a temporary one
        # exists means the temporary one was not enough.
        if expires_in_minutes is not None:
            existing.expires_at = _expiry(expires_in_minutes)
            changed = True
        if changed:
            await session.commit()
            return existing, True
        return existing, False
    rule = Rule(
        text=text,
        kind=classify(text).value,
        origin=origin.value,
        comment=comment,
        enabled=enabled,
        expires_at=_expiry(expires_in_minutes),
    )
    session.add(rule)
    try:
        await session.commit()
    except IntegrityError:  # pragma: no cover - concurrent insert
        await session.rollback()
        existing = await _find_by_text(session, text)
        if existing is None:
            raise
        return existing, False
    return rule, True


@router.get("", response_model=list[RuleOut])
async def list_rules(
    user: CurrentUser,
    session: SessionDep,
    kind: RuleKind | None = None,
    origin: RuleOrigin | None = None,
    search: str = "",
) -> list[Rule]:
    statement = select(Rule).order_by(Rule.id.desc())
    if kind is not None:
        statement = statement.where(Rule.kind == kind.value)
    if origin is not None:
        statement = statement.where(Rule.origin == origin.value)
    if search.strip():
        statement = statement.where(Rule.text.contains(search.strip()))
    result = await session.execute(statement)
    return list(result.scalars().all())


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(payload: RuleCreate, user: CurrentUser, session: SessionDep) -> Rule:
    rule, created = await _add_rule(
        session,
        payload.text,
        payload.origin,
        payload.comment,
        payload.enabled,
        payload.expires_in_minutes,
    )
    if not created and rule.text == payload.text:
        raise HTTPException(status.HTTP_409_CONFLICT, "That rule already exists")
    await record_version(session, f"rule added: {rule.text}", user)
    schedule_sync(RULE_KINDS, f"rule added: {rule.text}")
    return rule


@router.patch("/{rule_id}", response_model=RuleOut)
async def update_rule(
    rule_id: int, payload: RuleUpdate, user: CurrentUser, session: SessionDep
) -> Rule:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    data = payload.model_dump(exclude_unset=True)
    if "text" in data and data["text"]:
        rule.text = data["text"].strip()
        rule.kind = classify(rule.text).value
    if "enabled" in data:
        rule.enabled = bool(data["enabled"])
    if "comment" in data:
        rule.comment = data["comment"] or ""
    if "expires_in_minutes" in data:
        # 0 means "make this permanent", which is the one thing a duration cannot
        # express and the most likely thing to want from this field.
        rule.expires_at = _expiry(data["expires_in_minutes"])
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That rule already exists") from exc
    await record_version(session, f"rule updated: {rule.text}", user)
    schedule_sync(RULE_KINDS, f"rule updated: {rule.text}")
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_rule(rule_id: int, user: CurrentUser, session: SessionDep) -> None:
    rule = await session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    text = rule.text
    await session.delete(rule)
    await session.commit()
    await record_version(session, f"rule removed: {text}", user)
    schedule_sync(RULE_KINDS, f"rule removed: {text}")


@router.post("/allow", response_model=RuleOut)
async def allow_domain(
    payload: DomainRuleRequest,
    user: CurrentUser,
    session: SessionDep,
    origin: RuleOrigin = Query(RuleOrigin.allowlist),
) -> Rule:
    """Allowlist a domain. Used by the Allowlist tab and the query log's whitelist action."""
    rule, created = await _add_rule(
        session,
        allow_rule_for_domain(payload.domain),
        origin,
        payload.comment,
        expires_in_minutes=payload.expires_in_minutes,
    )
    if created:
        await record_version(session, f"allowlisted {payload.domain}", user)
    schedule_sync(RULE_KINDS, f"allowlisted {payload.domain}")
    return rule


@router.post("/block", response_model=RuleOut)
async def block_domain(
    payload: DomainRuleRequest,
    user: CurrentUser,
    session: SessionDep,
    origin: RuleOrigin = Query(RuleOrigin.custom),
) -> Rule:
    rule, created = await _add_rule(
        session,
        block_rule_for_domain(payload.domain),
        origin,
        payload.comment,
        expires_in_minutes=payload.expires_in_minutes,
    )
    if created:
        await record_version(session, f"blocked {payload.domain}", user)
    schedule_sync(RULE_KINDS, f"blocked {payload.domain}")
    return rule


@router.post("/bulk", response_model=list[RuleOut])
async def bulk_import(
    payload: BulkRulesRequest, user: CurrentUser, session: SessionDep
) -> list[Rule]:
    """Paste a block of AdGuard syntax. Blank lines are skipped; comments are kept."""
    created: list[Rule] = []
    for line in payload.text.splitlines():
        text = line.strip()
        if not text:
            continue
        rule, was_created = await _add_rule(session, text, payload.origin, "")
        if was_created:
            created.append(rule)
    if created:
        await record_version(session, f"{len(created)} rule(s) imported", user)
    schedule_sync(RULE_KINDS, f"{len(created)} rule(s) imported")
    return created


async def record_version(session: SessionDep, label: str, user: CurrentUser) -> None:
    """Snapshot the central state so the change can be diffed and rolled back."""
    await _record(session, label, author=user.username)
