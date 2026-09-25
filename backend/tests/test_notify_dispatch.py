"""How a notification reaches several targets at once.

The callers of ``notify`` are holding things. A push notifies from inside the
lock that serialises pushes to that node, so whatever the dispatch costs is
charged to every other push to the same node waiting behind it. Three targets
tried one after another stacked three timeouts there, for no gain: the targets
know nothing about each other and nothing about the order they are tried in.

The second test is about the bookkeeping that the parallel version has to get
right and the sequential one could not get wrong: each target's own result has to
end up on that target, rather than on whichever one happened to be next.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import NotifierTarget
from app.services import notify as notify_module


async def add_target(client: httpx.AsyncClient, name: str) -> int:
    response = await client.post(
        "/api/settings/notifiers",
        json={"name": name, "type": "gotify", "url": f"http://{name}.test/message"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


async def stored_errors() -> dict[str, str]:
    async with session_scope() as session:
        rows = (await session.execute(select(NotifierTarget))).scalars().all()
        return {row.name: row.last_error for row in rows}


async def test_every_target_is_tried_at_once(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted by overlap rather than by elapsed time, so it cannot pass slowly."""
    for name in ("one", "two", "three"):
        await add_target(auth_client, name)

    in_flight = 0
    peak = 0

    async def slow(client, target, event, title, message) -> str:  # noqa: ANN001
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return ""

    monkeypatch.setattr(notify_module, "send_to_target", slow)

    await notify_module.notify("push.failed", "title", "message")

    assert peak == 3, f"the three targets were tried {peak} at a time"


async def test_each_targets_own_failure_is_recorded_against_it(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One broken webhook must not take the others down, nor be blamed on them."""
    for name in ("good", "broken", "alsogood"):
        await add_target(auth_client, name)

    async def selective(client, target, event, title, message) -> str:  # noqa: ANN001
        return "HTTP 500: it is having a day" if target.name == "broken" else ""

    monkeypatch.setattr(notify_module, "send_to_target", selective)

    await notify_module.notify("push.failed", "title", "message")

    assert await stored_errors() == {
        "good": "",
        "broken": "HTTP 500: it is having a day",
        "alsogood": "",
    }
