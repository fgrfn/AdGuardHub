"""Instant push, retry queue and instance health (spec §6).

Every push is *full state*: AdGuardHub computes what an instance should look like
from the central DB and replaces the instance's managed config with it. That makes
pushes idempotent, lets the retry queue coalesce, and removes any need for merges.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import (
    AdapterError,
    DnsAdapter,
    RemoteFilterList,
    RemoteUpdate,
    build_adapter,
)
from ..db import session_scope
from ..models import (
    FilterList,
    Instance,
    InstanceStatus,
    JobStatus,
    PayloadKind,
    PushJob,
    Rule,
    utcnow,
)
from ..runtime import get_crypto
from . import hubsettings
from .config import managed_sections
from .events import bus
from .notify import (
    EVENT_INSTANCE_UNREACHABLE,
    EVENT_PUSH_FAILED,
    notify,
    notify_if_recovered,
)
from .retention import prune_applied_jobs

logger = logging.getLogger(__name__)

# Enough to identify what was refused without turning a status field into a
# wall of text; the drift log carries the full set.
MAX_REFUSED_SHOWN = 5

ALL_KINDS: tuple[PayloadKind, ...] = (
    PayloadKind.rules,
    PayloadKind.filters,
    PayloadKind.settings,
)


# --------------------------------------------------------------------------
# Desired state
# --------------------------------------------------------------------------


async def desired_rules(session: AsyncSession) -> list[str]:
    """The rule text every instance should carry, in a stable order."""
    result = await session.execute(
        select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.id.asc())
    )
    return [rule.text for rule in result.scalars().all()]


async def desired_filter_lists(session: AsyncSession) -> list[RemoteFilterList]:
    result = await session.execute(select(FilterList).order_by(FilterList.id.asc()))
    return [
        RemoteFilterList(name=item.name, url=item.url, enabled=item.enabled, kind=item.kind)
        for item in result.scalars().all()
    ]


async def desired_sections(session: AsyncSession) -> dict[str, dict]:
    """The configuration documents every instance should carry."""
    return await managed_sections(session)


async def has_desired_state(session: AsyncSession) -> bool:
    """Whether the hub holds anything at all to replicate.

    A hub with no rule, no subscription and no populated managed section has an
    empty desired state — and because every push is *full state*, "empty" is not
    a neutral value: reconciliation would read it as "these nodes should hold
    nothing" and clear them. A hub that was never set up therefore erases a
    working node on its first timer tick, which is precisely what happened to a
    second instance left standing on step 1 of the onboarding wizard with a
    production node already entered.

    Rules and subscriptions are counted as *rows*, not as enabled rows: a hub
    whose rules are all switched off has still been configured, and switching a
    rule off is a decision that must reach the nodes. Sections go through
    ``desired_sections`` because a section that is managed but has nothing
    imported yet pushes nothing either way.

    This deliberately gates reconciliation only. The instant push must keep
    running on an empty desired state, because deleting the last rule *produces*
    one — gating the push too would mean that deletion never reached a node.
    """
    if await session.scalar(select(Rule.id).limit(1)) is not None:
        return True
    if await session.scalar(select(FilterList.id).limit(1)) is not None:
        return True
    return bool(await desired_sections(session))


# --------------------------------------------------------------------------
# Push
# --------------------------------------------------------------------------


async def _not_kept(
    adapter: DnsAdapter, kind: PayloadKind, wanted: Any
) -> list[str]:
    """What the node accepted and then did not keep, read back straight away.

    A 2xx says the request was accepted, not that its contents were stored. The
    gap between those two is where this design's worst failure lives, and until
    now only reconciliation looked into it — five minutes later, and calling it
    drift, which is the wrong word for a write that never landed.

    Settings are deliberately not checked here. Deciding whether a section
    matches needs the comparison in services/reconcile.py, which knows that a
    node answering ``Europe/Berlin`` to a requested ``Local`` has obeyed rather
    than drifted; a plain equality check would report that as refused on every
    push. Reconciliation verifies sections properly within its interval.
    """
    if kind is PayloadKind.rules:
        landed = set(await adapter.pull_rules())
        return [rule for rule in wanted if rule not in landed]
    if kind is PayloadKind.filters:
        landed = {(item.kind, item.url) for item in await adapter.pull_filter_lists()}
        return [
            f"{item.kind}:{item.url}" for item in wanted if (item.kind, item.url) not in landed
        ]
    return []


def describe_refused(refused: dict[str, list[str]]) -> str:
    """One sentence naming what a node would not keep, or "" when it kept everything."""
    if not refused:
        return ""
    parts = []
    for kind, items in sorted(refused.items()):
        shown = ", ".join(items[:MAX_REFUSED_SHOWN])
        rest = len(items) - MAX_REFUSED_SHOWN
        parts.append(f"{kind}: {shown}" + (f" and {rest} more" if rest > 0 else ""))
    return "the node accepted the push and did not keep " + "; ".join(parts)


async def push_kind(
    session: AsyncSession,
    adapter: DnsAdapter,
    kind: PayloadKind,
) -> Any:
    """Push one payload. Returns what was sent, so it can be read back later.

    Reading back is deliberately not done here — see ``not_kept``.
    """
    if kind is PayloadKind.rules:
        wanted = await desired_rules(session)
        await adapter.push_rules(wanted)
    elif kind is PayloadKind.filters:
        wanted = await desired_filter_lists(session)
        await adapter.push_filter_lists(wanted)
    elif kind is PayloadKind.settings:
        wanted = None
        for name, data in (await desired_sections(session)).items():
            try:
                await adapter.push_section(name, data)
            except AdapterError as exc:
                # Name the section: "settings failed" alone is not actionable.
                raise AdapterError(f"section {name!r}: {exc}", status=exc.status) from exc
    else:  # pragma: no cover - PayloadKind has no fourth member
        wanted = None
    return wanted


async def not_kept(
    adapter: DnsAdapter, sent: dict[PayloadKind, Any]
) -> dict[str, list[str]]:
    """What the node accepted and then did not keep — read back after every write.

    The read-back happens at the end of the push rather than after each payload,
    because the writes are **not independent**. AdGuard reconfigures itself on
    every configuration change: a rule set, a subscription added, a settings
    section — each one is its own `reconfiguring server` on the node. A later
    reconfigure can undo what an earlier write put there.

    Verifying rules the moment they were sent, before the subscriptions and the
    settings sections had even left the hub, meant exactly one thing could
    happen and did: the hub confirmed a rule had landed, overwrote it a moment
    later with the rest of the push, and had nothing left to say about it. The
    operator saw *corrected*, and the rule was gone by the next run — which is
    the loop this verification exists to stop, one layer up from where it was
    fixed before.
    """
    refused: dict[str, list[str]] = {}
    for kind, wanted in sent.items():
        left = await _not_kept(adapter, kind, wanted)
        if left:
            refused[kind.value] = left
    return refused


# One lock per node, for as long as the process runs.
#
# Every push is full state, and every edit schedules one as a task of its own.
# Two edits a moment apart therefore race two full-state pushes to the same
# node — and full state is exactly what makes the order matter: whichever
# request AdGuard *finishes* last is the state it keeps. When the earlier push
# lands last (a slow login, a `reconfiguring server` pause on the node), the
# node holds the state from before the second edit, and nothing notices until
# reconciliation happens to look, up to five minutes later. That is the window
# this project exists to close. Reconciliation's own corrections push the same
# way and race the same way, so they take the same lock.
#
# Per node rather than one global lock, because a node that is slow or down
# must not delay the others (spec §6).
_push_locks: dict[int, asyncio.Lock] = {}


def push_lock(instance_id: int) -> asyncio.Lock:
    lock = _push_locks.get(instance_id)
    if lock is None:
        lock = _push_locks[instance_id] = asyncio.Lock()
    return lock


def reset_push_locks() -> None:
    """Forget every lock. For tests, which run each case on a fresh event loop."""
    _push_locks.clear()


async def push_to_instance(
    session: AsyncSession,
    instance: Instance,
    kinds: tuple[PayloadKind, ...],
    reason: str,
) -> str:
    """Push ``kinds`` to one instance. Returns an error string, or "" on success.

    Pushes to the same node run one at a time — see ``push_lock``. The desired
    state is read inside the lock, so the push that runs last carries the
    newest state, whatever order the edits behind them arrived in.
    """
    async with push_lock(instance.id):
        return await _push_to_instance(session, instance, kinds, reason)


async def _push_to_instance(
    session: AsyncSession,
    instance: Instance,
    kinds: tuple[PayloadKind, ...],
    reason: str,
) -> str:
    # Read once, before the probe: both notifications below are edge-triggered on
    # it, and after the push either branch has already overwritten the status.
    previous = instance.status
    adapter = build_adapter(instance, get_crypto())
    refused: dict[str, list[str]] = {}
    try:
        sent: dict[PayloadKind, Any] = {}
        for kind in kinds:
            sent[kind] = await push_kind(session, adapter, kind)
        # Every write is done; only now is it safe to ask what survived them all.
        refused = await not_kept(adapter, sent)
    except (AdapterError, ValueError) as exc:
        error = str(exc)
        was_online = previous == InstanceStatus.online.value
        logger.warning(
            "Push to %s failed (%s): %s — queued for retry",
            instance.name,
            reason or "sync",
            error,
        )
        instance.status = InstanceStatus.unreachable.value
        instance.last_error = error
        for kind in kinds:
            await queue_job(session, instance, kind, reason, error)
        await session.commit()
        await bus.publish(
            "instance.status",
            {"id": instance.id, "name": instance.name, "status": instance.status, "error": error},
        )
        await notify(
            EVENT_PUSH_FAILED,
            f"Push to {instance.name} failed",
            f"{reason or 'Sync'} could not be applied: {error}. Queued for retry.",
        )
        if was_online:
            await notify(
                EVENT_INSTANCE_UNREACHABLE,
                f"{instance.name} is unreachable",
                f"AdGuardHub can no longer reach {instance.base_url}: {error}",
            )
        return error
    finally:
        await adapter.aclose()

    # A refusal is not a transport failure, and the difference decides everything
    # that follows. The node answered, so it is online and was seen. The write
    # will not start working on its own, so it must not go to the retry queue —
    # that queue exists for a node that was unreachable, and feeding it something
    # the node actively will not keep rebuilds, one layer down, exactly the loop
    # reconciliation was just taught to stop.
    kept = describe_refused(refused)
    if kept:
        logger.warning("%s: %s", instance.name, kept)
    else:
        logger.info(
            "Pushed %s to %s (%s)",
            ", ".join(kind.value for kind in kinds),
            instance.name,
            reason or "sync",
        )

    instance.status = InstanceStatus.online.value
    instance.last_error = kept
    instance.last_seen_at = utcnow()
    # Only what actually landed counts as synced. Saying otherwise is the claim
    # that made the same failure invisible in reconciliation for two releases.
    if not refused:
        instance.last_synced_at = utcnow()
    for kind in kinds:
        await close_jobs(session, instance, kind)
    await session.commit()
    # The jobs just closed are history now. Trimming here rather than on a timer
    # keeps the cap true even when reconciliation is switched off.
    await prune_applied_jobs(session)
    await bus.publish(
        "instance.status",
        {"id": instance.id, "name": instance.name, "status": instance.status, "error": ""},
    )
    await notify_if_recovered(instance, previous)
    if refused:
        await notify(
            EVENT_PUSH_FAILED,
            f"{instance.name} did not keep part of the push",
            f"{reason or 'Sync'}: {kept}. Not queued for a retry — the node answered, "
            "so repeating the same write would not change the outcome.",
        )
    return kept


async def sync_all(
    session: AsyncSession,
    kinds: tuple[PayloadKind, ...] = ALL_KINDS,
    reason: str = "",
) -> dict[str, str]:
    """Instant push to every enabled instance, best effort and without rollback.

    A failing instance never blocks the others: its work goes to the retry queue.
    """
    result = await session.execute(select(Instance).where(Instance.enabled.is_(True)))
    instances = list(result.scalars().all())
    errors: dict[str, str] = {}
    for instance in instances:
        # A node in maintenance is being worked on by hand, and a push would
        # overwrite exactly that work. The change is not dropped, though: it goes
        # to the same queue an unreachable node uses, and is replayed when
        # maintenance ends. That is the whole difference from disabling it.
        if instance.maintenance:
            for kind in kinds:
                await queue_job(session, instance, kind, reason or "maintenance", "")
            await session.commit()
            continue
        error = await push_to_instance(session, instance, kinds, reason)
        if error:
            errors[instance.name] = error
    await bus.publish(
        "sync",
        {
            "reason": reason,
            "kinds": [kind.value for kind in kinds],
            "instances": len(instances),
            "failed": list(errors),
        },
    )
    return errors


def schedule_sync(kinds: tuple[PayloadKind, ...], reason: str) -> None:
    """Fire-and-forget push so an API request returns without waiting on the network."""

    async def _run() -> None:
        try:
            async with session_scope() as session:
                await sync_all(session, kinds, reason)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Background sync failed (%s)", reason)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


_background_tasks: set[asyncio.Task[None]] = set()


async def drain_background() -> None:
    """Wait for every in-flight ``schedule_sync`` task. Used on shutdown and in tests."""
    while _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


# --------------------------------------------------------------------------
# Retry queue
# --------------------------------------------------------------------------


async def queue_job(
    session: AsyncSession, instance: Instance, kind: PayloadKind, reason: str, error: str
) -> PushJob:
    """Open (or update) the single outstanding job for this instance + payload kind."""
    result = await session.execute(
        select(PushJob).where(
            PushJob.instance_id == instance.id,
            PushJob.payload_kind == kind.value,
            PushJob.status != JobStatus.applied.value,
        )
    )
    job = result.scalars().first()
    if job is None:
        job = PushJob(instance_id=instance.id, payload_kind=kind.value, reason=reason)
        session.add(job)
    job.status = JobStatus.failed.value if error else JobStatus.pending.value
    # ``attempts`` only gets its column default at flush time, so a freshly added
    # job still has None here.
    job.attempts = (job.attempts or 0) + 1
    job.last_error = error
    job.updated_at = utcnow()
    if reason:
        job.reason = reason
    return job


async def close_jobs(session: AsyncSession, instance: Instance, kind: PayloadKind) -> None:
    """A successful full-state push satisfies any outstanding job of the same kind."""
    result = await session.execute(
        select(PushJob).where(
            PushJob.instance_id == instance.id,
            PushJob.payload_kind == kind.value,
            PushJob.status != JobStatus.applied.value,
        )
    )
    for job in result.scalars().all():
        job.status = JobStatus.applied.value
        job.last_error = ""
        job.updated_at = utcnow()


async def process_retry_queue(session: AsyncSession) -> int:
    """Retry every open job whose instance is enabled. Returns the number recovered."""
    result = await session.execute(
        select(PushJob, Instance)
        .join(Instance, Instance.id == PushJob.instance_id)
        .where(
            PushJob.status != JobStatus.applied.value,
            Instance.enabled.is_(True),
            Instance.maintenance.is_(False),
        )
        .order_by(PushJob.id.asc())
    )
    rows = list(result.all())
    recovered = 0
    seen: set[tuple[int, str]] = set()
    for job, instance in rows:
        key = (instance.id, job.payload_kind)
        if key in seen:
            continue
        seen.add(key)
        error = await push_to_instance(
            session, instance, (PayloadKind(job.payload_kind),), job.reason or "retry"
        )
        if not error:
            recovered += 1
    if recovered:
        logger.info("Retry queue: %d of %d open job(s) applied", recovered, len(seen))
        await bus.publish("retry", {"recovered": recovered})
    elif seen:
        logger.debug("Retry queue: %d open job(s), none applied yet", len(seen))
    return recovered


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


async def check_instance(session: AsyncSession, instance: Instance) -> str:
    """Probe one instance and record its status. Returns an error string, or ""."""
    if not instance.enabled:
        instance.status = InstanceStatus.disabled.value
        await session.commit()
        return ""
    was = instance.status
    adapter = build_adapter(instance, get_crypto())
    update = RemoteUpdate()
    try:
        version = await adapter.check()
        # Asked here too, not only on the reconcile timer. Pressing Test on a node
        # whose update line looks wrong is the obvious thing to try, and this path
        # used to refresh every field on the card except that one — so the wrong
        # line survived every attempt to clear it until the timer came round.
        #
        # Suppressed on its own: what this function reports is whether the node is
        # reachable, and an update endpoint that will not answer is not the node
        # being down.
        with contextlib.suppress(AdapterError, ValueError):
            update = await adapter.check_update(version)
    except (AdapterError, ValueError) as exc:
        was_online = instance.status == InstanceStatus.online.value
        # Only the transition. A node that has been down for an hour would
        # otherwise write the same line every reconcile pass.
        if was_online:
            logger.warning("%s is unreachable: %s", instance.name, exc)
        else:
            logger.debug("%s still unreachable: %s", instance.name, exc)
        instance.status = InstanceStatus.unreachable.value
        instance.last_error = str(exc)
        await session.commit()
        if was_online:
            await notify(
                EVENT_INSTANCE_UNREACHABLE,
                f"{instance.name} is unreachable",
                f"AdGuardHub can no longer reach {instance.base_url}: {exc}",
            )
        await _announce_status(instance, changed=was != instance.status)
        return str(exc)
    finally:
        await adapter.aclose()

    if was != InstanceStatus.online.value:
        logger.info("%s answered again (%s)", instance.name, version)
    instance.status = InstanceStatus.online.value
    # check() already asked /control/status for it; throwing it away meant the UI
    # could never say which AdGuard version a node is running.
    instance.version = version
    instance.update_version = update.latest if update.available else ""
    instance.update_url = update.url if update.available else ""
    instance.update_error = update.error
    instance.last_error = ""
    instance.last_seen_at = datetime.now(UTC)
    await session.commit()
    await _announce_status(instance, changed=was != instance.status)
    await notify_if_recovered(instance, was)
    return ""


async def _announce_status(instance: Instance, *, changed: bool) -> None:
    """Let open browsers know, so the status pill does not sit on a stale list."""
    if not changed:
        return
    await bus.publish(
        "instance.status",
        {
            "id": instance.id,
            "name": instance.name,
            "status": instance.status,
            "error": instance.last_error,
        },
    )


async def retry_worker(stop: asyncio.Event) -> None:  # pragma: no cover - background loop
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=hubsettings.current().retry_interval)
            return
        except TimeoutError:
            pass
        try:
            async with session_scope() as session:
                await process_retry_queue(session)
        except Exception:
            logger.exception("Retry queue pass failed")
