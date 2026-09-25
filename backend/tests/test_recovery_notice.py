"""The notification that says an outage is over.

The hub had three events and all of them were bad news. You were told at 3am
that node-b had gone and never that it came back, so the only way to learn the
outage had ended was to go and look — which is the thing an alert exists to save
you from.

What has to be true for the notice to be worth having: it fires on the edge, not
on every pass that finds a node up. A message per reconciliation for a node that
is merely still online is how an alert becomes one people mute, and a muted
alert is worse than none, because it also mutes the outage.
"""

from __future__ import annotations

import httpx
import pytest

from app.db import session_scope
from app.models import Instance, InstanceStatus, PayloadKind
from app.services import notify as notify_module
from app.services import reconcile as reconcile_module
from app.services import sync as sync_module
from app.services.notify import (
    EVENT_INSTANCE_RECOVERED,
    EVENT_INSTANCE_UNREACHABLE,
    EVENT_PUSH_FAILED,
)
from app.services.sync import check_instance, push_to_instance

from .fakes import FakeAdapter
from .test_sync import A, add_instance

ALL_KINDS = (PayloadKind.rules, PayloadKind.filters, PayloadKind.settings)


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    """Collect what would have gone to the webhooks, as (event, title).

    Patched in all three modules on purpose: ``sync`` and ``reconcile`` each did
    ``from .notify import notify``, so each holds its own binding and patching
    only the source module would silently miss their calls — which is exactly
    what happened the first time this was written.
    """
    captured: list[tuple[str, str]] = []

    async def fake_notify(event: str, title: str, message: str) -> None:
        captured.append((event, title))

    monkeypatch.setattr(notify_module, "notify", fake_notify)
    monkeypatch.setattr(reconcile_module, "notify", fake_notify)
    monkeypatch.setattr(sync_module, "notify", fake_notify)
    return captured


async def _instance(client: httpx.AsyncClient) -> int:
    return await add_instance(client, "node-a", A)


async def _set_status(instance_id: int, status: InstanceStatus) -> None:
    async with session_scope() as session:
        row = await session.get(Instance, instance_id)
        assert row is not None
        row.status = status.value
        await session.commit()


def _events(sent: list[tuple[str, str]]) -> list[str]:
    return [event for event, _title in sent]


# --------------------------------------------------------------------------
# The edge
# --------------------------------------------------------------------------


async def test_a_node_that_comes_back_is_announced(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.unreachable)
    sent.clear()

    async with session_scope() as session:
        await check_instance(session, await session.get(Instance, instance_id))

    assert _events(sent) == [EVENT_INSTANCE_RECOVERED]
    assert "node-a" in sent[0][1]


async def test_a_node_that_was_already_up_says_nothing(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """The property that decides whether this alert is usable at all.

    Reconciliation talks to every node on a timer. Announcing each success would
    be a message every few minutes, for ever.
    """
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.online)
    sent.clear()

    for _ in range(3):
        async with session_scope() as session:
            await check_instance(session, await session.get(Instance, instance_id))

    assert sent == []


async def test_going_down_still_only_reports_the_outage(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.online)
    FakeAdapter.state_for(A).offline = True
    sent.clear()

    async with session_scope() as session:
        await check_instance(session, await session.get(Instance, instance_id))

    assert _events(sent) == [EVENT_INSTANCE_UNREACHABLE]


async def test_an_outage_and_its_recovery_are_one_message_each(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """The whole story, over a realistic sequence of polls."""
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.online)
    state = FakeAdapter.state_for(A)
    sent.clear()

    async def poll() -> None:
        async with session_scope() as session:
            await check_instance(session, await session.get(Instance, instance_id))

    await poll()  # still up
    state.offline = True
    await poll()  # goes down
    await poll()  # still down
    await poll()  # still down
    state.offline = False
    await poll()  # comes back
    await poll()  # still up

    assert _events(sent) == [EVENT_INSTANCE_UNREACHABLE, EVENT_INSTANCE_RECOVERED]


# --------------------------------------------------------------------------
# The other two ways a node is found to be back
# --------------------------------------------------------------------------


async def test_a_successful_push_announces_the_recovery(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """The retry queue is often what first reaches a node again."""
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.unreachable)
    sent.clear()

    async with session_scope() as session:
        instance = await session.get(Instance, instance_id)
        error = await push_to_instance(session, instance, ALL_KINDS, "retry")

    assert error == ""
    assert _events(sent) == [EVENT_INSTANCE_RECOVERED]


async def test_reconciliation_announces_the_recovery_before_any_drift(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """Order matters: "node-a is back" reads oddly *after* "drift corrected"."""
    instance_id = await _instance(auth_client)
    # Drift for reconciliation to find and correct on the way through.
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    FakeAdapter.state_for(A).rules = ["||something-else.example^"]
    await _set_status(instance_id, InstanceStatus.unreachable)
    sent.clear()

    async with session_scope() as session:
        await reconcile_module.reconcile_instance(
            session, await session.get(Instance, instance_id)
        )

    assert _events(sent)[0] == EVENT_INSTANCE_RECOVERED, (
        f"expected the recovery first, got {_events(sent)}"
    )


async def test_a_dry_run_still_announces_a_node_that_is_back(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """Reaching a node is an observation, not a correction.

    A dry run declines to *change* anything; it still records that the node
    answered, so staying silent here would mean the status flipped in the UI with
    no matching notification.
    """
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.unreachable)
    sent.clear()

    async with session_scope() as session:
        await reconcile_module.reconcile_instance(
            session, await session.get(Instance, instance_id), apply_fixes=False
        )

    assert EVENT_INSTANCE_RECOVERED in _events(sent)


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------


async def test_the_new_event_is_offered_in_the_settings_api(
    auth_client: httpx.AsyncClient,
) -> None:
    meta = await auth_client.get("/api/settings/notifiers/meta")
    assert meta.status_code == 200
    assert EVENT_INSTANCE_RECOVERED in meta.json()["events"]


async def test_a_target_can_subscribe_to_it(auth_client: httpx.AsyncClient) -> None:
    """A target that names its events must be able to name this one."""
    created = await auth_client.post(
        "/api/settings/notifiers",
        json={
            "name": "gotify",
            "type": "gotify",
            "url": "http://gotify.test/message",
            "events": [EVENT_INSTANCE_RECOVERED],
        },
    )
    assert created.status_code in (200, 201), created.text
    assert EVENT_INSTANCE_RECOVERED in created.json()["events"]


# --------------------------------------------------------------------------
# The push that keeps failing
# --------------------------------------------------------------------------


async def test_a_node_that_stays_down_reports_the_failed_push_once(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """The same rule as above, on the event that never followed it.

    The retry queue re-pushes every open job on its own timer — three payload
    kinds every thirty seconds by default. Reporting each attempt meant a message
    per kind per pass for as long as the node was down: several hundred an hour,
    to every notifier, each one the same sentence about the same outage.
    """
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.online)
    FakeAdapter.state_for(A).offline = True
    sent.clear()

    for _ in range(5):
        async with session_scope() as session:
            await push_to_instance(
                session, await session.get(Instance, instance_id), ALL_KINDS, "retry"
            )

    assert _events(sent) == [EVENT_PUSH_FAILED, EVENT_INSTANCE_UNREACHABLE], (
        f"one message per outage, not one per attempt — got {_events(sent)}"
    )


async def test_a_different_failure_is_reported_again(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """Silence is for the repetition, not for the node.

    A node that stops timing out and starts refusing the credentials is a
    different fault with a different fix, and the operator has been told nothing
    about it yet.
    """
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.online)
    state = FakeAdapter.state_for(A)
    state.offline = True
    sent.clear()

    async def attempt() -> None:
        async with session_scope() as session:
            await push_to_instance(
                session, await session.get(Instance, instance_id), ALL_KINDS, "retry"
            )

    await attempt()
    await attempt()
    state.offline_error = "401: the credentials were refused"
    await attempt()
    await attempt()

    assert _events(sent).count(EVENT_PUSH_FAILED) == 2, (
        f"the new reason is news, the repeats are not — got {_events(sent)}"
    )


async def test_the_failure_is_reported_again_after_a_recovery(
    auth_client: httpx.AsyncClient, sent: list[tuple[str, str]]
) -> None:
    """A second outage is a second outage, even with the identical error text."""
    instance_id = await _instance(auth_client)
    await _set_status(instance_id, InstanceStatus.online)
    state = FakeAdapter.state_for(A)
    sent.clear()

    async def attempt() -> None:
        async with session_scope() as session:
            await push_to_instance(
                session, await session.get(Instance, instance_id), ALL_KINDS, "retry"
            )

    state.offline = True
    await attempt()
    await attempt()
    state.offline = False
    await attempt()  # back up, which clears the recorded error
    state.offline = True
    await attempt()

    assert _events(sent).count(EVENT_PUSH_FAILED) == 2
