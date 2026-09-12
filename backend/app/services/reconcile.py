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
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import AdapterError, RemoteFilterList, build_adapter
from ..adapters.compare import section_differences
from ..db import session_scope
from ..models import DriftEvent, Instance, InstanceStatus, PayloadKind, ReconcileRun, utcnow
from ..runtime import get_crypto
from . import driftarchive, hubsettings
from .events import bus
from .notify import (
    EVENT_INSTANCE_UNREACHABLE,
    EVENT_RECONCILE_FIX,
    notify,
    notify_if_recovered,
)
from .retention import prune_drift_events, prune_reconcile_runs
from .sync import (
    desired_filter_lists,
    desired_rules,
    desired_sections,
    has_desired_state,
    push_kind,
    push_lock,
)

logger = logging.getLogger(__name__)

MAX_DETAIL_ITEMS = 25

#: A pass may take this fraction of the interval before it is abandoned.
#:
#: Not the whole interval: two passes overlapping is a worse failure than one
#: cut short, because both hold the same per-node push lock and the second would
#: block behind the first for as long as it ran. Not a fixed number of seconds
#: either — a pass over a node fetching twenty blocklists legitimately takes a
#: minute, and a deadline tighter than the work is a loop that never completes.
PASS_DEADLINE_FRACTION = 0.8

#: Whatever the interval, a pass gets at least this long. The interval is
#: settable down to 30 seconds, and 24 of those is shorter than a single
#: ``add_url`` on a large blocklist.
MIN_PASS_DEADLINE = 120.0


def pass_deadline(interval: int) -> float:
    """How long one reconciliation pass may take, for a given interval."""
    return max(MIN_PASS_DEADLINE, interval * PASS_DEADLINE_FRACTION)

NOTHING_TO_REPLICATE = (
    "The hub holds no rule, no subscription and no populated managed section, so there is "
    "nothing to reconcile against. Reconciliation starts with the first of them."
)

#: Whether the "nothing to replicate" notice has already been given. The state it
#: reports is not a fault and does not change on its own — an unconfigured hub is
#: unconfigured on every tick — so it is said once at WARNING and then kept at
#: DEBUG, the same rule the drift log follows for a repeating refusal.
_said_nothing_to_replicate = False


def _ms_since(started: float) -> int:
    """Whole milliseconds since a ``perf_counter`` reading.

    Whole, because these are network round trips against a node on the LAN: the
    interesting range is tens of milliseconds to the minute an ``add_url`` takes,
    and a fractional millisecond in a drift row is a digit nobody reads.
    """
    return int((time.perf_counter() - started) * 1000)


def human_ms(ms: int) -> str:
    """A duration for the log line a person reads.

    Stored as milliseconds and rendered in the unit that fits, because the spans
    involved are three orders of magnitude apart: a push to a healthy node is
    tens of milliseconds, a node fetching a list is seconds, and a large
    blocklist is a minute or more. `pass 10200 ms` makes the reader do the
    arithmetic at exactly the moment they are trying to spot a round number.
    """
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms // 60_000} min {round((ms % 60_000) / 1000)} s"


@dataclass(slots=True)
class Difference:
    payload_kind: str
    summary: str
    #: What the drift row and the interface get: capped at MAX_DETAIL_ITEMS, so
    #: a node missing four thousand rules does not put four thousand strings in
    #: a table cell or an API response.
    details: dict[str, Any] = field(default_factory=dict)
    #: The same finding with nothing left out, for the archive on disk. Empty
    #: when nothing was trimmed, in which case ``details`` is already complete —
    #: only rules are ever capped.
    #:
    #: Two fields rather than trimming at each edge: `details` is compared
    #: verbatim to decide whether a finding is the one already logged, so the
    #: value that is written must be the value that is compared.
    full_details: dict[str, Any] = field(default_factory=dict)

    def archived(self) -> dict[str, Any]:
        """The finding as the archive should keep it — the cap is a view, not the truth."""
        return self.full_details or self.details


@dataclass(slots=True)
class InstanceReport:
    instance_id: int
    instance_name: str
    checked: bool
    error: str = ""
    differences: list[Difference] = field(default_factory=list)
    corrected: bool = False
    #: How long the whole pass over this instance took, in milliseconds.
    took_ms: int = 0


def _trim(items: list[str]) -> list[str]:
    if len(items) <= MAX_DETAIL_ITEMS:
        return items
    return [*items[:MAX_DETAIL_ITEMS], f"… and {len(items) - MAX_DETAIL_ITEMS} more"]


def diff_rules(expected: list[str], actual: list[str]) -> Difference | None:
    if expected == actual:
        return None
    missing = [rule for rule in expected if rule not in set(actual)]
    extra = [rule for rule in actual if rule not in set(expected)]
    if missing or extra:
        # Counted before the cap. "25 rule(s) missing" on a node that has lost
        # four thousand of them would be a wrong number, not a shortened one.
        summary = f"{len(missing)} rule(s) missing, {len(extra)} unexpected rule(s)"
    else:
        summary = "rules present but in a different order"
    return Difference(
        PayloadKind.rules.value,
        summary,
        {"missing": _trim(missing), "extra": _trim(extra)},
        {"missing": missing, "extra": extra},
    )


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


def diff_settings(
    expected: dict[str, dict[str, Any]], actual: dict[str, dict[str, Any] | None]
) -> Difference | None:
    """Fold every managed section into one difference for the settings payload."""
    details: dict[str, Any] = {}
    unsupported: list[str] = []
    for name, wanted in expected.items():
        found = section_differences(name, wanted, actual.get(name))
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


async def _count_again(
    session: AsyncSession, instance_id: int, kind: str, summary: str, details: str, took_ms: int
) -> bool:
    """Record another sighting on the newest matching entry, if there is one.

    A refusal repeats by definition: the node goes on not keeping the same thing,
    so every run would write the same entry. One says it; five hundred bury it.
    Only refusals are held back this way — an out-of-band change that keeps being
    made and corrected is genuinely new each time and stays in the log.

    Held back is not the same as thrown away, though, and it used to be. The row
    that stands is the *first* sighting, so its timestamp answers "since when"
    and nothing answered "is this still happening" or "how often" — a fault four
    hours old and one resolved four hours ago looked identical. The count and the
    last sighting are now carried on that row, and the duration is refreshed to
    the latest attempt's.

    Returns whether an entry was found and updated, in which case the caller adds
    no new row.
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
    if row is None or row.summary != summary or row.details != details:
        return False
    row.occurrences += 1
    row.last_seen_at = utcnow()
    row.took_ms = took_ms
    return True


async def _correct(
    session: AsyncSession,
    adapter: Any,
    correctable: list[Difference],
    expected_sections: dict[str, dict[str, Any]],
    fixed: set[str],
    refused: dict[str, Difference],
    failed: dict[str, str],
    took: dict[str, int],
) -> None:
    """Push each correctable difference and sort it into fixed, refused or failed."""
    for difference in correctable:
        # Timed around both calls, and recorded even when they raise: a push that
        # ends in a timeout is exactly the one whose duration says what happened.
        # Ten seconds on the nose next to "read timeout" names the setting that
        # caused it; the same sentence without a number sent us looking for weeks.
        started = time.perf_counter()
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
            took[difference.payload_kind] = _ms_since(started)
            failed[difference.payload_kind] = str(exc)
            continue
        took[difference.payload_kind] = _ms_since(started)
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

    began = time.perf_counter()
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
        pull_started = time.perf_counter()
        try:
            state = await adapter.pull_state(tuple(expected_sections))
        except (AdapterError, ValueError) as exc:
            report.error = str(exc)
            report.took_ms = _ms_since(began)
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

        pull_ms = _ms_since(pull_started)
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
        # How long each correction attempt took, by payload kind.
        took: dict[str, int] = {}
        if correctable and apply_fixes:
            # A correction is a full-state push like any other, and races an edit's
            # push to the same node the same way — see sync.push_lock. Held around
            # the read-back too, so what is verified is what this pass wrote.
            async with push_lock(instance.id):
                await _correct(
                    session, adapter, correctable, expected_sections, fixed, refused, failed, took
                )
            # True only if something actually landed. Claiming a correction that
            # did not stick is what made this invisible for as long as it was.
            report.corrected = bool(fixed)
            if fixed:
                instance.last_synced_at = utcnow()
            if failed:
                report.error = "; ".join(f"{kind}: {text}" for kind, text in sorted(failed.items()))


    report.took_ms = _ms_since(began)

    # The timings go in the line rather than only in the drift row, because the
    # application log is where you are already looking when the hub feels slow —
    # and because a pass that found nothing has no drift row to carry them.
    timing = f"pull {human_ms(pull_ms)}, pass {human_ms(report.took_ms)}"
    if took:
        timing += ", " + ", ".join(f"{kind} {human_ms(ms)}" for kind, ms in sorted(took.items()))

    if correctable:
        logger.info(
            "Reconcile %s: %s%s [%s]",
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
            timing,
        )
    else:
        logger.debug("Reconcile %s: no differences [%s]", instance.name, timing)

    logged: list[Difference] = []
    for difference in report.differences:
        if not is_correctable(difference):
            # A section this AdGuard build does not implement is a standing capability
            # gap, not drift. Logging it would append the same entry on every run.
            continue
        remaining = refused.get(difference.payload_kind)
        error = failed.get(difference.payload_kind)
        details = json.dumps(difference.details, default=str)
        archived = difference.archived()
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
            archived = remaining.archived()
        attempt_ms = took.get(difference.payload_kind, 0)

        # Archived before the fold below, and that ordering is the point: the
        # database keeps one row per finding so the live view stays readable,
        # while the archive keeps one line per pass so the sequence survives —
        # every sighting, in order, with the rule lists untrimmed. Also before
        # the 500-row cap and before *Clear log*, neither of which reaches a file.
        driftarchive.record(
            {
                "at": utcnow().isoformat().replace("+00:00", "Z"),
                "instance": instance.name,
                "payload_kind": difference.payload_kind,
                "summary": summary,
                "corrected": difference.payload_kind in fixed,
                "took_ms": attempt_ms,
                "details": archived,
            }
        )

        # A refusal and a failing push both repeat on every run by definition, so
        # each is stated once and again when it changes — counted on the standing
        # row rather than repeated under it. A plain difference is not suppressed:
        # it is expected to be corrected, and a second one means the correction is
        # not holding.
        if (error or remaining is not None) and await _count_again(
            session, instance.id, difference.payload_kind, summary, details, attempt_ms
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
                took_ms=attempt_ms,
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


async def record_pass(
    session: AsyncSession, reports: list[InstanceReport], took_ms: int
) -> ReconcileRun:
    """Fold this pass onto the standing streak, or start a new one.

    Consecutive passes with the same outcome share a row and a counter. A
    healthy hub therefore keeps one row saying "two nodes, nothing to correct,
    4,032 passes since 15 August" rather than three hundred rows a day saying
    nothing happened — and that one row is what tells a quiet drift log apart
    from a reconciler that stopped weeks ago.
    """
    shape = {
        "instances": len(reports),
        "unreachable": sum(1 for item in reports if not item.checked),
        "with_differences": sum(1 for item in reports if item.differences),
        "corrected": sum(1 for item in reports if item.corrected),
        "out_of_sync": sum(1 for item in reports if item.error and item.checked),
    }
    standing = (
        (await session.execute(select(ReconcileRun).order_by(ReconcileRun.id.desc()).limit(1)))
        .scalars()
        .first()
    )
    if standing is not None and all(
        getattr(standing, key) == value for key, value in shape.items()
    ):
        standing.passes += 1
        standing.last_at = utcnow()
        standing.last_took_ms = took_ms
        # The worst of the streak, not the mean: a pass that usually takes 80 ms
        # and once took nine seconds is a node that was nearly unreachable, and
        # an average is exactly the statistic that hides it.
        standing.max_took_ms = max(standing.max_took_ms, took_ms)
        run = standing
    else:
        run = ReconcileRun(last_took_ms=took_ms, max_took_ms=took_ms, **shape)
        session.add(run)
    await session.commit()
    await prune_reconcile_runs(session)
    return run


async def reconcile_all(session: AsyncSession, *, apply_fixes: bool = True) -> list[InstanceReport]:
    """Compare every enabled instance against the central state.

    Unless there is no central state. A hub holding nothing has an empty desired
    state, and since every push is full state, reconciliation would read that as
    "these nodes should hold nothing" and clear them — rules, subscriptions and
    all — every five minutes for as long as it ran. The gate is here rather than
    in the push path on purpose: see ``sync.has_desired_state``.
    """
    global _said_nothing_to_replicate

    if not await has_desired_state(session):
        logger.log(
            logging.DEBUG if _said_nothing_to_replicate else logging.WARNING,
            "Reconciliation skipped: %s",
            NOTHING_TO_REPLICATE,
        )
        _said_nothing_to_replicate = True
        # No pass is recorded either. A skipped pass compared nothing, and folding
        # it into the streak would let "nothing to correct" — the one phrase that
        # table exists to be trusted about — mean "nothing was looked at".
        return []
    _said_nothing_to_replicate = False

    result = await session.execute(
        select(Instance).where(Instance.enabled.is_(True)).order_by(Instance.id.asc())
    )
    began = time.perf_counter()
    reports = []
    for instance in result.scalars().all():
        reports.append(await reconcile_instance(session, instance, apply_fixes=apply_fixes))

    # A dry run is deliberately not recorded. It attempted nothing, so folding it
    # into the streak would let "nothing to correct" mean "nothing was tried" —
    # and this table exists precisely to be trusted about whether the safety net
    # is running.
    if apply_fixes:
        await record_pass(session, reports, _ms_since(began))
    # At DEBUG because it runs on a timer: the answer to "did it run at all" has
    # to exist somewhere, and it must not be in everyone's log every five minutes.
    logger.debug(
        "Reconcile pass over %d instance(s), %d unreachable, %d with differences, %s",
        len(reports),
        sum(1 for item in reports if not item.checked),
        sum(1 for item in reports if item.differences),
        human_ms(_ms_since(began)),
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
            # A deadline, because supervision cannot see a pass that hangs. A
            # worker that *ends* is logged and restarted; a coroutine waiting for
            # something that never comes simply never ends, so there is nothing
            # to notice and nothing to restart. It would sit there for weeks
            # while every page in the hub rendered perfectly.
            #
            # Nothing inside a pass is known to hang today — the adapter's
            # timeout covers connect, read, write and pool, and both push locks
            # are context-managed, so they release even on cancellation. This is
            # the backstop for the one that is not known, and the shape of fault
            # it catches is the shape that is hardest to find without it.
            async with asyncio.timeout(pass_deadline(settings.reconcile_interval)):
                async with session_scope() as session:
                    await reconcile_all(session)
        except TimeoutError:
            # Not `Exception` above: asyncio.timeout raises TimeoutError, and
            # letting the generic handler log it as "failed" would hide the one
            # thing worth knowing — that it did not fail, it never came back.
            logger.error(
                "Reconciliation pass exceeded %s and was abandoned; the next one runs on "
                "schedule. A pass that cannot finish within its own deadline usually means a "
                "node accepting a connection and never answering.",
                human_ms(int(pass_deadline(settings.reconcile_interval) * 1000)),
            )
        except Exception:
            logger.exception("Reconciliation pass failed")
