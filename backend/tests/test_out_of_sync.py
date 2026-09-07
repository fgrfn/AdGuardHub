"""A node can answer every request and hold none of them.

Until this existed the interface had two words for a node — online or
unreachable — and one that answered every probe while refusing every correction
was called *online*, correctly and uselessly. One sat that way for four hours,
twenty subscriptions missing, the same failure every five minutes, with nothing
anywhere showing amber: the drift log had it if you went looking, and the single
notification was suppressed as a repeat after the first.

So the state is recorded on the instance, and these are the four things that
have to hold: it appears when a correction does not stick, it survives the runs
that follow, it says when it *started* rather than when it was last seen, and it
clears the moment the node comes back into line.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import Instance
from app.services.sync import drain_background

from .fakes import FakeAdapter
from .test_sync import A, add_instance


async def state_of(name: str = "a") -> Instance:
    async with session_scope() as session:
        return (
            await session.execute(select(Instance).where(Instance.name == name))
        ).scalars().one()


async def reconcile(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/reconcile")).status_code == 200


async def test_a_node_that_keeps_what_it_is_given_is_never_marked(
    auth_client: httpx.AsyncClient,
) -> None:
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    await reconcile(auth_client)

    assert (await state_of()).out_of_sync_since is None


async def test_drift_that_is_corrected_is_not_out_of_sync(
    auth_client: httpx.AsyncClient,
) -> None:
    """Reconciliation doing its job is not a fault to report.

    Something changed the node out of band, the hub put it back. Flagging that
    would light the dashboard up for the case the safety net exists to handle.
    """
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = []  # someone edited the node directly

    await reconcile(auth_client)

    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]
    assert (await state_of()).out_of_sync_since is None


async def test_a_refused_correction_marks_the_node(
    auth_client: httpx.AsyncClient,
) -> None:
    """The reported case: the node takes the push and does not keep it."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()

    await reconcile(auth_client)

    assert (await state_of()).out_of_sync_since is not None


async def test_the_mark_says_when_it_started_not_when_it_was_last_seen(
    auth_client: httpx.AsyncClient,
) -> None:
    """"For four hours" is the number that matters, and re-stamping loses it."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()

    await reconcile(auth_client)
    first = (await state_of()).out_of_sync_since
    await reconcile(auth_client)
    await reconcile(auth_client)

    assert (await state_of()).out_of_sync_since == first


async def test_the_mark_clears_when_the_node_comes_back_into_line(
    auth_client: httpx.AsyncClient,
) -> None:
    """A state that only ever switches on is a state nobody trusts."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    await reconcile(auth_client)
    assert (await state_of()).out_of_sync_since is not None

    FakeAdapter.state_for(A).refuses = set()  # whatever it was, it stopped
    await reconcile(auth_client)

    assert (await state_of()).out_of_sync_since is None


async def test_a_dry_run_neither_sets_nor_clears_it(
    auth_client: httpx.AsyncClient,
) -> None:
    """Nothing was attempted, so it has no standing to say whether one held."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()

    assert (
        await auth_client.post("/api/reconcile?apply_fixes=false")
    ).status_code == 200

    assert (await state_of()).out_of_sync_since is None


async def test_the_api_serves_it(auth_client: httpx.AsyncClient) -> None:
    """The interface cannot show what the API does not send."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    await reconcile(auth_client)

    card = (await auth_client.get("/api/instances")).json()[0]

    assert card["out_of_sync_since"] is not None
    # And it is still online: it answered everything it was asked.
    assert card["status"] == "online"
