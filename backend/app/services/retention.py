"""Caps on the tables that only ever grow.

The version history has been capped since it was written. The two operational
logs beside it were not: an applied push job is never deleted, and a drift event
is never deleted, so both tables grow for as long as the hub runs.

Neither grows fast — they only gain rows when something goes wrong, so a healthy
hub barely accumulates any. A node that flaps for a few months is a different
story, and nothing about the design would ever stop it.

Both caps are 500, which is what the API will serve in one request at most
(``limit`` is capped at 500 on /api/drift and /api/jobs). Keeping more rows than
can ever be read back would be storage spent on something nobody can look at.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DriftEvent, JobStatus, PushJob, ReconcileRun

MAX_DRIFT_EVENTS = 500
MAX_APPLIED_JOBS = 500

# Reconciliation streaks, not passes: consecutive passes with the same outcome
# share a row, so a healthy hub adds one row a month rather than three hundred a
# day. 500 of them is a long history of everything that actually changed — which
# is why this cap is the same number without meaning the same thing.
MAX_RECONCILE_RUNS = 500


async def _prune(session: AsyncSession, model: type, cap: int, *conditions) -> int:
    """Delete the oldest rows beyond ``cap``, counting only rows matching ``conditions``.

    The conditions matter as much as the cap: for push jobs they confine both the
    count and the delete to finished rows, so a queue full of pending retries can
    never be pruned away no matter how long it is.
    """
    counted = await session.execute(
        select(func.count()).select_from(model).where(*conditions)
    )
    total = int(counted.scalar_one())
    if total <= cap:
        return 0

    cutoff = (
        await session.execute(
            select(model.id).where(*conditions).order_by(model.id.desc()).offset(cap).limit(1)
        )
    ).scalar_one_or_none()
    if cutoff is None:
        return 0

    await session.execute(delete(model).where(model.id <= cutoff, *conditions))
    await session.commit()
    return total - cap


async def prune_drift_events(session: AsyncSession) -> int:
    return await _prune(session, DriftEvent, MAX_DRIFT_EVENTS)


async def prune_reconcile_runs(session: AsyncSession) -> int:
    return await _prune(session, ReconcileRun, MAX_RECONCILE_RUNS)


async def prune_applied_jobs(session: AsyncSession) -> int:
    """Trim the record of pushes that landed. The retry queue is left alone.

    A pending or failed job is work still owed to an instance; only an applied
    one is history, and history is the only thing safe to forget.
    """
    return await _prune(
        session, PushJob, MAX_APPLIED_JOBS, PushJob.status == JobStatus.applied.value
    )
