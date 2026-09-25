"""Checking a subscription for updates, and seeing when it last arrived.

The hub cannot refresh a list. It holds the URL and whether it is on; AdGuard
downloads and parses the file (spec §12). What the hub *can* do is ask every node
to go and look now rather than waiting out its own interval — and that is a thing
it offered on the AdGuard-compatible surface while doing nothing at all:

    async def refresh_filters(_: ControlUser) -> dict[str, int]:
        \"\"\"The hub tracks subscription URLs, not their contents.\"\"\"
        return {"updated": 0}

A phone remote or a Home Assistant automation pressing *check for updates* got a
success and a zero, and not one node went and looked.

The other half is the quieter fault. AdGuard keeps a subscription it cannot
download rather than dropping it, so a list whose URL has rotted never disappears
— it stops changing, silently, and nothing in the hub said when a list last
arrived. `last_updated` comes back in the same payload the sizes already come
from and was simply not read.
"""

from __future__ import annotations

import httpx

from app.adapters.base import RemoteFilterList
from app.services import filtersizes

from .fakes import FakeAdapter
from .test_sync import A, B, add_instance

FRESH = "2026-09-24T22:00:00Z"
OLDER = "2026-09-01T09:00:00Z"


async def a_subscription(client: httpx.AsyncClient, url: str = "https://e.test/1.txt") -> None:
    response = await client.post(
        "/api/filter-lists", json={"name": "EasyList", "url": url, "kind": "blocklist"}
    )
    assert response.status_code in (200, 201), response.text


def listed(node: str, *, url: str = "https://e.test/1.txt", last_updated: str = "") -> None:
    FakeAdapter.state_for(node).filter_lists = [
        RemoteFilterList("EasyList", url, True, "blocklist", 42, 101, last_updated)
    ]


async def sizes(client: httpx.AsyncClient) -> dict:
    filtersizes.invalidate()
    response = await client.get("/api/filter-lists/sizes")
    assert response.status_code == 200, response.text
    return response.json()["lists"][0]


# --------------------------------------------------------------------------
# Asking the nodes to look now
# --------------------------------------------------------------------------


async def test_every_node_is_asked_for_both_kinds(auth_client: httpx.AsyncClient) -> None:
    """Blocklists and allowlists are separate calls in AdGuard's API.

    Asking for only one would leave an allowlist that never updates — the harder
    of the two to notice, because nothing appears to be broken.
    """
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)

    response = await auth_client.post("/api/filter-lists/refresh")

    assert response.status_code == 200, response.text
    assert FakeAdapter.state_for(A).refresh_calls == [False, True]
    assert FakeAdapter.state_for(B).refresh_calls == [False, True]


async def test_the_headline_is_one_node_not_the_sum(auth_client: httpx.AsyncClient) -> None:
    """These are one set of subscriptions replicated everywhere.

    Two nodes each updating the same three lists is three lists changed, not six.
    """
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    FakeAdapter.state_for(A).refresh_updates = 3
    FakeAdapter.state_for(B).refresh_updates = 3

    body = (await auth_client.post("/api/filter-lists/refresh")).json()

    assert body["updated"] == 6, "both kinds are asked, so one node reports 3 + 3"
    assert [item["updated"] for item in body["instances"]] == [6, 6]


async def test_a_node_that_refuses_is_named_and_the_rest_still_go(
    auth_client: httpx.AsyncClient,
) -> None:
    """Best effort across the fleet, like every other fan-out (spec §6)."""
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    FakeAdapter.state_for(A).offline = True
    FakeAdapter.state_for(B).refresh_updates = 2

    body = (await auth_client.post("/api/filter-lists/refresh")).json()

    failed = {item["instance_name"]: item for item in body["instances"]}
    assert failed["a"]["error"], "the unreachable node has to be named"
    assert failed["b"]["updated"] == 4
    assert FakeAdapter.state_for(B).refresh_calls == [False, True]


async def test_a_node_in_maintenance_is_left_alone(auth_client: httpx.AsyncClient) -> None:
    """Maintenance means leave this one alone, and a refresh is the hub reaching in."""
    instance_id = await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    assert (
        await auth_client.patch(f"/api/instances/{instance_id}", json={"maintenance": True})
    ).status_code == 200

    await auth_client.post("/api/filter-lists/refresh")

    assert FakeAdapter.state_for(A).refresh_calls == []
    assert FakeAdapter.state_for(B).refresh_calls == [False, True]


async def test_the_adguard_compatible_endpoint_reaches_the_nodes(
    auth_client: httpx.AsyncClient,
) -> None:
    """The one that answered a cheerful zero while doing nothing."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refresh_updates = 1

    response = await auth_client.post("/control/filtering/refresh")

    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 2}
    assert FakeAdapter.state_for(A).refresh_calls == [False, True]


# --------------------------------------------------------------------------
# When a list last arrived
# --------------------------------------------------------------------------


async def test_the_newest_download_any_node_reports_is_the_answer(
    auth_client: httpx.AsyncClient,
) -> None:
    """Same reasoning as the rule counts: nodes refresh on their own schedules."""
    await a_subscription(auth_client)
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    listed(A, last_updated=OLDER)
    listed(B, last_updated=FRESH)

    item = await sizes(auth_client)

    assert item["last_updated"] == "2026-09-24T22:00:00Z"
    assert sorted(entry["last_updated"] for entry in item["per_instance"]) == [
        "2026-09-01T09:00:00Z",
        "2026-09-24T22:00:00Z",
    ]


async def test_a_list_no_node_has_ever_fetched_says_so(auth_client: httpx.AsyncClient) -> None:
    """The fault worth catching. AdGuard keeps a list it cannot download, so a
    rotted URL is invisible until something reports that nothing ever arrived."""
    await a_subscription(auth_client)
    await add_instance(auth_client, "a", A)
    listed(A, last_updated="")

    item = await sizes(auth_client)

    assert item["last_updated"] is None
    assert item["per_instance"][0]["last_updated"] is None
