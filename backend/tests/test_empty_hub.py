"""A hub with nothing in it must not replicate nothing onto a working node.

This is the worst failure the design has produced, and it is not hypothetical: a
second AdGuardHub, spun up to try something out and left standing on step 1 of
the onboarding wizard with a production node already entered, emptied that node
of all 24 rules and all 20 subscriptions every five minutes for days. Nothing was
misconfigured. The hub did exactly what it is built to do — push full state — and
its full state was nothing.

So the rule these tests hold: reconciliation compares against the central state,
and where there is no central state there is nothing to compare against. The push
path is deliberately *not* gated the same way, because deleting the last rule
produces an empty desired state on purpose and that deletion has to reach the
nodes.
"""

from __future__ import annotations

import httpx

from app.adapters.base import RemoteFilterList
from app.db import session_scope
from app.services.reconcile import reconcile_all
from app.services.sync import drain_background, has_desired_state

from .fakes import FakeAdapter
from .test_sync import A, add_instance

LIST = RemoteFilterList("EasyList", "https://example.com/easylist.txt", True, "blocklist")


def furnish(url: str) -> None:
    """Give a node the state a real one would have before the hub ever saw it."""
    state = FakeAdapter.state_for(url)
    state.rules = ["@@||doorbell.example.com^", "||ads.example.com^"]
    state.filter_lists = [LIST]


async def reconcile_as_the_timer_does() -> list:
    """What ``reconcile_worker`` runs, bypassing the API's own refusal."""
    async with session_scope() as session:
        return await reconcile_all(session)


async def test_an_unconfigured_hub_leaves_a_configured_node_alone(
    auth_client: httpx.AsyncClient,
) -> None:
    """The reported case, end to end."""
    await add_instance(auth_client, "a", A)
    furnish(A)
    await drain_background()

    for _ in range(3):
        assert await reconcile_as_the_timer_does() == []

    state = FakeAdapter.state_for(A)
    assert state.rules == ["@@||doorbell.example.com^", "||ads.example.com^"]
    assert state.filter_lists == [LIST]


async def test_a_skipped_pass_is_not_recorded_as_a_pass(
    auth_client: httpx.AsyncClient,
) -> None:
    """Otherwise the one table built to be trusted starts lying.

    "Nothing to correct" has to keep meaning "everything was compared and
    matched". A skipped pass compared nothing, and folding it in would make the
    dashboard reassuring about precisely the state that needs attention.
    """
    await add_instance(auth_client, "a", A)
    furnish(A)
    await reconcile_as_the_timer_does()

    assert (await auth_client.get("/api/reconcile/runs")).json() == []


async def test_a_manual_pass_is_refused_rather_than_answered_with_nothing(
    auth_client: httpx.AsyncClient,
) -> None:
    """An empty report list renders as "no drift found", which is the wrong news."""
    await add_instance(auth_client, "a", A)
    furnish(A)

    response = await auth_client.post("/api/reconcile")
    assert response.status_code == 409
    assert "nothing to reconcile" in response.json()["detail"]


async def test_the_dashboard_says_the_hub_is_not_replicating_yet(
    auth_client: httpx.AsyncClient,
) -> None:
    await add_instance(auth_client, "a", A)
    assert (await auth_client.get("/api/dashboard")).json()["replicating"] is False

    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    assert (await auth_client.get("/api/dashboard")).json()["replicating"] is True


async def test_the_first_rule_starts_reconciliation(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    furnish(A)
    await auth_client.post("/api/rules", json={"text": "||tracker.example.com^"})
    await drain_background()

    reports = await reconcile_as_the_timer_does()
    assert len(reports) == 1
    # And it now does its job: the node's out-of-band rules are the hub's to own.
    assert FakeAdapter.state_for(A).rules == ["||tracker.example.com^"]


async def test_deleting_the_last_rule_still_reaches_the_nodes(
    auth_client: httpx.AsyncClient,
) -> None:
    """The trap in gating the push path as well.

    Removing the final rule leaves the hub with an empty desired state, which is
    exactly the shape the gate above refuses to act on. If the push honoured that
    gate too, the one deletion the operator most certainly meant would be the one
    that never propagated — and it would fail silently, since the hub would
    consider the work done.
    """
    await add_instance(auth_client, "a", A)
    created = await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]

    deleted = await auth_client.delete(f"/api/rules/{created.json()['id']}")
    assert deleted.status_code == 204
    await drain_background()

    assert FakeAdapter.state_for(A).rules == []


async def test_a_rule_switched_off_still_counts_as_a_configured_hub(
    auth_client: httpx.AsyncClient,
) -> None:
    """Switching every rule off is a decision, and it has to keep being enforced.

    Counting only *enabled* rules would stop reconciliation the moment the last
    one was disabled — leaving the nodes holding rules the operator had just
    turned off, with the safety net quietly standing down.
    """
    await add_instance(auth_client, "a", A)
    created = await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await auth_client.patch(f"/api/rules/{created.json()['id']}", json={"enabled": False})
    await drain_background()

    async with session_scope() as session:
        assert await has_desired_state(session) is True

    FakeAdapter.state_for(A).rules = ["||ads.example.com^"]
    assert len(await reconcile_as_the_timer_does()) == 1
    assert FakeAdapter.state_for(A).rules == []


async def test_a_managed_section_with_nothing_in_it_is_not_state(
    auth_client: httpx.AsyncClient,
) -> None:
    """Switched on but never imported pushes nothing, so it seeds nothing either.

    The onboarding wizard turns sections on before the master import fills them.
    Treating that intermediate step as "configured" would put the gate back on
    the wrong side of the very screen this bug was found behind.
    """
    await add_instance(auth_client, "a", A)
    await auth_client.patch("/api/config/sections/dns", json={"managed": True})
    furnish(A)

    assert await reconcile_as_the_timer_does() == []
    assert FakeAdapter.state_for(A).rules == ["@@||doorbell.example.com^", "||ads.example.com^"]

    await auth_client.patch(
        "/api/config/sections/dns", json={"managed": True, "data": {"upstream_dns": ["1.1.1.1"]}}
    )
    assert len(await reconcile_as_the_timer_does()) == 1
