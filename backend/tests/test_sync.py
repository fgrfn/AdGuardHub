"""The core promise: one change in the hub reaches every instance (spec §2, §6)."""

from __future__ import annotations

import httpx
import pytest

from app.services.sync import drain_background

from .fakes import FakeAdapter

A = "http://adguard-a.local"
B = "http://adguard-b.local"


async def add_instance(client: httpx.AsyncClient, name: str, url: str) -> int:
    response = await client.post(
        "/api/instances",
        json={"name": name, "base_url": url, "username": "admin", "password": "pw"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_credentials_are_encrypted_and_never_returned(
    auth_client: httpx.AsyncClient,
) -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Instance

    await add_instance(auth_client, "a", A)
    body = (await auth_client.get("/api/instances")).json()[0]
    assert "password" not in body
    assert body["has_password"] is True

    async with session_scope() as session:
        instance = (await session.execute(select(Instance))).scalars().one()

    # Not a substring check: the ciphertext is base64, so it contains any given short
    # string by chance often enough to make that assertion flaky. What matters is that
    # the stored value is not the plaintext and only the right key recovers it.
    from app.security import Crypto

    assert instance.password_encrypted != "pw"
    assert Crypto("test-secret-key").decrypt(instance.password_encrypted) == "pw"
    with pytest.raises(ValueError):
        Crypto("a-different-key").decrypt(instance.password_encrypted)


async def test_rule_is_pushed_to_every_instance(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)

    created = await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    assert created.status_code == 201
    await drain_background()

    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]
    assert FakeAdapter.state_for(B).rules == ["||ads.example.com^"]


async def test_whitelisting_from_the_query_log_reaches_both_instances(
    auth_client: httpx.AsyncClient,
) -> None:
    """The failover race this project exists to fix: B must get A's whitelist too."""
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)

    response = await auth_client.post("/api/rules/allow", json={"domain": "Tracking.Example.COM."})
    assert response.status_code == 200
    assert response.json()["text"] == "@@||tracking.example.com^"
    assert response.json()["kind"] == "allow"
    await drain_background()

    for url in (A, B):
        assert FakeAdapter.state_for(url).rules == ["@@||tracking.example.com^"]


async def test_unreachable_instance_does_not_block_the_others(
    auth_client: httpx.AsyncClient,
) -> None:
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    FakeAdapter.state_for(B).offline = True

    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    # A still got the change immediately — no rollback, no waiting (spec §6).
    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]
    assert FakeAdapter.state_for(B).rules == []

    jobs = (await auth_client.get("/api/jobs")).json()
    assert [job["instance_name"] for job in jobs] == ["b"]
    assert jobs[0]["status"] == "failed"

    instances = {item["name"]: item for item in (await auth_client.get("/api/instances")).json()}
    assert instances["b"]["status"] == "unreachable"

    # Once B is back, the retry queue applies the full current state.
    FakeAdapter.state_for(B).offline = False
    retried = await auth_client.post("/api/jobs/retry")
    assert retried.json() == {"recovered": 1}
    assert FakeAdapter.state_for(B).rules == ["||ads.example.com^"]
    assert (await auth_client.get("/api/jobs")).json() == []


async def test_disabled_instances_are_skipped(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    instance_b = await add_instance(auth_client, "b", B)
    await auth_client.patch(f"/api/instances/{instance_b}", json={"enabled": False})

    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]
    assert FakeAdapter.state_for(B).rules == []
    assert (await auth_client.get("/api/jobs")).json() == []


async def test_disabled_rules_are_not_pushed(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    created = await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]

    await auth_client.patch(f"/api/rules/{created.json()['id']}", json={"enabled": False})
    await drain_background()
    assert FakeAdapter.state_for(A).rules == []


async def test_filter_list_subscriptions_are_pushed(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    response = await auth_client.post(
        "/api/filter-lists",
        json={"name": "AdGuard DNS filter", "url": "https://example.com/list.txt"},
    )
    assert response.status_code == 201
    await drain_background()

    lists = FakeAdapter.state_for(A).filter_lists
    assert [(item.url, item.kind, item.enabled) for item in lists] == [
        ("https://example.com/list.txt", "blocklist", True)
    ]

    await auth_client.patch(f"/api/filter-lists/{response.json()['id']}", json={"enabled": False})
    await drain_background()
    assert FakeAdapter.state_for(A).filter_lists[0].enabled is False


async def test_sections_are_only_pushed_when_managed(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)

    await auth_client.patch(
        "/api/config/sections/dns",
        json={"managed": False, "data": {"upstream_dns": ["1.1.1.1"]}},
    )
    await auth_client.post("/api/sync")
    assert FakeAdapter.state_for(A).sections == {}

    await auth_client.patch("/api/config/sections/dns", json={"managed": True})
    await drain_background()
    assert FakeAdapter.state_for(A).sections["dns"] == {"upstream_dns": ["1.1.1.1"]}


async def test_every_managed_section_reaches_the_instance(
    auth_client: httpx.AsyncClient,
) -> None:
    """The point of naming a master: the second node gets the whole configuration."""
    await add_instance(auth_client, "a", A)
    wanted = {
        "dns": {"upstream_dns": ["9.9.9.9"], "dnssec_enabled": True},
        "clients": {"clients": [{"name": "laptop", "ids": ["192.168.1.5"]}]},
        "access": {"allowed_clients": [], "disallowed_clients": ["1.2.3.4"]},
        "safesearch": {"enabled": True, "google": True},
        "rewrites": {"items": [{"domain": "nas.lan", "answer": "192.168.1.9"}]},
    }
    for name, data in wanted.items():
        await auth_client.patch(
            f"/api/config/sections/{name}", json={"managed": True, "data": data}
        )
    await drain_background()

    for name, data in wanted.items():
        assert FakeAdapter.state_for(A).sections[name] == data


async def test_a_section_the_instance_lacks_does_not_fail_the_push(
    auth_client: httpx.AsyncClient,
) -> None:
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).unsupported_sections = {"tls"}

    await auth_client.patch(
        "/api/config/sections/dns", json={"managed": True, "data": {"upstream_dns": ["1.1.1.1"]}}
    )
    await drain_background()
    assert FakeAdapter.state_for(A).sections["dns"] == {"upstream_dns": ["1.1.1.1"]}


async def test_only_the_tls_on_off_state_travels(auth_client: httpx.AsyncClient) -> None:
    """Each node terminates TLS with its own certificate; only the decision syncs."""
    await add_instance(auth_client, "a", A)
    await auth_client.patch(
        "/api/config/sections/tls", json={"managed": True, "data": {"enabled": True}}
    )
    await drain_background()

    assert FakeAdapter.state_for(A).sections["tls"] == {"enabled": True}

    listed = (await auth_client.get("/api/config/sections")).json()
    tls = next(item for item in listed if item["name"] == "tls")
    assert tls["keys"] == ["enabled"]
    assert tls["skipped_reason"] == ""


async def test_manual_full_sync_reports_failures(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    FakeAdapter.state_for(B).offline = True

    result = (await auth_client.post("/api/sync")).json()
    assert result["instances"] == 2
    assert list(result["failed"]) == ["b"]


async def test_a_section_can_be_built_without_importing_a_master(
    auth_client: httpx.AsyncClient,
) -> None:
    """A hub with no master to copy from is still a usable hub.

    Naming a master only saves you typing: it seeds the sections from a node that
    already has the configuration. Someone starting from nothing — a fresh pair of
    nodes, or an area they want to define rather than adopt — writes the section
    here and switches it on, and it reaches every instance the same way.
    """
    await add_instance(auth_client, "a", A)

    # Nothing has been imported: the section starts empty and unmanaged.
    listing = (await auth_client.get("/api/config/sections")).json()
    sections = {item["name"]: item for item in listing}
    assert sections["access"]["has_data"] is False
    assert sections["access"]["managed"] is False

    response = await auth_client.patch(
        "/api/config/sections/access",
        json={"managed": True, "data": {"disallowed_clients": ["10.0.0.9"]}},
    )
    assert response.status_code == 200
    await drain_background()

    assert FakeAdapter.state_for(A).sections["access"] == {"disallowed_clients": ["10.0.0.9"]}

    # And it is a normal hub version, so it can be diffed and rolled back.
    versions = (await auth_client.get("/api/versions")).json()
    assert any("access" in item["label"] for item in versions)


# --------------------------------------------------------------------------
# A push the node accepts and does not keep
# --------------------------------------------------------------------------


async def test_a_refused_rule_is_reported_by_the_push_itself(
    auth_client: httpx.AsyncClient,
) -> None:
    """Told while the operator is still looking at the button they pressed.

    Reconciliation catches this too, but five minutes later and under the name
    "drift" — which is the wrong word for a write that never landed.
    """
    instance_id = await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()

    body = (await auth_client.post(f"/api/instances/{instance_id}/push")).json()
    assert "did not keep" in body["error"]
    assert "@@||hitmyl.ink^" in body["error"]


async def test_a_refusal_does_not_go_to_the_retry_queue(
    auth_client: httpx.AsyncClient,
) -> None:
    """The queue is for a node that was unreachable.

    This node answered. Repeating the same write would not change the outcome,
    and queueing it would rebuild one layer down the loop reconciliation was
    just taught to stop.
    """
    instance_id = await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    await auth_client.post(f"/api/instances/{instance_id}/push")

    assert (await auth_client.get("/api/jobs")).json() == []


async def test_a_refusing_node_is_still_online(auth_client: httpx.AsyncClient) -> None:
    """It answered. Calling it unreachable would send an outage notification
    about a node that is serving DNS perfectly well."""
    instance_id = await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    await auth_client.post(f"/api/instances/{instance_id}/push")

    card = (await auth_client.get("/api/instances")).json()[0]
    assert card["status"] == "online"
    assert "did not keep" in card["last_error"]


async def test_a_partial_push_is_not_recorded_as_a_full_sync(
    auth_client: httpx.AsyncClient,
) -> None:
    """last_synced_at is the claim that everything landed. It did not."""
    instance_id = await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    await auth_client.post(f"/api/instances/{instance_id}/push")

    assert (await auth_client.get("/api/instances")).json()[0]["last_synced_at"] is None


async def test_a_rule_undone_by_the_rest_of_the_same_push_is_caught(
    auth_client: httpx.AsyncClient,
) -> None:
    """The three payloads of one push are not independent, and this is the proof.

    AdGuard reconfigures itself on every configuration change, so writing a
    settings section can undo the rule set that arrived moments before. While
    each payload was verified straight after its own write, that was invisible:
    the rules were read back before the sections had even been sent, the hub
    reported them as landed, and then overwrote them itself. The operator saw
    "corrected" and the rule was gone by the next run — with nothing anywhere
    saying so.
    """
    instance_id = await add_instance(auth_client, "a", A)
    state = FakeAdapter.state_for(A)
    # Managed sections, so the push carries one — without that there is no
    # second write to undo the first.
    await auth_client.patch(
        "/api/config/sections/access",
        json={"managed": True, "data": {"blocked_hosts": []}},
    )
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    state.section_push_drops_rules = True

    body = (await auth_client.post(f"/api/instances/{instance_id}/push")).json()

    assert "did not keep" in body["error"]
    assert "@@||hitmyl.ink^" in body["error"]


async def test_a_push_that_lands_says_nothing(auth_client: httpx.AsyncClient) -> None:
    """The check must not turn a working push into a complaint."""
    instance_id = await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules/allow", json={"domain": "example.com"})
    await drain_background()

    body = (await auth_client.post(f"/api/instances/{instance_id}/push")).json()
    assert body["error"] == ""
    card = (await auth_client.get("/api/instances")).json()[0]
    assert card["last_error"] == ""
    assert card["last_synced_at"] is not None


async def test_two_pushes_to_one_node_land_in_order(auth_client: httpx.AsyncClient) -> None:
    """Two edits a moment apart must leave the node with the state after both.

    Every push is full state and every edit schedules its own, so two edits race
    two pushes to the same node — and whichever the node *finishes* last is the
    state it keeps. Held up on the first push (a slow login, a node pausing to
    reconfigure), the state from before the second edit used to land last, and
    the node carried it until reconciliation happened to look.
    """
    import asyncio

    await add_instance(auth_client, "a", A)
    await drain_background()
    node = FakeAdapter.state_for(A)

    original = FakeAdapter.push_rules
    gate = asyncio.Event()
    pushes = 0

    async def first_push_is_slow(self: FakeAdapter, rules: list[str]) -> None:
        nonlocal pushes
        pushes += 1
        if pushes == 1:
            await gate.wait()
        await original(self, rules)

    FakeAdapter.push_rules = first_push_is_slow  # type: ignore[method-assign]
    try:
        added = await auth_client.post("/api/rules", json={"text": "||one.example^"})
        assert added.status_code == 201
        await asyncio.sleep(0.05)  # push #1 is now waiting inside the node
        added = await auth_client.post("/api/rules", json={"text": "||two.example^"})
        assert added.status_code == 201
        await asyncio.sleep(0.05)
        gate.set()
        await drain_background()
    finally:
        FakeAdapter.push_rules = original  # type: ignore[method-assign]

    assert sorted(node.rules) == ["||one.example^", "||two.example^"]



async def test_a_query_log_release_is_pushed_at_once_whatever_the_interval(
    auth_client: httpx.AsyncClient,
) -> None:
    """The reconciliation interval must not slow a change down.

    Asked directly when the default moved from five minutes to fifteen: does a
    domain released from the query log now wait a quarter of an hour? It does
    not, and nothing in the push path reads the interval — but "nothing reads it"
    is a claim about code that changes, so it is pinned here instead.

    The interval is set to its maximum first: a day. If propagation depended on
    the timer in any way, this would be the test that could not pass.
    """
    await add_instance(auth_client, "a", A)
    await add_instance(auth_client, "b", B)
    assert (
        await auth_client.put("/api/settings/hub", json={"reconcile_interval": 86_400})
    ).status_code == 200

    # What the query log's whitelist button calls, origin and all.
    released = await auth_client.post(
        "/api/rules/allow?origin=querylog", json={"domain": "doorbell.example.com"}
    )
    assert released.status_code == 200
    await drain_background()

    # Both nodes, not just the one the log entry came from (spec §9).
    for url in (A, B):
        assert FakeAdapter.state_for(url).rules == ["@@||doorbell.example.com^"]
