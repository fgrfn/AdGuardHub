"""The file an operator attaches to a bug report.

A backup stays with the person who downloaded it. This one gets pasted into a
public issue, so the tests that matter here are about what is *not* in it —
and, just as much, that enough is left to be worth sending.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.db import session_scope
from app.models import DriftEvent, JobStatus, PushJob
from app.services.logbuffer import get_buffer

NODE_URL = "http://adguard-primary.home.arpa:8080"
NODE_NAME = "Wohnzimmer-Primary"
NODE_USER = "hausmeister"


async def _add_node(client: httpx.AsyncClient) -> int:
    response = await client.post(
        "/api/instances",
        json={
            "name": NODE_NAME,
            "base_url": NODE_URL,
            "username": NODE_USER,
            "password": "verysecret",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _bundle(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/diagnostics")
    assert response.status_code == 200, response.text
    # Offered as a file, or a browser just renders the JSON.
    assert "attachment" in response.headers["content-disposition"]
    return response.json()


async def test_it_needs_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/diagnostics")).status_code == 401


async def test_nothing_in_it_says_where_a_node_is_or_who_signs_in(
    auth_client: httpx.AsyncClient,
) -> None:
    await _add_node(auth_client)

    raw = json.dumps(await _bundle(auth_client))

    for secret in (NODE_URL, "adguard-primary.home.arpa", NODE_NAME, NODE_USER, "verysecret"):
        assert secret not in raw, f"{secret!r} reached the bundle"


async def test_what_is_diagnostic_about_an_address_survives_it(
    auth_client: httpx.AsyncClient,
) -> None:
    await _add_node(auth_client)

    node = (await _bundle(auth_client))["instances"][0]

    assert node["id"] == "node-1"
    # A name against an address is the first question when a node the hub cannot
    # reach answers fine in a browser, and the port is the second.
    assert node["scheme"] == "http"
    assert node["host_kind"] == "name"
    assert node["port"] == 8080
    assert node["has_password"] is True


async def test_an_address_inside_an_error_string_is_replaced_too(
    auth_client: httpx.AsyncClient,
) -> None:
    """The case that makes redacting the column alone security theatre.

    A node's address turns up in free text far more often than in the field
    meant for it: in ``last_error``, in a queued job's error, and in every log
    line about the push that failed.
    """
    node_id = await _add_node(auth_client)
    async with session_scope() as session:
        session.add(
            PushJob(
                instance_id=node_id,
                payload_kind="rules",
                status=JobStatus.failed.value,
                attempts=3,
                reason="allowed a domain",
                last_error=f"connecting to {NODE_URL}/control/status: timed out",
            )
        )
        await session.commit()

    job = (await _bundle(auth_client))["jobs"][0]

    assert NODE_URL not in job["last_error"]
    assert "<node-1>" in job["last_error"]
    # The shape of the failure is the part worth keeping.
    assert "timed out" in job["last_error"]
    assert job["instance"] == "node-1"
    assert job["attempts"] == 3


async def test_one_node_reads_as_one_pseudonym_everywhere(
    auth_client: httpx.AsyncClient,
) -> None:
    """Naming a node is what leaks; correlating it is the point of the file."""
    node_id = await _add_node(auth_client)
    async with session_scope() as session:
        session.add(
            DriftEvent(
                instance_id=node_id,
                instance_name=NODE_NAME,
                payload_kind="rules",
                summary="the node did not keep this correction — 1 rule missing",
                details=json.dumps({"missing": ["@@||example.test^"]}),
                corrected=False,
            )
        )
        await session.commit()
    logging.getLogger("app.services.sync").warning(
        "Push to %s failed (rules): refused", NODE_NAME
    )

    bundle = await _bundle(auth_client)

    assert bundle["instances"][0]["id"] == "node-1"
    assert bundle["drift"][0]["instance"] == "node-1"
    assert any("<node-1>" in line for line in bundle["log"])


async def test_the_filtering_content_it_exists_to_explain_stays_in(
    auth_client: httpx.AsyncClient,
) -> None:
    """"This allow rule will not stick" is unanswerable without the rule."""
    node_id = await _add_node(auth_client)
    await auth_client.post("/api/rules", json={"text": "@@||example.test^", "kind": "allow"})
    async with session_scope() as session:
        session.add(
            DriftEvent(
                instance_id=node_id,
                payload_kind="rules",
                summary="1 rule missing",
                details=json.dumps({"missing": ["@@||example.test^"]}),
                corrected=False,
            )
        )
        await session.commit()

    bundle = await _bundle(auth_client)

    assert bundle["counts"]["rules"] == 1
    assert bundle["counts"]["rules_allow"] == 1
    assert "@@||example.test^" in bundle["drift"][0]["details"]


async def test_a_notifier_is_reported_by_host_and_never_by_path(
    auth_client: httpx.AsyncClient,
) -> None:
    """A Discord webhook URL *is* its credential, and the credential is the path."""
    response = await auth_client.post(
        "/api/settings/notifiers",
        json={
            "name": "Discord",
            "type": "discord",
            "url": "https://discord.com/api/webhooks/12345/aVerySecretToken",
            "events": [],
        },
    )
    assert response.status_code == 201, response.text

    target = (await _bundle(auth_client))["notifiers"][0]

    assert target["url_host"] == "https://discord.com"
    assert "aVerySecretToken" not in json.dumps(target)
    assert target["type"] == "discord"


async def test_a_subscription_keeps_its_address_and_loses_its_query(
    auth_client: httpx.AsyncClient,
) -> None:
    """Which lists somebody runs answers half the filtering reports.

    The query string goes, because that is where a self-hosted list would carry
    a token.
    """
    await auth_client.post(
        "/api/filter-lists",
        json={
            "name": "Mine",
            "url": "https://lists.example.com/mine.txt?key=abc123",
            "kind": "blocklist",
        },
    )

    entry = (await _bundle(auth_client))["filter_lists"][0]

    assert entry["url"] == "https://lists.example.com/mine.txt"
    assert "abc123" not in json.dumps(entry)


async def test_a_section_is_reported_by_shape_and_never_by_value(
    auth_client: httpx.AsyncClient,
) -> None:
    """A section document is a map of the household — clients, MACs, resolvers."""
    await auth_client.patch(
        "/api/config/sections/access",
        json={"managed": True, "data": {"disallowed_clients": ["10.0.0.9"], "blocked_hosts": []}},
    )

    bundle = await _bundle(auth_client)
    access = next(row for row in bundle["sections"] if row["name"] == "access")

    assert access["managed"] is True
    assert access["keys"] == ["blocked_hosts", "disallowed_clients"]
    assert "10.0.0.9" not in json.dumps(bundle)


async def test_a_client_address_in_a_log_line_is_masked(
    auth_client: httpx.AsyncClient,
) -> None:
    """What is left after the nodes are replaced belongs to clients and to the
    hub's own host. Neither is diagnostic, and together they map a network."""
    logging.getLogger("uvicorn.access").info('192.168.7.31 - "GET /api/dashboard" 200')

    bundle = await _bundle(auth_client)

    assert "192.168.7.31" not in json.dumps(bundle)
    assert any("<local-ip>" in line for line in bundle["log"])


async def test_a_public_address_is_left_alone(auth_client: httpx.AsyncClient) -> None:
    """Upstream resolvers are public addresses and naming them costs nothing."""
    logging.getLogger("app.services.sync").info("upstream 1.1.1.1 answered")

    bundle = await _bundle(auth_client)

    assert any("1.1.1.1" in line for line in bundle["log"])


async def test_the_hub_describes_itself_without_naming_its_paths(
    auth_client: httpx.AsyncClient,
) -> None:
    bundle = await _bundle(auth_client)

    assert bundle["format"] == "adguardhub-diagnostics"
    assert bundle["hub"]["version"]
    assert bundle["hub"]["install_method"] in {"docker", "native", "source"}
    # An unset key means credentials are re-encrypted on every boot, which
    # explains "my nodes lost their passwords again" before anyone asks.
    assert bundle["hub"]["secret_key_set"] is True
    # The path can name a home directory, so it is never in the bundle — only
    # whether a log is being kept, which is the diagnostic content of the answer.
    assert "log_file" not in bundle["hub"]
    # True by default now: this flag is read to decide whether there is evidence
    # worth asking for, and reporting "no log kept" on a hub that keeps one sent
    # one investigation looking for something it already had.
    assert bundle["hub"]["log_file_configured"] is True


@pytest.fixture(autouse=True)
def _quiet_buffer():
    """Each test reads the process-wide log buffer, so start it empty."""
    get_buffer().clear()
    yield
    get_buffer().clear()


async def test_ipv6_and_hardware_addresses_are_masked_like_private_ipv4(
    auth_client: httpx.AsyncClient,
) -> None:
    """A drift entry about the clients section names the household by every
    address family it has. IPv4 was masked; the rest shipped."""
    await _add_node(auth_client)
    async with session_scope() as session:
        session.add(
            DriftEvent(
                instance_id=1,
                instance_name=NODE_NAME,
                payload_kind="settings",
                summary="1 section(s) differ: clients",
                details=(
                    '{"clients": {"expected": [{"name": "laptop", "ids": ["192.168.1.20", '
                    '"fd12:3456:789a::20", "fe80::1c2d:3e4f:5a6b:7c8d%eth0", '
                    '"aa:bb:cc:dd:ee:ff"]}], "actual": []}}'
                ),
            )
        )
        await session.commit()
    logging.getLogger("adguardhub.auth").warning("Failed sign-in from fd00::42 (hub login)")
    logging.getLogger("adguardhub.auth").warning("Failed sign-in from ::1 (hub login)")

    raw = json.dumps(await _bundle(auth_client))

    for private in (
        "192.168.1.20",
        "fd12:3456:789a::20",
        "fe80::1c2d:3e4f:5a6b:7c8d",
        "aa:bb:cc:dd:ee:ff",
        "fd00::42",
        "from ::1 ",
    ):
        assert private not in raw, f"{private!r} reached the bundle"
    assert "<local-ipv6>" in raw
    assert "<mac>" in raw


async def test_a_public_resolver_and_a_time_survive_the_masking(
    auth_client: httpx.AsyncClient,
) -> None:
    """The masking must not eat what is diagnostic: which upstream a node uses,
    or when something happened."""
    await _add_node(auth_client)
    async with session_scope() as session:
        session.add(
            DriftEvent(
                instance_id=1,
                instance_name=NODE_NAME,
                payload_kind="settings",
                summary="1 section(s) differ: dns",
                details=(
                    '{"dns": {"expected": {"upstream_dns": ["2606:4700:4700::1111", '
                    '"9.9.9.9"]}, "actual": {"upstream_dns": ["tls://dns.quad9.net"]}}}'
                ),
            )
        )
        await session.commit()
    logging.getLogger("adguardhub").info("Reconcile pass at 12:34:56, took 00:00:03")

    raw = json.dumps(await _bundle(auth_client))

    assert "2606:4700:4700::1111" in raw
    assert "9.9.9.9" in raw
    assert "12:34:56" in raw
    assert "00:00:03" in raw
