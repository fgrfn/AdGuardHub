"""Operational settings and notification targets (spec §10)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ..config import get_settings
from ..deps import CurrentUser, SessionDep
from ..models import NotifierTarget
from ..runtime import get_login_throttle, get_update_checker
from ..schemas import (
    HubSettingsOut,
    HubSettingsUpdate,
    NotifierCreate,
    NotifierOut,
    NotifierUpdate,
)
from ..services import driftarchive, hubsettings, logbuffer, selfupdate, updates
from ..services.logbuffer import get_buffer
from ..services.notify import KNOWN_EVENTS, NOTIFIER_TYPES, test_target
from ..services.updates import install_method

router = APIRouter(prefix="/api/settings", tags=["settings"])


def to_out(target: NotifierTarget) -> NotifierOut:
    return NotifierOut(
        id=target.id,
        name=target.name,
        type=target.type,
        url=target.url,
        has_token=bool(target.token),
        enabled=target.enabled,
        events=[item for item in (target.events or "").split(",") if item],
        last_error=target.last_error,
    )


def _validate_events(events: list[str]) -> str:
    unknown = [event for event in events if event not in KNOWN_EVENTS]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown event(s): {', '.join(unknown)}"
        )
    return ",".join(events)


@router.post("/onboarding-complete", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def onboarding_complete(_: CurrentUser, session: SessionDep) -> None:
    """The first-run walkthrough is done, or the operator chose to skip it."""
    await hubsettings.finish_onboarding(session)


@router.get("/sign-ins")
async def failed_sign_ins(_: CurrentUser) -> dict[str, Any]:
    """Failed sign-ins and any lockout in force.

    The throttle refuses an address for five minutes after enough failures, and
    until now that was invisible from inside the hub: the operator saw the same
    rejection as an attacker would, with nothing to say the password was fine and
    the address was simply not being listened to. Reading the log meant leaving
    the interface for a shell.
    """
    throttle = get_login_throttle()
    return {
        "failures": [
            {
                "source": item.source,
                "door": item.door,
                "reason": item.reason,
                "at": item.at.isoformat().replace("+00:00", "Z"),
            }
            for item in throttle.recent_failures()
        ],
        "lockouts": [
            {"source": source, "seconds_left": int(seconds) + 1}
            for source, seconds in throttle.lockouts()
        ],
        "max_failures": throttle.max_failures,
        "window_seconds": int(throttle.window),
    }


@router.get("/update")
async def update_status(_: CurrentUser, session: SessionDep, force: bool = False) -> dict[str, Any]:
    """Whether a newer release exists, and whether this install could take it.

    The answer is cached for hours, so the interface can ask on every page load
    without that meaning a request to GitHub on every page load. ``force`` is the
    "check now" button, and is the only way to get past the cache.

    A hub with no internet reports the failure in `error` and nothing else: it is
    a convenience that could not be provided, not a fault in the hub.
    """
    values = await hubsettings.load(session)
    state = await get_update_checker().get(enabled=values.update_check_enabled, force=force)
    return {
        **updates.as_dict(state),
        # The checker knows how the hub was installed; only here does it also
        # know whether the trigger the button writes could actually be written.
        "self_update": state.self_update and selfupdate.available(get_settings().data_dir),
        "enabled": values.update_check_enabled,
    }


def _run_out(run: selfupdate.UpdateRun) -> dict[str, Any]:
    return {
        "requested": run.requested,
        "running": run.running,
        "finished": run.finished,
        "stalled": run.stalled,
        "exit_status": run.exit_status,
        "log": run.log,
    }


@router.get("/update/run")
async def update_run(_: CurrentUser) -> dict[str, Any]:
    """How far the upgrade this hub was asked to perform has got.

    Read from the file the privileged updater writes rather than held in memory,
    because the upgrade restarts this process halfway through — the state has to
    survive the thing it is describing.
    """
    return _run_out(selfupdate.read_run(get_settings().data_dir))


@router.post("/update/run")
async def start_update(_: CurrentUser) -> dict[str, Any]:
    """Ask to be upgraded. The hub does not perform it and cannot.

    All this does is create an empty file in the hub's own data directory. A
    systemd path unit watches for it and a root oneshot unit does the work; the
    hub has neither the privilege nor a way to influence what the upgrade
    installs. See services/selfupdate.py.
    """
    data_dir = get_settings().data_dir
    if install_method() != "native":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This hub cannot upgrade itself. See Settings → Updates for how to upgrade it.",
        )
    if not selfupdate.available(data_dir):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The hub cannot write to its data directory, so it cannot ask to be upgraded.",
        )
    try:
        selfupdate.request(data_dir)
    except OSError as caught:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Could not ask to be upgraded: {caught}"
        ) from caught
    return _run_out(selfupdate.read_run(data_dir))


@router.get("/log")
async def application_log(
    _: CurrentUser, cursor: int = 0, limit: int = 200
) -> dict[str, Any]:
    """The hub's own recent log lines, for reading without a shell.

    Not the query log — that is DNS traffic and lives on its own page. This is
    the hub talking about itself: what it did on start, a push that failed, a
    refused sign-in.

    `cursor` is the last sequence number the caller already has, so following
    along costs one small response per poll rather than the whole buffer. A
    cursor from before the buffer wrapped gets what is left, which is the honest
    answer — those lines are gone.
    """
    buffer = get_buffer()
    lines = buffer.since(cursor, limit=max(1, min(limit, logbuffer.MAX_LINES)))
    return {
        "lines": [
            {"seq": line.seq, "level": line.level, "logger": line.logger,
             "message": line.message, "at": line.at}
            for line in lines
        ],
        "cursor": lines[-1].seq if lines else cursor,
        "latest": buffer.latest_seq(),
        "capacity": logbuffer.MAX_LINES,
    }


@router.get("/drift-archive")
async def drift_archive(
    _: CurrentUser, after: int = 0, limit: int = driftarchive.MAX_PAGE
) -> dict[str, Any]:
    """The archived drift findings, newest first.

    The drift log on the dashboard answers "what is wrong now": capped at 500
    rows, one row per finding however often it repeats, rule lists trimmed, and
    emptied by *Clear log*. This is the other question — what happened, in
    order, with nothing left out — which by definition cannot be answered by the
    view that keeps itself short.

    `after` counts back from the newest entry rather than forward from the
    oldest, so a cursor stays valid as the file grows and across a rotation.
    """
    entries, more = driftarchive.read(limit=limit, after=max(0, after))
    return {
        "entries": [
            {
                "offset": entry.offset,
                "at": entry.at,
                "instance": entry.instance,
                "payload_kind": entry.payload_kind,
                "summary": entry.summary,
                "corrected": entry.corrected,
                "took_ms": entry.took_ms,
                "details": entry.details,
            }
            for entry in entries
        ],
        "more": more,
        "enabled": driftarchive.enabled(),
    }


@router.get("/notifiers/meta")
async def notifier_meta(_: CurrentUser) -> dict[str, list[str]]:
    return {"types": list(NOTIFIER_TYPES), "events": list(KNOWN_EVENTS)}


@router.get("/notifiers", response_model=list[NotifierOut])
async def list_notifiers(_: CurrentUser, session: SessionDep) -> list[NotifierOut]:
    result = await session.execute(select(NotifierTarget).order_by(NotifierTarget.id.asc()))
    return [to_out(target) for target in result.scalars().all()]


@router.post("/notifiers", response_model=NotifierOut, status_code=status.HTTP_201_CREATED)
async def create_notifier(
    payload: NotifierCreate, _: CurrentUser, session: SessionDep
) -> NotifierOut:
    target = NotifierTarget(
        name=payload.name,
        type=payload.type,
        url=payload.url,
        token=payload.token,
        enabled=payload.enabled,
        events=_validate_events(payload.events),
    )
    session.add(target)
    await session.commit()
    return to_out(target)


@router.patch("/notifiers/{target_id}", response_model=NotifierOut)
async def update_notifier(
    target_id: int, payload: NotifierUpdate, _: CurrentUser, session: SessionDep
) -> NotifierOut:
    target = await session.get(NotifierTarget, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notifier not found")
    data = payload.model_dump(exclude_unset=True)
    if "events" in data:
        target.events = _validate_events(data.pop("events") or [])
    for field, value in data.items():
        setattr(target, field, value)
    await session.commit()
    return to_out(target)


@router.delete(
    "/notifiers/{target_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_notifier(target_id: int, _: CurrentUser, session: SessionDep) -> None:
    target = await session.get(NotifierTarget, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notifier not found")
    await session.delete(target)
    await session.commit()


@router.post("/notifiers/{target_id}/test")
async def test_notifier(target_id: int, _: CurrentUser, session: SessionDep) -> dict[str, str]:
    target = await session.get(NotifierTarget, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notifier not found")
    error = await test_target(session, target)
    return {"ok": "false" if error else "true", "error": error}


def _hub_out(values: hubsettings.RuntimeSettings) -> HubSettingsOut:
    return HubSettingsOut(
        **vars(values),
        limits={field: list(bounds) for field, bounds in hubsettings.LIMITS.items()},
    )


@router.get("/hub", response_model=HubSettingsOut)
async def get_hub_settings(_: CurrentUser, session: SessionDep) -> HubSettingsOut:
    return _hub_out(await hubsettings.load(session))


@router.put("/hub", response_model=HubSettingsOut)
async def put_hub_settings(
    payload: HubSettingsUpdate, _: CurrentUser, session: SessionDep
) -> HubSettingsOut:
    """Timers and limits take effect on the next worker cycle — no restart needed."""
    return _hub_out(await hubsettings.update(session, payload.model_dump(exclude_unset=True)))
