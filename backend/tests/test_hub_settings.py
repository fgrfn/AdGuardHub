"""Operational settings: editable at runtime, bounded, and actually applied."""

from __future__ import annotations

import httpx

from app.services import hubsettings
from app.services.querylog import buffer


async def test_defaults_come_from_the_environment(auth_client: httpx.AsyncClient) -> None:
    values = (await auth_client.get("/api/settings/hub")).json()
    # Fifteen minutes: propagation rides on the instant push, not on this timer,
    # so what is left for it is drift caused outside the hub.
    assert values["reconcile_interval"] == 900
    assert values["reconcile_enabled"] is True
    assert values["limits"]["reconcile_interval"] == [30, 86400]


async def test_changes_take_effect_without_a_restart(auth_client: httpx.AsyncClient) -> None:
    """The workers read the cache each cycle, so the process must see the new value."""
    # Deliberately not the default, or this asserts nothing.
    response = await auth_client.put(
        "/api/settings/hub", json={"reconcile_interval": 1200, "retry_interval": 60}
    )
    assert response.status_code == 200
    assert response.json()["reconcile_interval"] == 1200

    assert hubsettings.current().reconcile_interval == 1200
    assert hubsettings.current().retry_interval == 60


async def test_values_are_clamped_to_a_sane_range(auth_client: httpx.AsyncClient) -> None:
    """A typo must not turn the poller into a request flood."""
    values = (
        await auth_client.put(
            "/api/settings/hub", json={"querylog_poll_interval": 0, "http_timeout": 9999}
        )
    ).json()
    assert values["querylog_poll_interval"] == 1
    assert values["http_timeout"] == 120


async def test_buffer_size_is_applied_to_the_live_buffer(
    auth_client: httpx.AsyncClient,
) -> None:
    await auth_client.put("/api/settings/hub", json={"querylog_buffer_size": 100})
    assert buffer._entries.maxlen == 100


async def test_partial_updates_leave_the_rest_alone(auth_client: httpx.AsyncClient) -> None:
    await auth_client.put("/api/settings/hub", json={"reconcile_interval": 600})
    values = (await auth_client.put("/api/settings/hub", json={"retry_interval": 45})).json()
    assert values["reconcile_interval"] == 600
    assert values["retry_interval"] == 45


async def test_polling_and_reconciliation_can_be_paused(
    auth_client: httpx.AsyncClient,
) -> None:
    values = (
        await auth_client.put(
            "/api/settings/hub", json={"reconcile_enabled": False, "querylog_enabled": False}
        )
    ).json()
    assert values["reconcile_enabled"] is False
    assert hubsettings.current().querylog_enabled is False
