"""Whether the safety net is running, which the drift log cannot say.

A reconciliation pass that finds nothing writes nothing — rightly, because two
nodes on a five-minute timer would otherwise put several hundred rows a day into
a table nobody would then read. The cost is that an empty drift log means either
a healthy fleet or a reconciler that stopped weeks ago, and nothing anywhere
told those two apart.

So every pass is recorded, including the quiet ones — but folded: consecutive
passes with the same outcome share a row and a counter. These pin both halves,
because either alone is useless. Folding without recording says nothing; and
recording without folding is the three hundred rows a day that made the drift
log stay quiet in the first place.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import ReconcileRun
from app.services.sync import drain_background

from .fakes import FakeAdapter
from .test_sync import A, B, add_instance


async def runs() -> list[ReconcileRun]:
    async with session_scope() as session:
        result = await session.execute(select(ReconcileRun).order_by(ReconcileRun.id.asc()))
        return list(result.scalars().all())


async def reconcile(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/reconcile")).status_code == 200


async def test_a_quiet_pass_is_recorded_even_though_it_logs_no_drift(
    auth_client: httpx.AsyncClient,
) -> None:
    """The whole point. An empty drift log is now a provable state, not an ambiguous one."""
    await add_instance(auth_client, "a", A)
    await drain_background()

    await reconcile(auth_client)

    assert (await auth_client.get("/api/drift")).json() == []
    recorded = await runs()
    assert len(recorded) == 1
    assert recorded[0].instances == 1
    assert recorded[0].with_differences == 0


async def test_quiet_passes_fold_onto_one_row(auth_client: httpx.AsyncClient) -> None:
    """Three hundred rows a day saying nothing happened is how a table stops being read."""
    await add_instance(auth_client, "a", A)
    await drain_background()

    for _ in range(5):
        await reconcile(auth_client)

    recorded = await runs()
    assert len(recorded) == 1
    assert recorded[0].passes == 5


async def test_the_row_says_since_when_and_whether_it_is_still_running(
    auth_client: httpx.AsyncClient,
) -> None:
    """Two questions, two timestamps. started_at must not move."""
    await add_instance(auth_client, "a", A)
    await drain_background()

    await reconcile(auth_client)
    began = (await runs())[0].started_at
    await reconcile(auth_client)

    row = (await runs())[0]
    assert row.started_at == began
    assert row.last_at >= began


async def test_an_outcome_that_changes_starts_a_new_row(
    auth_client: httpx.AsyncClient,
) -> None:
    """The table is the history of what changed, not a tape of what did not."""
    await add_instance(auth_client, "a", A)
    await drain_background()
    await reconcile(auth_client)

    FakeAdapter.state_for(A).rules = ["||somebody-edited-the-node^"]
    await reconcile(auth_client)

    recorded = await runs()
    assert len(recorded) == 2
    assert recorded[0].with_differences == 0
    assert recorded[1].with_differences == 1


async def test_a_node_going_unreachable_starts_a_new_row(
    auth_client: httpx.AsyncClient,
) -> None:
    """The state changed, so the streak did — even though nothing drifted."""
    await add_instance(auth_client, "a", A)
    await drain_background()
    await reconcile(auth_client)

    FakeAdapter.state_for(A).offline = True
    await reconcile(auth_client)

    recorded = await runs()
    assert len(recorded) == 2
    assert recorded[-1].unreachable == 1


async def test_the_worst_pass_of_a_streak_is_kept_not_the_average(
    auth_client: httpx.AsyncClient,
) -> None:
    """A pass that usually takes 80 ms and once took nine seconds is a node that
    was nearly unreachable, and a mean is exactly the statistic that hides it."""
    await add_instance(auth_client, "a", A)
    await drain_background()
    await reconcile(auth_client)

    async with session_scope() as session:
        row = (await session.execute(select(ReconcileRun))).scalars().one()
        row.max_took_ms = 9000
        row.last_took_ms = 9000
        await session.commit()

    await reconcile(auth_client)

    row = (await runs())[0]
    assert row.max_took_ms == 9000  # the outlier survives the passes after it
    assert row.last_took_ms < 9000  # while the latest is the latest


async def test_a_dry_run_is_not_recorded(auth_client: httpx.AsyncClient) -> None:
    """It attempted nothing, so folding it in would let "nothing to correct"
    mean "nothing was tried" — in the one table built to be trusted about
    whether the safety net is running."""
    await add_instance(auth_client, "a", A)
    await drain_background()

    assert (await auth_client.post("/api/reconcile?apply_fixes=false")).status_code == 200

    assert await runs() == []


async def test_the_api_serves_them_newest_first(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    await drain_background()
    await reconcile(auth_client)
    FakeAdapter.state_for(A).offline = True
    await reconcile(auth_client)

    body = (await auth_client.get("/api/reconcile/runs")).json()

    assert len(body) == 2
    assert body[0]["unreachable"] == 1
    assert body[1]["unreachable"] == 0
    assert body[0]["instances"] == 2
