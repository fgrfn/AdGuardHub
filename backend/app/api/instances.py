"""Dynamic instance management (spec §8) and initial import (spec §7)."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..adapters import ADAPTERS, AdapterError, available_adapters
from ..adapters import session as adapter_session
from ..adapters.sections import SECTION_NAMES
from ..config import get_settings
from ..deps import CurrentUser, SessionDep
from ..models import Instance, InstanceStatus, PushJob
from ..runtime import get_crypto
from ..schemas import (
    ConnectionResult,
    ConnectionTest,
    ImportRequest,
    InstanceCreate,
    InstanceOut,
    InstanceUpdate,
)
from ..semver import is_newer, parse_version
from ..services import filtersizes
from ..services.aggregate import invalidate_stats_cache
from ..services.events import bus
from ..services.importer import import_from_instance
from ..services.sync import (
    ALL_KINDS,
    check_instance,
    process_retry_queue,
    push_to_instance,
    schedule_sync,
)
from ..services.versions import record as _record

router = APIRouter(prefix="/api/instances", tags=["instances"])


def offered_update(instance: Instance) -> tuple[str, str]:
    """The update to show for a node, dropping one that is not actually newer.

    These fields are written by reconciliation and then read back for as long as
    it takes the next run to come round. A row written by an earlier build can
    therefore name the version the node is already running — which is what put
    "v0.107.79 — update to v0.107.79" on a card and left it there, because
    nothing between the two runs re-examined it.

    Judged again on the way out, such a row simply never reaches the interface,
    on every hub that already has one stored and without waiting for anything.
    A version the hub cannot parse is left alone: it is not evidence the stored
    update is wrong, and suppressing on it would hide a real one.
    """
    if not instance.update_version:
        return "", ""
    if parse_version(instance.version) and not is_newer(
        instance.update_version, instance.version
    ):
        return "", ""
    return instance.update_version, instance.update_url


def to_out(instance: Instance) -> InstanceOut:
    update_version, update_url = offered_update(instance)
    return InstanceOut(
        id=instance.id,
        name=instance.name,
        base_url=instance.base_url,
        adapter=instance.adapter,
        username=instance.username,
        has_password=bool(instance.password_encrypted),
        verify_tls=instance.verify_tls,
        enabled=instance.enabled,
        maintenance=instance.maintenance,
        status=instance.status,
        version=instance.version,
        update_version=update_version,
        update_url=update_url,
        update_error=instance.update_error,
        last_error=instance.last_error,
        last_seen_at=instance.last_seen_at,
        last_synced_at=instance.last_synced_at,
        out_of_sync_since=instance.out_of_sync_since,
        created_at=instance.created_at,
    )


async def _announce_list() -> None:
    """The set of instances changed — open browsers hold a copy of it.

    Without this the top bar's status element keeps whatever list it loaded on
    page load, so adding the first node leaves it reading "No nodes yet".
    """
    await bus.publish("instances", {})


async def _get(session: SessionDep, instance_id: int) -> Instance:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instance not found")
    return instance


@router.get("/adapters")
async def list_adapters(_: CurrentUser) -> list[str]:
    return available_adapters()


@router.post("/test-connection", response_model=ConnectionResult)
async def test_connection(
    payload: ConnectionTest, _: CurrentUser, session: SessionDep
) -> ConnectionResult:
    """Check a URL and credentials without saving anything.

    Returns the failure in the body rather than as an error status: the caller is a
    form that wants to show the message inline, not handle an exception.
    """
    adapter_cls = ADAPTERS.get(payload.adapter)
    if adapter_cls is None:
        return ConnectionResult(ok=False, error=f"Unknown adapter {payload.adapter!r}")

    password = payload.password
    if not password and payload.instance_id is not None:
        # Re-testing a saved instance whose password field was left blank.
        existing = await session.get(Instance, payload.instance_id)
        if existing is not None and existing.password_encrypted:
            if payload.base_url != existing.base_url:
                # The stored password belongs to the node at the stored address
                # and goes nowhere else. Combining "the password of instance 3"
                # with "whatever address is in the form" was a way to make the
                # hub deliver a node's password, in a login attempt, to any
                # host somebody typed — the one thing the API otherwise never
                # does with a password. A node that moved gets its password
                # typed once more.
                return ConnectionResult(
                    ok=False,
                    error=(
                        "The address differs from the saved one, so the saved password "
                        "is not used. Enter the password to test the new address."
                    ),
                )
            password = get_crypto().decrypt(existing.password_encrypted)

    adapter = adapter_cls(
        payload.base_url,
        payload.username,
        password,
        verify_tls=payload.verify_tls,
        timeout=get_settings().http_timeout,
    )
    try:
        version = await adapter.check()
    except (AdapterError, ValueError) as exc:
        return ConnectionResult(ok=False, error=str(exc))
    finally:
        await adapter.aclose()
    return ConnectionResult(ok=True, version=version)


@router.get("", response_model=list[InstanceOut])
async def list_instances(_: CurrentUser, session: SessionDep) -> list[InstanceOut]:
    result = await session.execute(select(Instance).order_by(Instance.id.asc()))
    return [to_out(instance) for instance in result.scalars().all()]


@router.post("", response_model=InstanceOut, status_code=status.HTTP_201_CREATED)
async def create_instance(
    payload: InstanceCreate, _: CurrentUser, session: SessionDep
) -> InstanceOut:
    if payload.adapter not in available_adapters():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown adapter")
    instance = Instance(
        name=payload.name,
        base_url=payload.base_url,
        adapter=payload.adapter,
        username=payload.username,
        password_encrypted=get_crypto().encrypt(payload.password) if payload.password else "",
        verify_tls=payload.verify_tls,
        enabled=payload.enabled,
        status=InstanceStatus.unknown.value
        if payload.enabled
        else InstanceStatus.disabled.value,
    )
    session.add(instance)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An instance with that name exists") from exc
    # The dashboard's "n of m nodes reporting" is now stale by one node.
    invalidate_stats_cache()
    filtersizes.invalidate()
    await _announce_list()
    await check_instance(session, instance)
    return to_out(instance)


@router.patch("/{instance_id}", response_model=InstanceOut)
async def update_instance(
    instance_id: int, payload: InstanceUpdate, _: CurrentUser, session: SessionDep
) -> InstanceOut:
    instance = await _get(session, instance_id)
    # The cached session belongs to the old URL/user pair; drop it before either moves.
    previous_key = (instance.base_url, instance.username)

    data = payload.model_dump(exclude_unset=True)
    password = data.pop("password", None)
    for field, value in data.items():
        setattr(instance, field, value)
    if password is not None:
        instance.password_encrypted = get_crypto().encrypt(password) if password else ""
    adapter_session.store.forget(previous_key)
    adapter_session.store.forget((instance.base_url, instance.username))
    if "enabled" in data or "maintenance" in data:
        # Disabled wins over maintenance: an instance nobody syncs at all is not
        # "being worked on". Otherwise the status is cleared rather than guessed —
        # the probe below, or the next push, writes what is actually true.
        if not instance.enabled:
            instance.status = InstanceStatus.disabled.value
        elif instance.maintenance:
            instance.status = InstanceStatus.maintenance.value
        else:
            instance.status = InstanceStatus.unknown.value
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An instance with that name exists") from exc
    invalidate_stats_cache()
    filtersizes.invalidate()
    await _announce_list()
    if instance.enabled and not instance.maintenance:
        await check_instance(session, instance)
        # Everything the hub could not send while the node was held back is
        # waiting in the retry queue. Replaying it here is what makes maintenance
        # a pause rather than a gap: the node catches up the moment it is
        # released, instead of waiting for the retry timer to come round.
        if data.get("maintenance") is False:
            await process_retry_queue(session)
    return to_out(instance)


@router.delete("/{instance_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_instance(instance_id: int, _: CurrentUser, session: SessionDep) -> None:
    instance = await _get(session, instance_id)
    jobs = await session.execute(select(PushJob).where(PushJob.instance_id == instance_id))
    for job in jobs.scalars().all():
        await session.delete(job)
    adapter_session.store.forget((instance.base_url, instance.username))
    await session.delete(instance)
    await session.commit()
    invalidate_stats_cache()
    filtersizes.invalidate()
    await _announce_list()


@router.post("/{instance_id}/test")
async def test_instance(instance_id: int, _: CurrentUser, session: SessionDep) -> dict[str, str]:
    """Probe connectivity and credentials without changing anything on the instance.

    Goes through check_instance rather than probing separately: a failed test used
    to leave the instance marked online, so the operator watched the test fail and
    the card still claimed the node was fine.
    """
    instance = await _get(session, instance_id)
    error = await check_instance(session, instance)
    if error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, error)
    return {"ok": "true", "version": instance.version}


@router.post("/{instance_id}/push")
async def push_instance(instance_id: int, _: CurrentUser, session: SessionDep) -> dict[str, str]:
    """Force a full-state push to a single instance."""
    instance = await _get(session, instance_id)
    error = await push_to_instance(session, instance, ALL_KINDS, "manual push")
    return {"ok": "false" if error else "true", "error": error}


@router.post("/{instance_id}/import")
async def import_instance(
    instance_id: int, payload: ImportRequest, user: CurrentUser, session: SessionDep
) -> dict[str, object]:
    """Adopt this instance's configuration as the hub's state (spec §7)."""
    instance = await _get(session, instance_id)
    try:
        result = await import_from_instance(
            session,
            instance,
            replace=payload.replace,
            sections=tuple(payload.sections) if payload.sections else SECTION_NAMES,
        )
    except (AdapterError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await record_version(
        session, f"imported configuration from {instance.name}", user, kind="import"
    )
    if payload.push_after_import:
        schedule_sync(ALL_KINDS, f"initial import from {instance.name}")
    return asdict(result)


async def record_version(
    session: SessionDep, label: str, user: CurrentUser, kind: str = "change"
) -> None:
    """Snapshot the central state so the change can be diffed and rolled back."""
    await _record(session, label, author=user.username, kind=kind)
