"""Timestamps must leave the hub saying which zone they are in.

Everything is written with ``utcnow()``, which is aware — but SQLite has no
timestamp type and SQLAlchemy's ``DateTime(timezone=True)`` is a no-op there, so
the offset is dropped on write and the value comes back naive. Pydantic then
serialised `2026-09-12T18:23:36`, and an ISO string without an offset is not
ambiguous in JavaScript: it is *defined* as local time.

A browser in UTC+2 therefore read every timestamp two hours early. The
*Reconciliation* card compares the last pass against the clock to decide whether
the safety net has stopped, so a healthy reconciler reported "may have stopped"
permanently, at any interval below eight hours — and an evening went into looking
for a fault in a worker that was running the whole time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from .test_sync import A, add_instance


def offsets(value: object) -> list[str]:
    """Every timestamp-looking string in a response body, as JSON renders it."""
    found: list[str] = []
    if isinstance(value, str):
        if len(value) >= 19 and value[4] == "-" and value[10] == "T":
            found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(offsets(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(offsets(item))
    return found


async def test_every_timestamp_the_api_sends_carries_its_zone(
    auth_client: httpx.AsyncClient,
) -> None:
    """The property, across the endpoints the dashboard actually reads."""
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    await auth_client.post("/api/reconcile")

    for path in ("/api/instances", "/api/rules", "/api/reconcile/runs", "/api/dashboard"):
        body = (await auth_client.get(path)).json()
        stamps = offsets(body)
        assert stamps, f"{path} returned no timestamps, so this proves nothing"
        for stamp in stamps:
            assert stamp.endswith("Z") or stamp[-6] in "+-", (
                f"{path} sent {stamp!r} with no zone; a browser reads that as local time"
            )


async def test_the_reconciliation_card_can_tell_a_live_reconciler_from_a_stopped_one(
    auth_client: httpx.AsyncClient,
) -> None:
    """The failure in the terms it was reported in.

    A pass that just ran must not look hours old. Parsed as an instant, the gap
    between `last_at` and now has to stay small whatever zone the reader is in —
    which is exactly what a missing offset destroyed.
    """
    await add_instance(auth_client, "a", A)
    await auth_client.post("/api/rules", json={"text": "||ads.example.com^"})
    assert (await auth_client.post("/api/reconcile")).status_code == 200

    row = (await auth_client.get("/api/reconcile/runs")).json()[0]
    last_at = datetime.fromisoformat(row["last_at"])

    assert last_at.tzinfo is not None, "without a zone the client has to guess, and guesses local"
    gap = datetime.now(UTC) - last_at
    # Generous: the point is that it is seconds, not the hours an offset adds.
    assert timedelta(0) <= gap < timedelta(minutes=1), f"a pass that just ran looks {gap} old"
