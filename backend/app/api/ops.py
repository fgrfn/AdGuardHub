"""Dashboard, retry queue and drift log — the operational surface of the sync engine."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, func, select

from ..deps import CurrentUser, SessionDep
from ..models import (
    ConfigSection,
    ConfigVersion,
    DriftEvent,
    FilterList,
    Instance,
    InstanceStatus,
    JobStatus,
    PushJob,
    ReconcileRun,
    Rule,
    RuleKind,
)
from ..schemas import (
    DashboardStats,
    DriftEventOut,
    PushJobOut,
    ReconcileReportOut,
    ReconcileRunOut,
    SyncResult,
    TrafficOut,
)
from ..services import querylog
from ..services.aggregate import traffic_summary
from ..services.reconcile import NOTHING_TO_REPLICATE, reconcile_all
from ..services.sync import ALL_KINDS, has_desired_state, process_retry_queue, sync_all

router = APIRouter(prefix="/api", tags=["ops"])


async def _count(session: SessionDep, statement) -> int:
    return int((await session.execute(statement)).scalar_one() or 0)


@router.get("/traffic", response_model=TrafficOut)
async def traffic(_: CurrentUser) -> TrafficOut:
    """DNS statistics across the fleet, for the hub's own dashboard.

    Behind the session cookie like every other hub route — the ``/control`` façade's
    switch governs that façade, not what the hub shows its own operator.
    """
    return TrafficOut.model_validate(await traffic_summary())


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard(_: CurrentUser, session: SessionDep) -> DashboardStats:
    def instances_where(*conditions):
        return select(func.count()).select_from(Instance).where(*conditions)

    last_sync = (
        await session.execute(select(func.max(Instance.last_synced_at)))
    ).scalar_one_or_none()

    return DashboardStats(
        # From the same function reconciliation gates on, not recomputed from the
        # counts below: `managed_sections` counts sections that are switched on
        # including ones with nothing imported yet, so deriving it here would let
        # the card claim the safety net is running while the timer skips it.
        replicating=await has_desired_state(session),
        instances_total=await _count(session, select(func.count()).select_from(Instance)),
        last_sync_at=last_sync,
        instances_synced=await _count(
            session, instances_where(Instance.last_synced_at.is_not(None))
        ),
        managed_sections=await _count(
            session,
            select(func.count()).select_from(ConfigSection).where(ConfigSection.managed.is_(True)),
        ),
        versions_total=await _count(session, select(func.count()).select_from(ConfigVersion)),
        instances_online=await _count(
            session, instances_where(Instance.status == InstanceStatus.online.value)
        ),
        instances_unreachable=await _count(
            session, instances_where(Instance.status == InstanceStatus.unreachable.value)
        ),
        instances_disabled=await _count(session, instances_where(Instance.enabled.is_(False))),
        rules_total=await _count(session, select(func.count()).select_from(Rule)),
        rules_allow=await _count(
            session,
            select(func.count()).select_from(Rule).where(Rule.kind == RuleKind.allow.value),
        ),
        rules_block=await _count(
            session,
            select(func.count()).select_from(Rule).where(Rule.kind == RuleKind.block.value),
        ),
        filter_lists_total=await _count(session, select(func.count()).select_from(FilterList)),
        filter_lists_enabled=await _count(
            session,
            select(func.count()).select_from(FilterList).where(FilterList.enabled.is_(True)),
        ),
        pending_jobs=await _count(
            session,
            select(func.count()).select_from(PushJob).where(
                PushJob.status == JobStatus.pending.value
            ),
        ),
        failed_jobs=await _count(
            session,
            select(func.count())
            .select_from(PushJob)
            .where(PushJob.status == JobStatus.failed.value),
        ),
        recent_drift=await _count(session, select(func.count()).select_from(DriftEvent)),
        querylog_buffered=len(querylog.buffer),
    )


@router.post("/sync", response_model=SyncResult)
async def force_sync(_: CurrentUser, session: SessionDep) -> SyncResult:
    """Push the full central state to every enabled instance right now."""
    errors = await sync_all(session, ALL_KINDS, "manual full sync")
    total = await _count(
        session, select(func.count()).select_from(Instance).where(Instance.enabled.is_(True))
    )
    return SyncResult(instances=total, failed=errors)


@router.post("/reconcile", response_model=list[ReconcileReportOut])
async def run_reconcile(
    _: CurrentUser, session: SessionDep, apply_fixes: bool = True
) -> list[ReconcileReportOut]:
    """Run a reconciliation pass now. ``apply_fixes=false`` performs a dry run."""
    # Refused rather than answered with an empty list. Reconciliation skips a hub
    # with nothing to replicate, and an empty list renders as "no drift found —
    # every instance matches", which is both false and reassuring about the one
    # state in which this hub would otherwise have wiped a node.
    if not await has_desired_state(session):
        raise HTTPException(status.HTTP_409_CONFLICT, NOTHING_TO_REPLICATE)
    reports = await reconcile_all(session, apply_fixes=apply_fixes)
    return [
        ReconcileReportOut(
            instance_id=report.instance_id,
            instance_name=report.instance_name,
            checked=report.checked,
            error=report.error,
            corrected=report.corrected,
            differences=[asdict(difference) for difference in report.differences],
            took_ms=report.took_ms,
        )
        for report in reports
    ]


@router.get("/jobs", response_model=list[PushJobOut])
async def list_jobs(
    _: CurrentUser,
    session: SessionDep,
    open_only: bool = True,
    limit: int = Query(100, ge=1, le=500),
) -> list[PushJobOut]:
    statement = (
        select(PushJob, Instance.name)
        .join(Instance, Instance.id == PushJob.instance_id)
        .order_by(PushJob.updated_at.desc())
        .limit(limit)
    )
    if open_only:
        statement = statement.where(PushJob.status != JobStatus.applied.value)
    rows = (await session.execute(statement)).all()
    return [
        PushJobOut(
            id=job.id,
            instance_id=job.instance_id,
            instance_name=name,
            payload_kind=job.payload_kind,
            status=job.status,
            attempts=job.attempts,
            last_error=job.last_error,
            reason=job.reason,
            updated_at=job.updated_at,
        )
        for job, name in rows
    ]


@router.post("/jobs/retry")
async def retry_jobs(_: CurrentUser, session: SessionDep) -> dict[str, int]:
    return {"recovered": await process_retry_queue(session)}


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_job(job_id: int, _: CurrentUser, session: SessionDep) -> None:
    job = await session.get(PushJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    await session.delete(job)
    await session.commit()


@router.get("/drift", response_model=list[DriftEventOut])
async def list_drift(
    _: CurrentUser, session: SessionDep, limit: int = Query(100, ge=1, le=500)
) -> list[DriftEvent]:
    result = await session.execute(
        select(DriftEvent).order_by(DriftEvent.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


@router.get("/reconcile/runs", response_model=list[ReconcileRunOut])
async def list_reconcile_runs(
    _: CurrentUser, session: SessionDep, limit: int = Query(50, ge=1, le=500)
) -> list[ReconcileRun]:
    """What the reconciliation timer has been doing, newest first.

    The drift log answers "what was wrong". It cannot answer "is the safety net
    running at all", because a pass that finds nothing writes nothing — so an
    empty drift log means either a healthy fleet or a reconciler that stopped
    weeks ago. These rows tell those two apart.

    One row is a streak of consecutive passes with the same outcome, not one
    pass, so a healthy hub answers with a single row rather than a tape of three
    hundred a day saying nothing happened.
    """
    result = await session.execute(
        select(ReconcileRun).order_by(ReconcileRun.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


@router.delete("/drift")
async def clear_drift(_: CurrentUser, session: SessionDep) -> dict[str, int]:
    """Empty the drift log.

    Deleting the record of a difference does not resolve it: if a node still
    disagrees with the hub, the next reconciliation run finds it again and writes
    a new entry. This is for the other case — a log full of findings whose cause
    is already gone — where the history is noise rather than evidence.
    """
    deleted = await _count(session, select(func.count()).select_from(DriftEvent))
    await session.execute(delete(DriftEvent))
    await session.commit()
    return {"deleted": deleted}
