"""Pluggable webhook notifications (spec §10)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import session_scope
from ..models import Instance, InstanceStatus, NotifierTarget
from .events import bus

logger = logging.getLogger(__name__)

# Events that can fire a notification. The UI offers these as checkboxes.
EVENT_RECONCILE_FIX = "reconcile.fixed"
EVENT_INSTANCE_UNREACHABLE = "instance.unreachable"
EVENT_INSTANCE_RECOVERED = "instance.recovered"
EVENT_PUSH_FAILED = "push.failed"
# The safety net itself, which nothing used to watch. Supervision covers a worker
# that ends; these cover one that hangs, and one switched off months ago and
# forgotten — neither of which anything in the hub said a word about.
EVENT_RECONCILE_STALLED = "reconcile.stalled"
EVENT_RECONCILE_RESUMED = "reconcile.resumed"

KNOWN_EVENTS = (
    EVENT_RECONCILE_FIX,
    EVENT_INSTANCE_UNREACHABLE,
    EVENT_INSTANCE_RECOVERED,
    EVENT_PUSH_FAILED,
    EVENT_RECONCILE_STALLED,
    EVENT_RECONCILE_RESUMED,
)
NOTIFIER_TYPES = ("homeassistant", "discord", "gotify")


def build_payload(target: NotifierTarget, event: str, title: str, message: str) -> dict[str, Any]:
    """Shape one notification for the target's specific webhook contract."""
    if target.type == "discord":
        return {"content": f"**{title}**\n{message}"}
    if target.type == "gotify":
        return {"title": title, "message": message, "priority": 5}
    # Home Assistant webhook triggers accept arbitrary JSON, exposed as trigger.json.
    return {"event": event, "title": title, "message": message, "source": "adguardhub"}


def _request_kwargs(target: NotifierTarget, payload: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"json": payload}
    if target.type == "gotify" and target.token:
        kwargs["headers"] = {"X-Gotify-Key": target.token}
    elif target.token:
        kwargs["headers"] = {"Authorization": f"Bearer {target.token}"}
    return kwargs


def target_wants(target: NotifierTarget, event: str) -> bool:
    """An empty ``events`` list means "everything"."""
    selected = [item for item in (target.events or "").split(",") if item]
    return not selected or event in selected


async def send_to_target(
    client: httpx.AsyncClient, target: NotifierTarget, event: str, title: str, message: str
) -> str:
    """Deliver one notification. Returns an error string, or "" on success."""
    payload = build_payload(target, event, title, message)
    try:
        response = await client.post(target.url, **_request_kwargs(target, payload))
    except httpx.HTTPError as exc:
        return str(exc)
    if response.status_code >= 400:
        return f"HTTP {response.status_code}: {response.text.strip()[:160]}"
    return ""


async def notify(event: str, title: str, message: str) -> None:
    """Fan a notification out to every enabled target subscribed to ``event``.

    Never raises: a broken webhook must not take down a sync or reconcile run.
    """
    await bus.publish("notification", {"event": event, "title": title, "message": message})
    try:
        async with session_scope() as session:
            targets = list(
                (
                    await session.execute(
                        select(NotifierTarget).where(NotifierTarget.enabled.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            wanted = [target for target in targets if target_wants(target, event)]
            if not wanted:
                return
            timeout = get_settings().http_timeout
            async with httpx.AsyncClient(timeout=timeout) as client:
                for target in wanted:
                    error = await send_to_target(client, target, event, title, message)
                    if error:
                        logger.warning("Notifier '%s' failed: %s", target.name, error)
                    target.last_error = error
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Notification dispatch failed for event %s", event)


async def notify_if_recovered(instance: Instance, previous: str) -> None:
    """Announce a node that has come back — the other half of ``unreachable``.

    Without this the hub only ever reported bad news: you were told at 3am that a
    node had gone, and never that it returned, so the only way to learn the
    outage was over was to go and look.

    Edge-triggered on unreachable → online, exactly as the outage notice is
    edge-triggered the other way. Announcing on *every* successful pass instead
    would mean a message per reconciliation for a node that is merely still up,
    which is how a useful alert becomes one people mute.

    ``previous`` is the status read before the probe, not after: by the time this
    is called the caller has already set the instance online.
    """
    if previous != InstanceStatus.unreachable.value:
        return
    await notify(
        EVENT_INSTANCE_RECOVERED,
        f"{instance.name} is reachable again",
        f"AdGuardHub can reach {instance.base_url} again. "
        "Anything queued for it is applied on the next retry pass.",
    )


async def test_target(session: AsyncSession, target: NotifierTarget) -> str:
    timeout = get_settings().http_timeout
    async with httpx.AsyncClient(timeout=timeout) as client:
        error = await send_to_target(
            client,
            target,
            "test",
            "AdGuardHub test notification",
            f"Notifier '{target.name}' is wired up correctly.",
        )
    target.last_error = error
    await session.commit()
    return error
