"""Telling somebody when the safety net has gone quiet.

Reconciliation is what catches a node that drifted, and nothing was watching
*it*. The only thing in the hub that knew whether it was still running was the
*Reconciliation* card — a panel somebody has to be looking at, on a dashboard
nobody keeps open. A reconciler that stopped on a Saturday afternoon would be
found on Monday, by luck.

Supervision (``services/supervisor``) covers a worker that *ends*: it is logged
and restarted. It cannot cover a worker that hangs, because a coroutine waiting
forever never ends, and it cannot cover a reconciler switched off months ago and
forgotten. Those stay silent, and this is what breaks that silence — from
outside, because the reconciler cannot be the thing that reports its own failure.

Two ways out, deliberately both:

* ``/api/health`` carries the same answer, so an external monitor sees it without
  the hub having to reach anything. That matters precisely when the hub is the
  part that is unwell.
* A notification fires once when it goes quiet and once when it comes back, to
  whatever targets the operator already has.

The rule lives here and both read it, rather than each deciding for itself. The
dashboard card keeps its own wording — it has states these do not, like "switched
off" and "nothing to replicate yet" — but the threshold below is the one it uses.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import session_scope
from ..models import ReconcileRun, utcnow
from . import hubsettings
from .notify import EVENT_RECONCILE_RESUMED, EVENT_RECONCILE_STALLED, notify
from .sync import has_desired_state

logger = logging.getLogger(__name__)

#: How many intervals may pass before the timer is presumed stopped. Three
#: rather than one, and for the reason the dashboard uses the same number: a
#: single late pass is a slow node, a restart, or a pass that ran long, and an
#: alarm that fires for those is one people mute. Kept in step with
#: STALE_AFTER_INTERVALS in frontend/src/reconcileState.ts.
STALE_AFTER_INTERVALS = 3

#: How often the question is asked. Independent of the reconcile interval, which
#: is the thing being measured — a watchdog that slept as long as its subject
#: would report a stall an interval late, and a stalled reconciler is already
#: late by definition.
CHECK_INTERVAL = 60.0


async def reconcile_health(session: AsyncSession) -> dict[str, object]:
    """Whether reconciliation is doing its job, as a fact rather than a verdict.

    Three things have to be true before silence counts as a fault, and each of
    them is a state where silence is *correct*:

    * it is switched on — off is a decision, not a failure;
    * the hub has something to replicate — an empty hub deliberately does not
      reconcile at all (see ``sync.has_desired_state``);
    * a pass has been recorded before — a hub that started a minute ago has not
      missed anything yet, and reporting one as stalled would make every restart
      an incident.
    """
    settings = hubsettings.current()
    overdue_after = settings.reconcile_interval * STALE_AFTER_INTERVALS

    latest = (
        (await session.execute(select(ReconcileRun).order_by(ReconcileRun.id.desc()).limit(1)))
        .scalars()
        .first()
    )
    seeded = await has_desired_state(session)

    age: int | None = None
    if latest is not None:
        # Stored naive by SQLite even though it was written aware; compared
        # against a naive now so the two are the same kind of thing.
        age = max(0, int((utcnow().replace(tzinfo=None) - latest.last_at).total_seconds()))

    stalled = bool(
        settings.reconcile_enabled and seeded and age is not None and age > overdue_after
    )
    return {
        "enabled": settings.reconcile_enabled,
        "replicating": seeded,
        "last_pass_age_s": age,
        "overdue_after_s": overdue_after,
        "stalled": stalled,
    }


async def watchdog_worker(stop: asyncio.Event) -> None:  # pragma: no cover - background loop
    """Report the reconciler going quiet, once, and its return, once.

    Edge-triggered on purpose. A stall is true on every check by definition, so
    notifying per check would send a message a minute for as long as it lasted —
    which is how a notifier stops being read, and this one has exactly one thing
    to say.
    """
    reported = False
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=CHECK_INTERVAL)
            return
        except TimeoutError:
            pass
        try:
            async with session_scope() as session:
                health = await reconcile_health(session)
        except Exception:
            logger.exception("Reconciliation watchdog check failed")
            continue

        if health["stalled"] and not reported:
            reported = True
            age = int(health["last_pass_age_s"] or 0)
            logger.error(
                "Reconciliation has not completed a pass in %d s (limit %s s)",
                age,
                health["overdue_after_s"],
            )
            await notify(
                EVENT_RECONCILE_STALLED,
                "Reconciliation has stopped",
                f"No reconciliation pass has completed in {age // 60} minute(s). Drift on your "
                "instances is not being detected or corrected. Changes you make are still "
                "pushed immediately.",
            )
        elif not health["stalled"] and reported:
            reported = False
            logger.info("Reconciliation is running again")
            await notify(
                EVENT_RECONCILE_RESUMED,
                "Reconciliation is running again",
                "A pass has completed, so drift is being detected and corrected as usual.",
            )
