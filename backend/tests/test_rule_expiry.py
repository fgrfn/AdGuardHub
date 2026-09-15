"""Rules that clean up after themselves.

Most of a real hub's rule set is archaeology. Something broke, a domain was
allowed from the query log to find out whether that was the cause, it worked, and
the allow stayed — because nobody goes back to a thing that is working again. An
allow rule kept past its purpose is a hole in the filtering nobody remembers
opening, and what makes them hard to clean up later is that by then nothing says
which were meant to be temporary.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select

from app.db import session_scope
from app.models import Rule, utcnow
from app.services.expiry import sweep
from app.services.sync import desired_rules, drain_background

from .fakes import FakeAdapter
from .test_sync import A, add_instance


async def age_out(text: str) -> None:
    """Move a rule's expiry into the past, as the clock would."""
    async with session_scope() as session:
        rule = (await session.execute(select(Rule).where(Rule.text == text))).scalars().one()
        rule.expires_at = utcnow().replace(tzinfo=None) - timedelta(seconds=1)
        await session.commit()


async def rule_texts(client: httpx.AsyncClient) -> list[str]:
    return [row["text"] for row in (await client.get("/api/rules")).json()]


async def run_sweep() -> list[str]:
    async with session_scope() as session:
        removed = await sweep(session)
    await drain_background()
    return removed


# --------------------------------------------------------------------------
# Writing one
# --------------------------------------------------------------------------


async def test_a_domain_can_be_allowed_for_a_while(auth_client: httpx.AsyncClient) -> None:
    """The query log's reason for existing: allow it to find out whether that was
    the cause, without the allow outliving the question."""
    response = await auth_client.post(
        "/api/rules/allow?origin=querylog",
        json={"domain": "doorbell.example.com", "expires_in_minutes": 30},
    )

    assert response.status_code == 200
    assert response.json()["expires_at"] is not None


async def test_a_rule_without_a_duration_stands(auth_client: httpx.AsyncClient) -> None:
    """Which is nearly all of them, and must stay the default."""
    created = await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})

    assert created.json()["expires_at"] is None


async def test_asking_again_restarts_the_countdown(auth_client: httpx.AsyncClient) -> None:
    """"Allow this for 30 minutes" means thirty minutes from now.

    Re-allowing a domain that is already allowed is how somebody extends it, and
    answering "you already did that" would leave them watching a rule they just
    renewed expire on the old schedule.
    """
    first = await auth_client.post(
        "/api/rules/allow", json={"domain": "doorbell.example.com", "expires_in_minutes": 1}
    )
    second = await auth_client.post(
        "/api/rules/allow", json={"domain": "doorbell.example.com", "expires_in_minutes": 600}
    )

    assert second.json()["id"] == first.json()["id"], "the same rule, not a second one"
    assert second.json()["expires_at"] > first.json()["expires_at"]


async def test_a_temporary_rule_can_be_made_permanent(auth_client: httpx.AsyncClient) -> None:
    """0 rather than null, because "leave it as it is" is what an omitted field
    means everywhere else in this API."""
    created = await auth_client.post(
        "/api/rules", json={"text": "@@||doorbell.example.com^", "expires_in_minutes": 30}
    )
    rule_id = created.json()["id"]

    patched = await auth_client.patch(f"/api/rules/{rule_id}", json={"expires_in_minutes": 0})

    assert patched.json()["expires_at"] is None


async def test_an_absurd_duration_is_refused(auth_client: httpx.AsyncClient) -> None:
    """Beyond a week it is a rule somebody means to keep, and should be one."""
    response = await auth_client.post(
        "/api/rules", json={"text": "@@||x.example.com^", "expires_in_minutes": 60 * 24 * 400}
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Removing it again
# --------------------------------------------------------------------------


async def test_an_expired_rule_is_removed_and_the_nodes_told(
    auth_client: httpx.AsyncClient,
) -> None:
    """The whole point: the cleanup is an ordinary edit that nobody had to
    remember, and it reaches the nodes like any other deletion."""
    await add_instance(auth_client, "a", A)
    await auth_client.post(
        "/api/rules/allow", json={"domain": "doorbell.example.com", "expires_in_minutes": 30}
    )
    await drain_background()
    assert FakeAdapter.state_for(A).rules == ["@@||doorbell.example.com^"]

    await age_out("@@||doorbell.example.com^")
    removed = await run_sweep()

    assert removed == ["@@||doorbell.example.com^"]
    assert await rule_texts(auth_client) == []
    assert FakeAdapter.state_for(A).rules == [], "the node has to be told, not just the hub"


async def test_a_rule_that_has_not_fallen_due_is_left_alone(
    auth_client: httpx.AsyncClient,
) -> None:
    await auth_client.post(
        "/api/rules/allow", json={"domain": "doorbell.example.com", "expires_in_minutes": 600}
    )

    assert await run_sweep() == []
    assert await rule_texts(auth_client) == ["@@||doorbell.example.com^"]


async def test_a_permanent_rule_is_never_swept(auth_client: httpx.AsyncClient) -> None:
    """The one thing this must never do."""
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})

    assert await run_sweep() == []
    assert await rule_texts(auth_client) == ["||ads.example.com^"]


async def test_several_expiries_in_one_minute_are_one_push(
    auth_client: httpx.AsyncClient,
) -> None:
    """The rule set is pushed whole, so three expiries are one change to a node."""
    await add_instance(auth_client, "a", A)
    for domain in ("one.example.com", "two.example.com", "three.example.com"):
        await auth_client.post(
            "/api/rules/allow", json={"domain": domain, "expires_in_minutes": 30}
        )
    await drain_background()
    for domain in ("one.example.com", "two.example.com", "three.example.com"):
        await age_out(f"@@||{domain}^")

    before = FakeAdapter.state_for(A).push_calls
    removed = await run_sweep()

    assert len(removed) == 3
    assert FakeAdapter.state_for(A).push_calls - before == 1
    assert FakeAdapter.state_for(A).rules == []


async def test_an_expired_rule_is_not_pushed_back_before_the_sweep_runs(fresh_db) -> None:
    """The window between falling due and being swept, and why the filter exists.

    Reconciliation reads the desired state on its own timer. If that still
    contained a rule that had fallen due, a pass landing in the gap would push it
    *back* onto every node — an allow rule reopening a hole by itself, which is
    the exact failure this feature is meant to close.
    """
    async with session_scope() as session:
        session.add(
            Rule(
                text="@@||doorbell.example.com^",
                kind="allow",
                origin="querylog",
                expires_at=utcnow().replace(tzinfo=None) - timedelta(seconds=1),
            )
        )
        session.add(Rule(text="||ads.example.com^", kind="block", origin="custom"))
        await session.commit()

        assert await desired_rules(session) == ["||ads.example.com^"]


async def test_the_removal_is_recorded_in_the_history(auth_client: httpx.AsyncClient) -> None:
    """It is an edit like any other, so it has to be undoable like any other."""
    await auth_client.post(
        "/api/rules/allow", json={"domain": "doorbell.example.com", "expires_in_minutes": 30}
    )
    await age_out("@@||doorbell.example.com^")

    await run_sweep()

    labels = [row["label"] for row in (await auth_client.get("/api/versions")).json()]
    assert any("expired" in label for label in labels)
