"""Drift detection and auto-correction (spec §6).

Anything changed on an instance out-of-band — after downtime, or by someone using the
native UI despite §2 — is detected here, corrected, and written to the drift log. A
correction is always applied, but never silently.

The one exception is an instance in maintenance: there somebody is working on the node
deliberately, and correcting them every five minutes is the opposite of helping.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import AdapterError, RemoteFilterList, build_adapter
from ..db import session_scope
from ..models import DriftEvent, Instance, InstanceStatus, PayloadKind, utcnow
from ..runtime import get_crypto
from . import hubsettings
from .events import bus
from .notify import (
    EVENT_INSTANCE_UNREACHABLE,
    EVENT_RECONCILE_FIX,
    notify,
    notify_if_recovered,
)
from .retention import prune_drift_events
from .sync import desired_filter_lists, desired_rules, desired_sections, push_kind, push_lock

logger = logging.getLogger(__name__)

MAX_DETAIL_ITEMS = 25


@dataclass(slots=True)
class Difference:
    payload_kind: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InstanceReport:
    instance_id: int
    instance_name: str
    checked: bool
    error: str = ""
    differences: list[Difference] = field(default_factory=list)
    corrected: bool = False


def _trim(items: list[str]) -> list[str]:
    if len(items) <= MAX_DETAIL_ITEMS:
        return items
    return [*items[:MAX_DETAIL_ITEMS], f"… and {len(items) - MAX_DETAIL_ITEMS} more"]


def diff_rules(expected: list[str], actual: list[str]) -> Difference | None:
    if expected == actual:
        return None
    missing = _trim([rule for rule in expected if rule not in set(actual)])
    extra = _trim([rule for rule in actual if rule not in set(expected)])
    if missing or extra:
        summary = f"{len(missing)} rule(s) missing, {len(extra)} unexpected rule(s)"
    else:
        summary = "rules present but in a different order"
    return Difference(PayloadKind.rules.value, summary, {"missing": missing, "extra": extra})


def _list_key(item: RemoteFilterList) -> tuple[str, str]:
    return (item.kind, item.url)


def diff_filter_lists(
    expected: list[RemoteFilterList], actual: list[RemoteFilterList]
) -> Difference | None:
    expected_map = {_list_key(item): item for item in expected}
    actual_map = {_list_key(item): item for item in actual}
    missing = [f"{kind}:{url}" for kind, url in expected_map.keys() - actual_map.keys()]
    extra = [f"{kind}:{url}" for kind, url in actual_map.keys() - expected_map.keys()]
    changed = [
        f"{kind}:{url}"
        for (kind, url), item in expected_map.items()
        if (kind, url) in actual_map and actual_map[(kind, url)].enabled != item.enabled
    ]
    if not (missing or extra or changed):
        return None
    summary = (
        f"{len(missing)} subscription(s) missing, {len(extra)} unexpected, "
        f"{len(changed)} with a different enabled state"
    )
    return Difference(
        PayloadKind.filters.value,
        summary,
        {"missing": sorted(missing), "extra": sorted(extra), "changed": sorted(changed)},
    )


def _normalise(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in sorted(value.items())}
    return value


# Settings each node is entitled to answer for itself, by the path they sit at.
#
# "Local" is not a time zone; it is an instruction to use whichever zone the node
# is in. A node reading back "Europe/Berlin" has obeyed that instruction rather
# than drifted from it — but comparing the request against the answer made every
# reconciliation run report a difference, correct it by pushing "Local" again, and
# find the same difference on the next run. Forever, on any node whose clock knows
# where it is, filling the drift log and firing a notification each time.
#
# Only the placeholder is forgiven. A hub that says Europe/Berlin and a node that
# says something else is still drift, and is still corrected.
SELF_RESOLVED: dict[tuple[str, ...], frozenset[str]] = {
    ("blocked_services", "schedule", "time_zone"): frozenset({"Local", ""}),
}


def _equivalent(expected: Any, actual: Any, path: tuple[str, ...]) -> bool:
    """Whether the node's answer satisfies what the hub asked for, at this path."""
    allowed = SELF_RESOLVED.get(path)
    if allowed is not None and expected in allowed:
        return True
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            return False
        return all(_equivalent(expected[key], actual[key], path + (key,)) for key in expected)
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        return all(
            _equivalent(item, other, path)
            for item, other in zip(expected, actual, strict=True)
        )
    return _normalise(expected) == _normalise(actual)


def diff_section(
    name: str, expected: dict[str, Any], actual: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Per-key differences for one section, or ``None`` when it matches.

    ``actual`` is ``None`` when the instance does not implement the section; that is
    not drift, just a capability difference, so it is reported without a correction.
    """
    if actual is None:
        return {"unsupported": True}
    changed = {
        key: {"expected": _normalise(value), "actual": _normalise(actual.get(key))}
        for key, value in expected.items()
        if not _equivalent(value, actual.get(key), (name, key))
    }
    return changed or None


def diff_settings(
    expected: dict[str, dict[str, Any]], actual: dict[str, dict[str, Any] | None]
) -> Difference | None:
    """Fold every managed section into one difference for the settings payload."""
    details: dict[str, Any] = {}
    unsupported: list[str] = []
    for name, wanted in expected.items():
        found = diff_section(name, wanted, actual.get(name))
        if found is None:
            continue
        if found.get("unsupported"):
            unsupported.append(name)
            continue
        details[name] = found

    if not details and not unsupported:
        return None

    parts = []
    if details:
        parts.append(f"{len(details)} section(s) differ: {', '.join(sorted(details))}")
    if unsupported:
        parts.append(f"not supported by this instance: {', '.join(sorted(unsupported))}")
    payload: dict[str, Any] = dict(details)
    if unsupported:
        payload["_unsupported"] = unsupported
    return Difference(PayloadKind.settings.value, "; ".join(parts), payload)


def is_correctable(difference: Difference) -> bool:
    """Whether pushing can actually resolve this difference.

    A settings difference that is only "the instance does not implement this area"
    cannot be pushed away, and must not be treated as drift.
    """
    if difference.payload_kind != PayloadKind.settings.value:
        return True
    return bool(set(difference.details) - {"_unsupported"})


async def _still_differs(
    session: AsyncSession,
    adapter: Any,
    kind: str,
    expected_sections: dict[str, dict[str, Any]],
) -> Difference | None:
    """Re-read one payload from the node and diff it again.

    A 2xx from AdGuard means it accepted the request, not that it kept what was
    in it. Without asking again, "corrected" is a claim about the write having
    been sent — which is exactly how a rule the node quietly refused could be
    reported as fixed every five minutes, forever, while never arriving.

    Only the payload that was just pushed is re-read, not the whole state.
    """
    if kind == PayloadKind.rules.value:
        return diff_rules(await desired_rules(session), await adapter.pull_rules())
    if kind == PayloadKind.filters.value:
        return diff_filter_lists(
            await desired_filter_lists(session), await adapter.pull_filter_lists()
        )
    actual = {name: await adapter.pull_section(name) for name in expected_sections}
    return diff_settings(expected_sections, actual)


async def _already_said(
    session: AsyncSession, instance_id: int, kind: str, summary: str, details: str
) -> bool:
    """Whether the newest entry for this instance and payload already says this.

    A refusal repeats by definition: the node goes on not keeping the same thing,
    so every run would write the same entry. One says it; five hundred bury it.
    Only refusals are held back this way — an out-of-band change that keeps being
    made and corrected is genuinely new each time and stays in the log.
    """
    row = (
        (
            await session.execute(
                select(DriftEvent)
                .where(DriftEvent.instance_id == instance_id)
                .where(DriftEvent.payload_kind == kind)
                .order_by(DriftEvent.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return row is not None and row.summary == summary and row.details == details


async def _correct(
    session: AsyncSession,
    adapter: Any,
    correctable: list[Difference],
    expected_sections: dict[str, dict[str, Any]],
    fixed: set[str],
    refused: dict[str, Difference],
    failed: dict[str, str],
) -> None:
    """Push each correctable difference and sort it into fixed, refused or failed."""
    for difference in correctable:
        # Per difference rather than around the loop. A settings section one
        # AdGuard build rejects used to abort the pass, so the rule set was
        # never corrected — and the drift row still said only "detected".
        # Best-effort with no rollback is the rule everywhere else in the
        # sync engine; one shared try block quietly suspended it here.
        try:
            await push_kind(session, adapter, PayloadKind(difference.payload_kind))
            remaining = await _still_differs(
                session, adapter, difference.payload_kind, expected_sections
            )
        except (AdapterError, ValueError) as exc:
            failed[difference.payload_kind] = str(exc)
            continue
        if remaining is None:
            fixed.add(difference.payload_kind)
        else:
            refused[difference.payload_kind] = remaining


async def reconcile_instance(
    session: AsyncSession, instance: Instance, *, apply_fixes: bool = True
) -> InstanceReport:
    report = InstanceReport(instance.id, instance.name, checked=False)
    if not instance.enabled:
        return report
    # The point of maintenance mode: whatever the operator is doing to this node
    # by hand, reconciliation would undo it within five minutes.
    if instance.maintenance:
        return report

    expected_sections = await desired_sections(session)
    # Both the outage and the recovery notice are edge-triggered on this, and the
    # branches below overwrite the status before either can be decided.
    previous = instance.status
    adapter = build_adapter(instance, get_crypto())
    # Closed however this ends. The two hand-placed aclose() calls covered the
    # pull failing and the pass finishing; anything else that escaped between
    # them — a fault in the diff, a cancellation at shutdown — leaked the
    # node's HTTP client and its connection.
    async with contextlib.aclosing(adapter):
        try:
            state = await adapter.pull_state(tuple(expected_sections))
        except (AdapterError, ValueError) as exc:
            report.error = str(exc)
            was_online = previous == InstanceStatus.online.value
            instance.status = InstanceStatus.unreachable.value
            instance.last_error = report.error
            await session.commit()
            if was_online:
                await notify(
                    EVENT_INSTANCE_UNREACHABLE,
                    f"{instance.name} is unreachable",
                    f"Reconciliation could not reach {instance.base_url}: {exc}",
                )
            return report

        report.checked = True
        instance.status = InstanceStatus.online.value
        instance.last_error = ""
        instance.last_seen_at = utcnow()
        # /control/status is the cheapest call AdGuard has, and reconciliation is the
        # only thing that talks to every node on a timer. Without this the reported
        # version would only ever refresh when the operator pressed Test by hand.
        with contextlib.suppress(AdapterError, ValueError):
            instance.version = await adapter.check()

        # Asked here for the same reason as the version: this is the only thing that
        # talks to every node on a timer. The node answers from its own cached check
        # rather than reaching out, so this costs one local request.
        with contextlib.suppress(AdapterError, ValueError):
            # The version read a moment ago is what "is there a newer one" is asked
            # against: AdGuard's version endpoint says what exists, never what is
            # installed.
            update = await adapter.check_update(instance.version or "")
            instance.update_version = update.latest if update.available else ""
            instance.update_url = update.url if update.available else ""
            instance.update_error = update.error

        # Written out now, before the pass talks to the node again. The status
        # fields above mark the row dirty, and the first query below would flush
        # them — which opens a write transaction that SQLite backs with a lock on
        # the whole file, held until this session commits. That commit used to be
        # at the very end, after every correction push and read-back, so against a
        # slow node the hub could not accept a single edit for as long as the
        # correction took: each waited on SQLite's busy timeout and failed with
        # "database is locked". Nothing below needs these writes to be pending.
        await session.commit()

        candidates = [
            diff_rules(await desired_rules(session), state.rules),
            diff_filter_lists(await desired_filter_lists(session), state.filter_lists),
            diff_settings(expected_sections, state.sections),
        ]
        report.differences = [item for item in candidates if item is not None]

        correctable = [item for item in report.differences if is_correctable(item)]
        fixed: set[str] = set()
        # What survived being corrected, by payload kind. The node took the request
        # and did not keep the result — the one outcome the old code could not tell
        # apart from success, and the one that turns into an endless loop.
        refused: dict[str, Difference] = {}
        # What could not be attempted at all: the push itself errored, so the node
        # never took the write. Kept apart from a refusal, and from success, because
        # all three used to reach the operator as the single word "detected".
        failed: dict[str, str] = {}
        if correctable and apply_fixes:
            # A correction is a full-state push like any other, and races an edit's
            # push to the same node the same way — see sync.push_lock. Held around
            # the read-back too, so what is verified is what this pass wrote.
            async with push_lock(instance.id):
                await _correct(
                    session, adapter, correctable, expected_sections, fixed, refused, failed
                )
            # True only if something actually landed. Claiming a correction that
            # did not stick is what made this invisible for as long as it was.
            report.corrected = bool(fixed)
            if fixed:
                instance.last_synced_at = utcnow()
            if failed:
                report.error = "; ".join(f"{kind}: {text}" for kind, text in sorted(failed.items()))


    if correctable:
        logger.info(
            "Reconcile %s: %s%s",
            instance.name,
            "; ".join(item.summary for item in correctable),
            (
                f" — the correction could not be pushed: {report.error}"
                if failed
                else " — not kept by the node"
                if refused
                else " — corrected"
                if fixed
                else " — not corrected"
            ),
        )
    else:
        logger.debug("Reconcile %s: no differences", instance.name)

    logged: list[Difference] = []
    for difference in report.differences:
        if not is_correctable(difference):
            # A section this AdGuard build does not implement is a standing capability
            # gap, not drift. Logging it would append the same entry on every run.
            continue
        remaining = refused.get(difference.payload_kind)
        error = failed.get(difference.payload_kind)
        details = json.dumps(difference.details, default=str)
        if error:
            # The reason belongs in the row. It was going into report.error,
            # which nothing persists, so a pass that tried and could not push
            # was indistinguishable from one that never tried — both read
            # "detected", five minutes apart, for as long as the fault lasted.
            summary = f"{difference.summary} — the correction could not be pushed: {error}"
        elif remaining is None:
            summary = difference.summary
        else:
            # Said as what it is. "1 rule(s) missing, corrected" describes a
            # correction that worked; this one did not, and the operator needs to
            # know that rather than watch it repeat.
            summary = f"the node did not keep this correction — {remaining.summary}"
            details = json.dumps(remaining.details, default=str)
        # A refusal and a failing push both repeat on every run by definition,
        # so each is stated once and again when it changes. A plain difference
        # is not suppressed: it is expected to be corrected, and a second one
        # means the correction is not holding.
        if (error or remaining is not None) and await _already_said(
            session, instance.id, difference.payload_kind, summary, details
        ):
            continue
        session.add(
            DriftEvent(
                instance_id=instance.id,
                instance_name=instance.name,
                payload_kind=difference.payload_kind,
                summary=summary,
                details=details,
                # Per difference: a later push can fail after an earlier one succeeded.
                corrected=difference.payload_kind in fixed,
            )
        )
        logged.append(Difference(difference.payload_kind, summary, difference.details))

    # Whether the node currently holds what the hub wants — the question its
    # status could not answer. Drift that was found and corrected does not count:
    # that is reconciliation working. What counts is a difference still standing
    # after the attempt, because the push errored or the node would not keep it.
    #
    # A dry run leaves this alone. Nothing was attempted, so it has no business
    # saying whether a correction would have held.
    if apply_fixes:
        if failed or refused:
            # First seen, not last: the useful number is how long this has been
            # true, and re-stamping it every five minutes would reset that.
            instance.out_of_sync_since = instance.out_of_sync_since or utcnow()
        else:
            instance.out_of_sync_since = None

    await session.commit()
    await prune_drift_events(session)

    # Before any drift notice: "node-b is back" then "drift corrected on node-b"
    # is the order the two actually happened in. A dry run still sends this —
    # reaching a node is an observation, not a correction, and the status is
    # recorded either way.
    await notify_if_recovered(instance, previous)

    # Tied to what was written, not to what was found. A refusal is found on every
    # run by definition, and notifying each time is how the last loop of this shape
    # sent a message every five minutes for weeks about a fault that never existed.
    if logged:
        await bus.publish("drift", {"instance": instance.name, "report": asdict(report)})
        headline = "; ".join(difference.summary for difference in logged)
        if failed:
            title = f"A correction could not be pushed to {instance.name}"
        elif refused:
            title = f"A correction did not hold on {instance.name}"
        else:
            title = f"Drift {'corrected' if report.corrected else 'detected'} on {instance.name}"
        await notify(EVENT_RECONCILE_FIX, title, headline)
    return report


async def reconcile_all(session: AsyncSession, *, apply_fixes: bool = True) -> list[InstanceReport]:
    result = await session.execute(
        select(Instance).where(Instance.enabled.is_(True)).order_by(Instance.id.asc())
    )
    reports = []
    for instance in result.scalars().all():
        reports.append(await reconcile_instance(session, instance, apply_fixes=apply_fixes))
    # At DEBUG because it runs on a timer: the answer to "did it run at all" has
    # to exist somewhere, and it must not be in everyone's log every five minutes.
    logger.debug(
        "Reconcile pass over %d instance(s), %d unreachable, %d with differences",
        len(reports),
        sum(1 for item in reports if not item.checked),
        sum(1 for item in reports if item.differences),
    )
    return reports


async def reconcile_worker(stop: asyncio.Event) -> None:  # pragma: no cover - background loop
    while not stop.is_set():
        settings = hubsettings.current()
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.reconcile_interval)
            return
        except TimeoutError:
            pass
        if not hubsettings.current().reconcile_enabled:
            continue
        try:
            async with session_scope() as session:
                await reconcile_all(session)
        except Exception:
            logger.exception("Reconciliation pass failed")
