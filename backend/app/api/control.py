"""AdGuard Home compatible API, so existing clients can talk to the hub.

Apps built for AdGuard Home — the iOS and Android remotes, scripts, Home Assistant
integrations — speak ``/control/*``. Pointing one at AdGuardHub instead of at a
single node makes it manage all of them at once, which is exactly the promise of
the hub.

The mapping is deliberate:

* configuration is answered from the hub's own state, not from any one node, so
  what a client reads is what the hub will enforce;
* writes go into the central model and are pushed like any other change, so an
  edit from a phone reaches every instance;
* live data that only a running resolver has — counters, top lists — is aggregated
  across the instances.

Authentication is the hub's own admin account and session cookie: this surface is
no more exposed than the web UI it sits beside.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from ..adapters.sections import SECTION_NAMES
from ..deps import (
    ControlUser,
    SessionDep,
    enforce_login_throttle,
    note_signin_failure,
    note_signin_success,
)
from ..models import FilterList, Instance, ListKind, PayloadKind, Rule
from ..schemas import ControlLogin
from ..security import check_password
from ..services import filtersizes
from ..services import versions as version_service
from ..services.aggregate import cached_stats
from ..services.config import get_section, loads, set_section
from ..services.hubsettings import current as current_settings
from ..services.querylog import buffer
from ..services.rules import classify
from ..services.sync import schedule_sync
from ..version import VERSION

logger = logging.getLogger(__name__)

def _guard() -> None:
    """Refuse the whole surface while the switch is off.

    A router dependency, so it runs before anything else on every route here —
    before the credentials are looked at. It used to be called from inside each
    handler, which is after ``ControlUser`` has run: a switched-off API still
    took a Basic Auth header, spent ~300 ms of bcrypt on it, counted a wrong
    password toward the lockout and logged it as a failed sign-in, and only
    then answered 404. "Off" meant the responses were off; the password door
    stayed open and lit.
    """
    if not current_settings().external_api_enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "The AdGuard-compatible API is switched off in AdGuardHub's settings.",
        )


router = APIRouter(
    prefix="/control",
    tags=["adguard-compat"],
    include_in_schema=False,
    dependencies=[Depends(_guard)],
)

RULE_KINDS = (PayloadKind.rules,)
LIST_KINDS = (PayloadKind.filters,)
SETTING_KINDS = (PayloadKind.settings,)


async def _section(session: SessionDep, name: str) -> dict[str, Any]:
    return loads((await get_section(session, name)).data)


async def _write_section(
    session: SessionDep, name: str, changes: dict[str, Any], user: ControlUser, label: str
) -> None:
    """Merge a partial change into a section and push it, as a UI edit would."""
    data = {**(await _section(session, name)), **changes}
    await set_section(session, name, data=data)
    await version_service.record(session, label, author=user.username)
    schedule_sync(SETTING_KINDS, label)


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@router.post("/login")
async def login(
    payload: ControlLogin, request: Request, response: Response, session: SessionDep
) -> dict[str, str]:
    """AdGuard's login, answered with the hub's own admin account."""
    from ..models import User
    from .auth import _set_cookie

    source = enforce_login_throttle(request)
    result = await session.execute(select(User).where(User.username == payload.name))
    user = result.scalars().first()
    if not await check_password(payload.password, user.password_hash if user else None):
        note_signin_failure(source, "AdGuard-compatible login")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid username or password")
    note_signin_success(source, "AdGuard-compatible login")
    _set_cookie(request, response, user)
    return {}


@router.get("/logout")
@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    from .auth import _clear_cookie

    _clear_cookie(request, response)
    return {}


# --------------------------------------------------------------------------
# Status and live data
# --------------------------------------------------------------------------


@router.get("/status")
async def status_endpoint(user: ControlUser, session: SessionDep) -> dict[str, Any]:
    dns = await _section(session, "dns")
    instances = (
        (await session.execute(select(Instance).where(Instance.enabled.is_(True))))
        .scalars()
        .all()
    )
    return {
        "version": f"AdGuardHub {VERSION}",
        "language": "en",
        "dns_addresses": [instance.base_url for instance in instances],
        "dns_port": 53,
        "protection_enabled": bool(dns.get("protection_enabled", True)),
        "protection_disabled_duration": 0,
        "running": True,
        # The hub does not serve DHCP and never syncs it.
        "dhcp_available": False,
    }


@router.get("/profile")
async def profile(user: ControlUser) -> dict[str, Any]:
    return {"name": user.username, "language": "en", "theme": "auto"}


@router.get("/stats")
async def stats(_: ControlUser) -> dict[str, Any]:
    # Same held result the hub's own dashboard reads, so a phone app polling
    # this endpoint does not fan out to every resolver on each request.
    return await cached_stats()


@router.get("/querylog")
async def querylog(
    _: ControlUser,
    limit: int = Query(100, ge=1, le=2000),
    search: str = "",
    response_status: str = Query("", alias="response_status"),
) -> dict[str, Any]:
    """The aggregated log, in AdGuard's own shape.

    Each entry keeps the instance name in ``client_info``, so a client that shows it
    can tell which node answered.
    """
    entries = await buffer.snapshot(
        limit, search=search, blocked_only=response_status == "blocked"
    )
    return {
        "data": [
            {
                "time": entry["time"],
                "question": {
                    "name": entry["question"],
                    "type": entry["question_type"] or "A",
                    "class": "IN",
                },
                "client": entry["client"],
                "client_info": {"name": entry["instance"]},
                "reason": entry["answer_status"] or "NotFilteredNotFound",
                "rules": [{"text": entry["rule"]}] if entry["rule"] else [],
                "elapsed_ms": str(entry["elapsed_ms"]),
                "upstream": entry["upstream"],
                "answer": [],
            }
            for entry in entries
        ],
        "oldest": entries[-1]["time"] if entries else "",
    }


# --------------------------------------------------------------------------
# Filtering: rules and subscriptions
# --------------------------------------------------------------------------


@router.get("/filtering/status")
async def filtering_status(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    config = await _section(session, "filtering_config")
    rules = (
        (await session.execute(select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.id)))
        .scalars()
        .all()
    )
    lists = (
        (await session.execute(select(FilterList).order_by(FilterList.id))).scalars().all()
    )

    def serialise(item: FilterList) -> dict[str, Any]:
        return {
            "id": item.id,
            "enabled": item.enabled,
            "url": item.url,
            "name": item.name,
            "rules_count": 0,
            "last_updated": "",
        }

    return {
        "enabled": bool(config.get("enabled", True)),
        "interval": config.get("interval", 24),
        "user_rules": [rule.text for rule in rules],
        "filters": [serialise(item) for item in lists if item.kind == ListKind.blocklist.value],
        "whitelist_filters": [
            serialise(item) for item in lists if item.kind == ListKind.allowlist.value
        ],
    }


@router.post("/filtering/set_rules")
async def set_rules(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    """Replace the hub's rule set, exactly as the native UI's editor would."""
    from sqlalchemy import delete

    from ..models import RuleOrigin

    incoming = [str(line).strip() for line in payload.get("rules") or []]
    await session.execute(delete(Rule))
    await session.flush()
    seen: set[str] = set()
    for text in incoming:
        if not text or text in seen:
            continue
        seen.add(text)
        session.add(
            Rule(text=text, kind=classify(text).value, origin=RuleOrigin.custom.value)
        )
    await session.commit()

    label = f"rules replaced via the AdGuard API ({len(seen)} rule(s))"
    await version_service.record(session, label, author=user.username)
    schedule_sync(RULE_KINDS, label)
    return {}


@router.post("/filtering/config")
async def filtering_config(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    changes = {key: payload[key] for key in ("enabled", "interval") if key in payload}
    await _write_section(
        session, "filtering_config", changes, user, "filtering config changed via the AdGuard API"
    )
    return {}


@router.post("/filtering/add_url")
async def add_url(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "url is required")
    kind = ListKind.allowlist.value if payload.get("whitelist") else ListKind.blocklist.value

    existing = (
        await session.execute(
            select(FilterList).where(FilterList.url == url, FilterList.kind == kind)
        )
    ).scalars().first()
    if existing is None:
        session.add(
            FilterList(name=str(payload.get("name") or url), url=url, kind=kind, enabled=True)
        )
        await session.commit()

    label = f"subscription added via the AdGuard API: {url}"
    await version_service.record(session, label, author=user.username)
    schedule_sync(LIST_KINDS, label)
    return {}


@router.post("/filtering/remove_url")
async def remove_url(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    url = str(payload.get("url") or "")
    kind = ListKind.allowlist.value if payload.get("whitelist") else ListKind.blocklist.value
    item = (
        await session.execute(
            select(FilterList).where(FilterList.url == url, FilterList.kind == kind)
        )
    ).scalars().first()
    if item is not None:
        await session.delete(item)
        await session.commit()
        label = f"subscription removed via the AdGuard API: {url}"
        await version_service.record(session, label, author=user.username)
        schedule_sync(LIST_KINDS, label)
    return {}


@router.post("/filtering/set_url")
async def set_url(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    url = str(payload.get("url") or "")
    kind = ListKind.allowlist.value if payload.get("whitelist") else ListKind.blocklist.value
    data = payload.get("data") or {}
    item = (
        await session.execute(
            select(FilterList).where(FilterList.url == url, FilterList.kind == kind)
        )
    ).scalars().first()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")
    if "enabled" in data:
        item.enabled = bool(data["enabled"])
    if data.get("name"):
        item.name = str(data["name"])
    await session.commit()

    label = f"subscription updated via the AdGuard API: {url}"
    await version_service.record(session, label, author=user.username)
    schedule_sync(LIST_KINDS, label)
    return {}


@router.post("/filtering/refresh")
async def refresh_filters(_: ControlUser) -> dict[str, int]:
    """Pass the request on to every node, which is where the lists actually live.

    This used to answer ``{"updated": 0}`` without doing anything, on the grounds
    that the hub holds URLs rather than contents. True, and the wrong conclusion:
    a phone remote or a Home Assistant automation pressing *check for updates*
    against the hub got a success and a zero, and not one node went and looked.

    The number reported is the largest any single node updated, not the sum. A
    client asking this question means "how many of my lists changed", and the
    hub's subscriptions are one set replicated everywhere — adding the nodes
    together would count the same list twice. Same reasoning as the rule counts
    in ``services.filtersizes``.
    """
    results = await filtersizes.refresh_all()
    return {"updated": max((item.updated for item in results), default=0)}


# --------------------------------------------------------------------------
# Protection and the toggle modules
# --------------------------------------------------------------------------


@router.post("/protection")
async def protection(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    """Pause or resume filtering everywhere at once."""
    await _write_section(
        session,
        "dns",
        {"protection_enabled": bool(payload.get("enabled"))},
        user,
        "protection {} via the AdGuard API".format(
            "enabled" if payload.get("enabled") else "disabled"
        ),
    )
    return {}


def _toggle_routes(name: str, path: str) -> None:
    @router.get(f"/{path}/status", name=f"{name}_status")
    async def read(_: ControlUser, session: SessionDep) -> dict[str, Any]:
        return {"enabled": bool((await _section(session, name)).get("enabled"))}

    @router.post(f"/{path}/enable", name=f"{name}_enable")
    async def enable(user: ControlUser, session: SessionDep) -> dict[str, str]:
        await _write_section(
            session, name, {"enabled": True}, user, f"{path} enabled via the AdGuard API"
        )
        return {}

    @router.post(f"/{path}/disable", name=f"{name}_disable")
    async def disable(user: ControlUser, session: SessionDep) -> dict[str, str]:
        await _write_section(
            session, name, {"enabled": False}, user, f"{path} disabled via the AdGuard API"
        )
        return {}


_toggle_routes("safebrowsing", "safebrowsing")
_toggle_routes("parental", "parental")


@router.get("/safesearch/status")
async def safesearch_status(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    return await _section(session, "safesearch")


@router.put("/safesearch/settings")
async def safesearch_settings(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    await _write_section(
        session, "safesearch", payload, user, "safe search changed via the AdGuard API"
    )
    return {}


# --------------------------------------------------------------------------
# The remaining configuration sections
# --------------------------------------------------------------------------


@router.get("/dns_info")
async def dns_info(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    return await _section(session, "dns")


@router.post("/dns_config")
async def dns_config(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    await _write_section(
        session, "dns", payload, user, "DNS settings changed via the AdGuard API"
    )
    return {}


@router.get("/clients")
async def clients(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    data = await _section(session, "clients")
    return {"clients": data.get("clients") or [], "auto_clients": [], "supported_tags": []}


@router.get("/access/list")
async def access_list(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    return await _section(session, "access")


@router.post("/access/set")
async def access_set(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    await _write_section(
        session, "access", payload, user, "access list changed via the AdGuard API"
    )
    return {}


@router.get("/rewrite/list")
async def rewrite_list(_: ControlUser, session: SessionDep) -> list[dict[str, Any]]:
    return list((await _section(session, "rewrites")).get("items") or [])


@router.post("/rewrite/add")
async def rewrite_add(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    items = list((await _section(session, "rewrites")).get("items") or [])
    entry = {"domain": payload.get("domain"), "answer": payload.get("answer")}
    if entry not in items:
        items.append(entry)
    await _write_section(
        session, "rewrites", {"items": items}, user, "rewrite added via the AdGuard API"
    )
    return {}


@router.post("/rewrite/delete")
async def rewrite_delete(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    entry = {"domain": payload.get("domain"), "answer": payload.get("answer")}
    items = [
        item for item in (await _section(session, "rewrites")).get("items") or [] if item != entry
    ]
    await _write_section(
        session, "rewrites", {"items": items}, user, "rewrite removed via the AdGuard API"
    )
    return {}


@router.get("/querylog/config")
async def querylog_config(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    return await _section(session, "querylog_config")


@router.get("/stats/config")
async def stats_config(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    return await _section(session, "stats_config")


@router.get("/blocked_services/get")
async def blocked_services(_: ControlUser, session: SessionDep) -> dict[str, Any]:
    data = await _section(session, "blocked_services")
    return {"ids": data.get("ids") or [], "schedule": data.get("schedule") or {}}


@router.put("/blocked_services/update")
async def blocked_services_update(
    payload: dict[str, Any], user: ControlUser, session: SessionDep
) -> dict[str, str]:
    await _write_section(
        session, "blocked_services", payload, user, "blocked services changed via the AdGuard API"
    )
    return {}


@router.get("/adguardhub/sections")
async def sections_index(_: ControlUser) -> dict[str, Any]:
    """Not part of AdGuard's API: says which areas this compatibility layer serves."""
    return {"sections": list(SECTION_NAMES)}


__all__ = ["router"]
