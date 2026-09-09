"""Drift detection and correction (spec §6)."""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import AdapterError, RemoteFilterList
from app.services.reconcile import diff_filter_lists, diff_rules, diff_settings
from app.services.sync import drain_background

from .fakes import FakeAdapter
from .test_sync import A, add_instance


def test_diff_rules_detects_both_directions() -> None:
    difference = diff_rules(["@@||a.com^", "||b.com^"], ["||b.com^", "||rogue.com^"])
    assert difference is not None
    assert difference.details["missing"] == ["@@||a.com^"]
    assert difference.details["extra"] == ["||rogue.com^"]


def test_diff_rules_is_quiet_when_states_match() -> None:
    assert diff_rules(["||a.com^"], ["||a.com^"]) is None


def test_diff_filter_lists_notices_a_disabled_subscription() -> None:
    expected = [RemoteFilterList("List", "https://e.com/l.txt", True, "blocklist")]
    actual = [RemoteFilterList("List", "https://e.com/l.txt", False, "blocklist")]
    difference = diff_filter_lists(expected, actual)
    assert difference is not None
    assert difference.details["changed"] == ["blocklist:https://e.com/l.txt"]


def test_diff_settings_is_quiet_when_nothing_is_managed() -> None:
    assert diff_settings({}, {}) is None


def test_diff_settings_reports_the_changed_keys_per_section() -> None:
    difference = diff_settings(
        {"dns": {"upstream_dns": ["1.1.1.1"], "dnssec_enabled": True}},
        {"dns": {"upstream_dns": ["8.8.8.8"], "dnssec_enabled": True}},
    )
    assert difference is not None
    assert difference.details["dns"]["upstream_dns"]["actual"] == ["8.8.8.8"]
    # An unchanged key must not be reported as drift.
    assert "dnssec_enabled" not in difference.details["dns"]


def test_a_node_resolving_its_own_time_zone_is_not_drift() -> None:
    """``Local`` asks for the node's own zone, so the zone it names is the answer.

    Reported from a running hub: every reconciliation run found blocked_services
    differing, corrected it, and found it again five minutes later — because the
    hub sent "Local" and the node read back "Europe/Berlin".
    """
    difference = diff_settings(
        {"blocked_services": {"ids": ["tiktok"], "schedule": {"time_zone": "Local"}}},
        {"blocked_services": {"ids": ["tiktok"], "schedule": {"time_zone": "Europe/Berlin"}}},
    )
    assert difference is None


def test_a_real_time_zone_mismatch_is_still_drift() -> None:
    """Only the placeholder is forgiven — a named zone still has to match."""
    difference = diff_settings(
        {"blocked_services": {"schedule": {"time_zone": "Europe/Berlin"}}},
        {"blocked_services": {"schedule": {"time_zone": "UTC"}}},
    )
    assert difference is not None
    assert difference.details["blocked_services"]["schedule"]["actual"] == {"time_zone": "UTC"}


def test_the_time_zone_allowance_does_not_leak_into_other_sections() -> None:
    """The allowance is bound to one path, not to the key name."""
    difference = diff_settings(
        {"querylog": {"time_zone": "Local"}},
        {"querylog": {"time_zone": "Europe/Berlin"}},
    )
    assert difference is not None


def test_diff_settings_flags_an_unsupported_section_without_calling_it_drift() -> None:
    """An instance that lacks an area is a capability gap, not a config difference."""
    difference = diff_settings({"tls": {"enabled": True}}, {"tls": None})
    assert difference is not None
    assert difference.details["_unsupported"] == ["tls"]
    assert "not supported" in difference.summary


async def test_out_of_band_change_is_corrected_and_logged(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    # Someone edits the instance directly in the native UI, despite spec §2.
    FakeAdapter.state_for(A).rules = ["||something-else.com^"]

    reports = (await auth_client.post("/api/reconcile")).json()
    assert reports[0]["corrected"] is True
    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]

    drift = (await auth_client.get("/api/drift")).json()
    assert len(drift) == 1
    assert drift[0]["instance_name"] == "a"
    assert drift[0]["payload_kind"] == "rules"
    assert drift[0]["corrected"] is True


async def test_dry_run_reports_without_correcting(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = []

    reports = (await auth_client.post("/api/reconcile?apply_fixes=false")).json()
    assert reports[0]["corrected"] is False
    assert len(reports[0]["differences"]) == 1
    assert FakeAdapter.state_for(A).rules == []

    # Detection without correction is still logged, never silent.
    assert (await auth_client.get("/api/drift")).json()[0]["corrected"] is False


async def test_reconcile_records_an_unreachable_instance(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    # A hub with nothing in it does not reconcile at all, so the node has to be
    # unreachable to a hub that has something to say.
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).offline = True

    reports = (await auth_client.post("/api/reconcile")).json()
    assert reports[0]["checked"] is False
    assert "unreachable" in reports[0]["error"]
    assert (await auth_client.get("/api/drift")).json() == []


async def test_reconcile_finds_nothing_when_in_sync(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    reports = (await auth_client.post("/api/reconcile")).json()
    assert reports[0]["differences"] == []
    assert reports[0]["corrected"] is False


async def test_a_capability_gap_is_not_logged_as_drift(auth_client: httpx.AsyncClient) -> None:
    """An area the instance does not implement must not append drift on every run.

    Reconciliation runs on a timer, so logging a standing capability gap would grow
    the drift log forever and drown the real findings.
    """
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).unsupported_sections = {"tls"}
    await auth_client.patch(
        "/api/config/sections/tls", json={"managed": True, "data": {"enabled": True}}
    )
    await drain_background()

    for _ in range(3):
        reports = (await auth_client.post("/api/reconcile")).json()

    # It is still reported to the caller…
    assert "not supported" in reports[0]["differences"][0]["summary"]
    # …but never written to the log.
    assert (await auth_client.get("/api/drift")).json() == []


async def test_drift_is_reported_even_when_the_correction_fails(
    auth_client: httpx.AsyncClient,
) -> None:
    """The dashboard summary reads `differences`, so a failed fix must not look clean."""
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    FakeAdapter.state_for(A).rules = ["||rogue.example^"]
    # The instance drops off between the read and the write.
    original_push = FakeAdapter.push_rules

    async def failing_push(self, rules):
        raise AdapterError("connection reset")

    FakeAdapter.push_rules = failing_push
    try:
        reports = (await auth_client.post("/api/reconcile")).json()
    finally:
        FakeAdapter.push_rules = original_push

    assert reports[0]["checked"] is True
    assert reports[0]["corrected"] is False
    assert len(reports[0]["differences"]) == 1
    assert "connection reset" in reports[0]["error"]

    # Logged as found-but-not-fixed, rather than silently dropped or marked corrected.
    drift = (await auth_client.get("/api/drift")).json()
    assert len(drift) == 1
    assert drift[0]["corrected"] is False


async def test_each_difference_records_its_own_outcome(
    auth_client: httpx.AsyncClient,
) -> None:
    """Rules can land while settings fail; one aggregate flag would mislabel both."""
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await auth_client.patch(
        "/api/config/sections/dns", json={"managed": True, "data": {"upstream_dns": ["1.1.1.1"]}}
    )
    await drain_background()

    state = FakeAdapter.state_for(A)
    state.rules = []
    state.sections["dns"] = {"upstream_dns": ["8.8.8.8"]}

    async def failing_section(self, name, data):
        raise AdapterError("section rejected")

    original = FakeAdapter.push_section
    FakeAdapter.push_section = failing_section
    try:
        await auth_client.post("/api/reconcile")
    finally:
        FakeAdapter.push_section = original

    outcomes = {
        event["payload_kind"]: event["corrected"]
        for event in (await auth_client.get("/api/drift")).json()
    }
    assert outcomes == {"rules": True, "settings": False}


async def test_the_drift_log_can_be_cleared(auth_client: httpx.AsyncClient) -> None:
    """A log full of findings whose cause is already gone is noise, not evidence."""
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = []
    await auth_client.post("/api/reconcile")
    assert (await auth_client.get("/api/drift")).json()

    response = await auth_client.delete("/api/drift")
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert (await auth_client.get("/api/drift")).json() == []


async def test_clearing_the_log_does_not_resolve_the_difference(
    auth_client: httpx.AsyncClient,
) -> None:
    """The button empties a record, not the fleet. Anything still out of step returns."""
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()

    FakeAdapter.state_for(A).rules = []
    await auth_client.post("/api/reconcile?apply_fixes=false")
    await auth_client.delete("/api/drift")

    await auth_client.post("/api/reconcile?apply_fixes=false")
    assert len((await auth_client.get("/api/drift")).json()) == 1


async def test_clearing_an_empty_log_is_not_an_error(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.delete("/api/drift")
    assert response.status_code == 200
    assert response.json() == {"deleted": 0}


async def test_clearing_the_drift_log_needs_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.delete("/api/drift")).status_code == 401


# --------------------------------------------------------------------------
# A correction that does not hold
# --------------------------------------------------------------------------


async def test_a_refused_rule_is_not_reported_as_corrected(
    auth_client: httpx.AsyncClient,
) -> None:
    """Reported from a real fleet: the same allow rule pushed every five minutes.

    AdGuard answered 2xx and did not keep the rule. The hub called that
    "corrected", rediscovered it on the next run, corrected it again, and wrote a
    drift event and fired a notification each time — while knowing, every time,
    that its own correction had not taken.
    """
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()

    reports = (await auth_client.post("/api/reconcile")).json()
    assert reports[0]["corrected"] is False, "nothing landed, so nothing was corrected"

    events = (await auth_client.get("/api/drift")).json()
    assert len(events) == 1
    assert "did not keep" in events[0]["summary"]
    assert events[0]["corrected"] is False
    assert "@@||hitmyl.ink^" in events[0]["details"]


async def test_the_same_refusal_is_said_once_rather_than_every_run(
    auth_client: httpx.AsyncClient,
) -> None:
    """The loop is the defect. One entry states it; three hundred bury it."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()

    for _ in range(5):
        await auth_client.post("/api/reconcile")

    assert len((await auth_client.get("/api/drift")).json()) == 1


async def test_a_refusal_that_changes_is_reported_again(
    auth_client: httpx.AsyncClient,
) -> None:
    """Quieting a repeat must not quiet a new fact."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    await auth_client.post("/api/reconcile")

    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^", "@@||second.example^"}
    await auth_client.post("/api/rules/allow", json={"domain": "second.example"})
    await drain_background()
    await auth_client.post("/api/reconcile")

    events = (await auth_client.get("/api/drift")).json()
    assert len(events) == 2
    assert "second.example" in events[0]["details"]


async def test_a_node_that_starts_keeping_it_is_corrected_normally(
    auth_client: httpx.AsyncClient,
) -> None:
    """Recovery has to leave the quiet state behind, or it is a new kind of stuck."""
    await add_instance(auth_client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await auth_client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()
    await auth_client.post("/api/reconcile")

    FakeAdapter.state_for(A).refuses = set()
    reports = (await auth_client.post("/api/reconcile")).json()

    assert reports[0]["corrected"] is True
    assert FakeAdapter.state_for(A).rules == ["@@||hitmyl.ink^"]
    events = (await auth_client.get("/api/drift")).json()
    assert events[0]["corrected"] is True
    assert "did not keep" not in events[0]["summary"]


async def test_an_ordinary_correction_still_reads_as_one(
    auth_client: httpx.AsyncClient,
) -> None:
    """The verification must not turn a working correction into a complaint."""
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = []

    reports = (await auth_client.post("/api/reconcile")).json()
    assert reports[0]["corrected"] is True
    event = (await auth_client.get("/api/drift")).json()[0]
    assert event["corrected"] is True
    assert "did not keep" not in event["summary"]


async def test_an_edit_is_not_blocked_while_a_slow_node_is_corrected(
    auth_client: httpx.AsyncClient,
) -> None:
    """Reconciliation must not hold the database while it talks to a node.

    Setting the instance's status marks the row dirty, the first diff query
    flushes it, and SQLite hands that connection the write lock — which it then
    kept through every correction push and read-back. Against a slow node that
    is seconds, during which every write to the hub waited on SQLite's busy
    timeout and then failed with "database is locked".
    """
    import asyncio
    import time

    from app.db import session_scope
    from app.services.reconcile import reconcile_all
    from app.services.sync import drain_background

    from .fakes import FakeAdapter
    from .test_sync import A, add_instance

    await add_instance(auth_client, "a", A)
    await drain_background()
    node = FakeAdapter.state_for(A)
    node.rules = ["||out-of-band.example^"]  # so the pass has something to correct

    original = FakeAdapter.push_rules
    gate = asyncio.Event()

    async def a_slow_node(self: FakeAdapter, rules: list[str]) -> None:
        await gate.wait()
        await original(self, rules)

    FakeAdapter.push_rules = a_slow_node  # type: ignore[method-assign]
    try:

        async def correct() -> None:
            async with session_scope() as session:
                await reconcile_all(session)

        pass_ = asyncio.create_task(correct())
        await asyncio.sleep(0.05)  # the correction is now waiting inside the node

        started = time.monotonic()
        edit = await auth_client.post("/api/rules", json={"text": "||new.example^"})
        assert edit.status_code == 201, edit.text
        assert time.monotonic() - started < 1.0, "the edit waited on the database"
    finally:
        gate.set()
        FakeAdapter.push_rules = original  # type: ignore[method-assign]
    await pass_
    await drain_background()


async def test_the_nodes_client_is_closed_even_when_the_pass_blows_up(
    auth_client: httpx.AsyncClient, monkeypatch
) -> None:
    """The adapter owns an HTTP client. The pass closed it after a failed pull
    and after a normal finish, and nowhere in between."""
    from app.db import session_scope
    from app.services.reconcile import reconcile_all
    from app.services.sync import drain_background

    from .fakes import FakeAdapter
    from .test_sync import A, add_instance

    await add_instance(auth_client, "a", A)
    # Something to replicate, or the pass is skipped before it opens a client.
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = ["||out-of-band.example^"]  # something to correct

    closed = 0

    async def count_close(self: FakeAdapter) -> None:
        nonlocal closed
        closed += 1

    async def blow_up(self: FakeAdapter, rules: list[str]) -> None:
        raise RuntimeError("not an AdapterError: the kind nobody planned for")

    monkeypatch.setattr(FakeAdapter, "aclose", count_close)
    monkeypatch.setattr(FakeAdapter, "push_rules", blow_up)

    with pytest.raises(RuntimeError):
        async with session_scope() as session:
            await reconcile_all(session)

    assert closed == 1
