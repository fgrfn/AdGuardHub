"""The last place an empty hub could still erase a node.

Reconciliation stopped doing this in #106: a hub holding nothing has an empty
desired state, and since every push is full state, replicating it means "these
nodes should hold nothing". The instant push was deliberately left ungated —
deleting your last rule *produces* an empty desired state and that deletion has
to reach the nodes — and so was this button, on the grounds that somebody
pressing it is not the same as a timer acting alone.

That grounds it too far. Pressing a button is not the same as *meaning* this:
the other things this button does are safe and routine and none of them warns,
so an operator whose hub is empty for a reason they have not noticed — a fresh
install pointed at a working node, a restored database — cannot tell this press
apart from those.

So it asks, and the question counts what would actually go.
"""

from __future__ import annotations

import httpx

from app.adapters.base import RemoteFilterList
from app.services.sync import drain_background

from .fakes import FakeAdapter
from .test_sync import A, add_instance

LIST = RemoteFilterList("EasyList", "https://e.com/l.txt", True, "blocklist")


def furnish(url: str) -> None:
    state = FakeAdapter.state_for(url)
    state.rules = ["@@||doorbell.example.com^", "||ads.example.com^"]
    state.filter_lists = [LIST]


async def test_an_empty_hub_asks_before_erasing_a_node(auth_client: httpx.AsyncClient) -> None:
    node_id = await add_instance(auth_client, "a", A)
    furnish(A)

    response = await auth_client.post(f"/api/instances/{node_id}/push")

    assert response.status_code == 409
    detail = response.json()["detail"]
    # The counts are the point: a generality ("this may delete data") is the kind
    # of warning people click through.
    assert "2 rule(s)" in detail
    assert "1 subscription(s)" in detail
    assert FakeAdapter.state_for(A).rules == ["@@||doorbell.example.com^", "||ads.example.com^"]


async def test_confirming_goes_through(auth_client: httpx.AsyncClient) -> None:
    """It is still the operator's call — the point is that it is now a call."""
    node_id = await add_instance(auth_client, "a", A)
    furnish(A)

    response = await auth_client.post(f"/api/instances/{node_id}/push?confirm=true")

    assert response.status_code == 200
    assert FakeAdapter.state_for(A).rules == []
    assert FakeAdapter.state_for(A).filter_lists == []


async def test_a_configured_hub_never_asks(auth_client: httpx.AsyncClient) -> None:
    """The ordinary case, which must stay one press."""
    node_id = await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = ["||somebody-edited-the-node^"]

    response = await auth_client.post(f"/api/instances/{node_id}/push")

    assert response.status_code == 200
    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]


async def test_a_node_with_nothing_to_lose_is_not_worth_a_question(
    auth_client: httpx.AsyncClient,
) -> None:
    """Empty hub, empty node: the push changes nothing, so a warning is noise."""
    node_id = await add_instance(auth_client, "a", A)

    assert (await auth_client.post(f"/api/instances/{node_id}/push")).status_code == 200


async def test_a_node_that_cannot_be_asked_is_not_blocked(
    auth_client: httpx.AsyncClient,
) -> None:
    """The push is about to fail on its own, with its own error.

    Turning "I could not count what you would lose" into a refusal would block
    the operator on the one thing that says nothing about whether they meant it.
    """
    node_id = await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).offline = True

    response = await auth_client.post(f"/api/instances/{node_id}/push")

    assert response.status_code == 200
    assert response.json()["ok"] == "false"
    assert "unreachable" in response.json()["error"]
