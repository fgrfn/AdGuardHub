"""How often a finding has happened, and how long the attempt took.

The drift log states a refusal once and then holds the repeats back, because a
node that will not keep something goes on not keeping it and a row per pass
buries the log. Held back used to mean thrown away, and that cost the two
numbers an operator actually asks for: the standing row's timestamp is when the
fault was *first* seen, so a fault still happening every five minutes and one
that stopped four hours ago read identically.

Now the repeats are counted on that row instead of dropped, and each row carries
how long its correction attempt took — which is the number that would have named
the ``add_url`` timeout in one glance instead of over weeks.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select, text

from app import db
from app.db import session_scope
from app.models import DriftEvent
from app.services import reconcile as reconcile_service
from app.services.sync import drain_background

from .fakes import FakeAdapter
from .test_sync import A, add_instance


async def rows() -> list[DriftEvent]:
    async with session_scope() as session:
        result = await session.execute(select(DriftEvent).order_by(DriftEvent.id.asc()))
        return list(result.scalars().all())


async def reconcile(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/reconcile")).status_code == 200


async def refusing_node(client: httpx.AsyncClient) -> None:
    """A node that answers, takes the push, and does not keep the rule."""
    await add_instance(client, "a", A)
    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^"}
    await client.post("/api/rules/allow", json={"domain": "hitmyl.ink"})
    await drain_background()


async def test_a_repeated_refusal_is_still_one_row(
    auth_client: httpx.AsyncClient,
) -> None:
    """The reason the suppression exists, and it has to keep holding.

    Five hundred rows saying the same thing is how a log stops being read.
    """
    await refusing_node(auth_client)

    await reconcile(auth_client)
    await reconcile(auth_client)
    await reconcile(auth_client)

    refusals = [row for row in await rows() if "did not keep" in row.summary]
    assert len(refusals) == 1


async def test_the_repeats_are_counted_rather_than_dropped(
    auth_client: httpx.AsyncClient,
) -> None:
    """Three passes found it three times, and the row says so."""
    await refusing_node(auth_client)

    await reconcile(auth_client)
    await reconcile(auth_client)
    await reconcile(auth_client)

    refusal = next(row for row in await rows() if "did not keep" in row.summary)
    assert refusal.occurrences == 3


async def test_the_row_keeps_its_first_timestamp_and_gains_a_last_one(
    auth_client: httpx.AsyncClient,
) -> None:
    """Two different questions: since when, and is it still happening.

    ``created_at`` answers the first and must not move — re-stamping it would
    lose the age, which is the number that makes someone act.
    """
    await refusing_node(auth_client)

    await reconcile(auth_client)
    first = next(row for row in await rows() if "did not keep" in row.summary)
    assert first.last_seen_at is None  # nothing has repeated yet
    created = first.created_at

    await reconcile(auth_client)

    again = next(row for row in await rows() if "did not keep" in row.summary)
    assert again.created_at == created
    assert again.last_seen_at is not None


async def test_a_finding_that_changes_starts_a_new_row(
    auth_client: httpx.AsyncClient,
) -> None:
    """Counting is for the same finding, not for the same payload kind.

    A refusal that turns into a different refusal is news, and appending it is
    the whole reason the suppression is scoped to an identical summary.
    """
    await refusing_node(auth_client)
    await reconcile(auth_client)

    FakeAdapter.state_for(A).refuses = {"@@||hitmyl.ink^", "@@||other.example^"}
    await auth_client.post("/api/rules/allow", json={"domain": "other.example"})
    await drain_background()
    await reconcile(auth_client)

    refusals = [row for row in await rows() if "did not keep" in row.summary]
    assert len(refusals) == 2
    assert refusals[0].occurrences == 1


async def test_the_duration_is_the_attempt_and_not_a_placeholder(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded number has to come from the clock, not from a constant.

    A fake adapter over no network answers in well under a millisecond, so the
    honest way to prove the measurement is wired to the thing being measured is
    to make the push slow on purpose and see the row say so. Delayed inside
    ``push_kind``, which is where the real seconds go on a node fetching a list.
    """
    slow = reconcile_service.push_kind

    async def delayed(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0.05)
        return await slow(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reconcile_service, "push_kind", delayed)

    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await drain_background()
    FakeAdapter.state_for(A).rules = []  # changed on the node directly

    await reconcile(auth_client)

    drift = next(row for row in await rows() if row.payload_kind == "rules")
    assert drift.corrected
    # 50 ms was slept; 40 is the same claim with room for a slow runner.
    assert drift.took_ms >= 40


async def test_the_duration_follows_the_latest_attempt(
    auth_client: httpx.AsyncClient,
) -> None:
    """A counted repeat refreshes the timing rather than keeping the first one.

    The count and the first timestamp are history; the duration is a measurement
    of what is happening now, and a stale one would mislead exactly when the
    node starts getting slower.
    """
    await refusing_node(auth_client)
    await reconcile(auth_client)

    async with session_scope() as session:
        row = (
            await session.execute(select(DriftEvent).order_by(DriftEvent.id.desc()).limit(1))
        ).scalars().one()
        row.took_ms = 999999  # a value no local attempt could produce
        await session.commit()

    await reconcile(auth_client)

    refusal = next(row for row in await rows() if "did not keep" in row.summary)
    assert refusal.took_ms != 999999
    assert refusal.occurrences == 2


def test_the_log_line_renders_a_duration_in_a_unit_a_person_reads() -> None:
    """`pass 10200 ms` makes the reader do the arithmetic.

    And they are doing it at the exact moment they are trying to notice a round
    number — ten seconds on the nose is what names a timeout as the cause.
    """
    assert reconcile_service.human_ms(84) == "84 ms"
    assert reconcile_service.human_ms(999) == "999 ms"
    assert reconcile_service.human_ms(10_000) == "10.0 s"
    assert reconcile_service.human_ms(125_400) == "2 min 5 s"


async def test_the_api_serves_all_three(auth_client: httpx.AsyncClient) -> None:
    """The interface cannot show what the API does not send."""
    await refusing_node(auth_client)
    await reconcile(auth_client)
    await reconcile(auth_client)

    entry = next(
        row for row in (await auth_client.get("/api/drift")).json()
        if "did not keep" in row["summary"]
    )

    assert entry["occurrences"] == 2
    assert entry["last_seen_at"] is not None
    assert "took_ms" in entry


async def test_an_upgraded_database_gets_a_count_of_one_not_a_null(fresh_db: None) -> None:
    """The rows that existed before this column did have to answer "once".

    A NULL here is not a cosmetic difference: the counter is incremented in
    place, so a row whose count came back NULL would raise on the first repeat —
    on exactly the hubs that already have a drift log worth keeping. The column
    is NOT NULL with a default so the additive migration writes a real value; if
    that DEFAULT ever stopped being emitted, this is where it shows.
    """
    engine = db.get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO drift_events (instance_id, instance_name, payload_kind,"
                " summary, details, corrected, occurrences, took_ms, created_at)"
                " VALUES (NULL, 'old-node', 'rules', 'from before the upgrade', '{}',"
                " 0, 1, 0, '2026-01-01')"
            )
        )
        # Stand in for a database written before the columns existed.
        await conn.execute(text("ALTER TABLE drift_events DROP COLUMN occurrences"))
        await conn.execute(text("ALTER TABLE drift_events DROP COLUMN last_seen_at"))

    await db.init_db()

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT occurrences, last_seen_at FROM drift_events")
            )
        ).all()
    assert row == [(1, None)]


async def test_the_diagnostic_bundle_carries_them(auth_client: httpx.AsyncClient) -> None:
    """A bundle is read by somebody who cannot ask a follow-up question.

    Whether a fault is live or historical is the first thing they need, and
    without the count and the last sighting the bundle cannot say.
    """
    await refusing_node(auth_client)
    await reconcile(auth_client)
    await reconcile(auth_client)

    bundle = (await auth_client.get("/api/diagnostics")).json()

    entry = next(row for row in bundle["drift"] if "did not keep" in row["summary"])
    assert entry["occurrences"] == 2
    assert entry["last_seen_at"]
