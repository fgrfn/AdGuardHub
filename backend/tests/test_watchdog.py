"""Nothing used to report that the safety net had stopped.

Reconciliation is what catches a node that drifted, and the only thing in the
hub that knew whether it was still running was a dashboard card — a panel
somebody has to be looking at. Supervision covers a worker that *ends*; it
cannot cover one that hangs, or one switched off months ago and forgotten.

What these pin is mostly the *negative* half, because a watchdog that cries wolf
is worse than none: three states where silence is correct must not be reported
as a fault, and each of them is a state a real hub spends time in.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from app.db import session_scope
from app.models import ReconcileRun, utcnow
from app.services import hubsettings
from app.services.sync import drain_background
from app.services.watchdog import STALE_AFTER_INTERVALS, reconcile_health

from .test_sync import A, add_instance


async def health() -> dict:
    async with session_scope() as session:
        return await reconcile_health(session)


async def age_the_last_pass(seconds: int) -> None:
    """Move the standing streak's last pass into the past."""
    async with session_scope() as session:
        newest = ReconcileRun.__table__.select().order_by(ReconcileRun.id.desc())
        row = (await session.execute(newest)).first()
        assert row is not None, "no pass recorded, so this test proves nothing"
        await session.execute(
            ReconcileRun.__table__.update()
            .where(ReconcileRun.id == row.id)
            .values(last_at=utcnow().replace(tzinfo=None) - timedelta(seconds=seconds))
        )
        await session.commit()


async def configured(client: httpx.AsyncClient) -> None:
    await add_instance(client, "a", A)
    assert (
        await client.post("/api/rules", json={"text": "||ads.example.com^"})
    ).status_code == 201
    await drain_background()
    assert (await client.post("/api/reconcile")).status_code == 200


async def test_a_reconciler_that_just_ran_is_not_stalled(auth_client: httpx.AsyncClient) -> None:
    await configured(auth_client)

    state = await health()
    assert state["stalled"] is False
    assert state["last_pass_age_s"] < 60


async def test_a_reconciler_that_has_gone_quiet_is_reported(
    auth_client: httpx.AsyncClient,
) -> None:
    """The fault itself: passes stopped, and every page still renders fine."""
    await configured(auth_client)
    interval = hubsettings.current().reconcile_interval
    await age_the_last_pass(interval * (STALE_AFTER_INTERVALS + 1))

    state = await health()
    assert state["stalled"] is True
    assert state["overdue_after_s"] == interval * STALE_AFTER_INTERVALS


async def test_one_late_pass_is_not_a_fault(auth_client: httpx.AsyncClient) -> None:
    """A slow node, a restart, or a pass that ran long. An alarm for those is muted."""
    await configured(auth_client)
    await age_the_last_pass(hubsettings.current().reconcile_interval + 30)

    assert (await health())["stalled"] is False


async def test_a_reconciler_switched_off_is_a_decision_not_a_fault(
    auth_client: httpx.AsyncClient,
) -> None:
    await configured(auth_client)
    await age_the_last_pass(86_400)
    await auth_client.put("/api/settings/hub", json={"reconcile_enabled": False})

    state = await health()
    assert state["enabled"] is False
    assert state["stalled"] is False


async def test_a_hub_with_nothing_to_replicate_is_not_stalled(
    auth_client: httpx.AsyncClient,
) -> None:
    """It deliberately does not reconcile at all — see sync.has_desired_state.

    Without this the onboarding wizard would raise an alarm about a hub nobody
    had finished setting up yet.
    """
    await add_instance(auth_client, "a", A)

    state = await health()
    assert state["replicating"] is False
    assert state["stalled"] is False


async def test_a_hub_that_has_never_run_a_pass_is_not_stalled(
    auth_client: httpx.AsyncClient,
) -> None:
    """A hub that started a minute ago has not missed anything yet."""
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    state = await health()
    assert state["last_pass_age_s"] is None
    assert state["stalled"] is False


async def test_health_carries_it_without_a_session(client: httpx.AsyncClient) -> None:
    """An external monitor is the one observer that needs nobody looking at a page."""
    body = (await client.get("/api/health")).json()

    # The container's own start-up check greps for this exact field.
    assert body["status"] == "ok"
    assert body["reconcile"]["stalled"] is False
    assert "last_pass_age_s" in body["reconcile"]
