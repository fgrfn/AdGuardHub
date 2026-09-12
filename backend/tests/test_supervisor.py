"""A background worker that ends must be loud, and must come back.

The hub's three timers were started with a bare ``create_task`` and then trusted.
Each catches ``Exception`` around its own body — the pass that goes wrong, the
node that misbehaves — which is why the loops survived three years of those. What
none of them covered is the loop itself ending: ``BaseException`` is not
``Exception``, so a cancellation that is not a shutdown ends the coroutine, and
nothing awaited it until shutdown to notice.

The hub then went on answering requests as though all three were running. That is
the failure these pin: not "a pass failed" — that was always handled — but "the
worker is gone and the application looks fine".
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.services import supervisor
from app.services.supervisor import supervise


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch) -> None:
    """The backoff is real behaviour, but not what these tests are about."""
    monkeypatch.setattr(supervisor, "RESTART_DELAY", 0.0)
    monkeypatch.setattr(supervisor, "MAX_RESTART_DELAY", 0.0)


async def run_until(stop: asyncio.Event, task: asyncio.Task, *, calls: list, wanted: int) -> None:
    """Let the supervisor work until the worker has been entered ``wanted`` times."""
    for _ in range(200):
        if len(calls) >= wanted:
            break
        await asyncio.sleep(0)
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_worker_that_raises_is_restarted(caplog) -> None:
    stop = asyncio.Event()
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(1)
        raise RuntimeError("the node said something unexpected")

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(supervise("reconcile", flaky, stop))
        await run_until(stop, task, calls=calls, wanted=3)

    assert len(calls) >= 3
    assert "Worker reconcile stopped with an error" in caplog.text


async def test_a_worker_that_returns_early_is_restarted_and_named(caplog) -> None:
    """The quiet ending. Its loop condition is the stop event, so returning
    while that event is unset is never correct — and it used to be invisible."""
    stop = asyncio.Event()
    calls: list[int] = []

    async def gives_up() -> None:
        calls.append(1)

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(supervise("querylog", gives_up, stop))
        await run_until(stop, task, calls=calls, wanted=3)

    assert len(calls) >= 3
    assert "Worker querylog returned unexpectedly" in caplog.text


async def test_a_cancellation_that_is_not_a_shutdown_is_reported_and_survived(caplog) -> None:
    """The ending that `except Exception` cannot see.

    ``CancelledError`` is a ``BaseException``, so every worker's own handler let
    it through. This is the shape of the fault that stopped a reconciler with no
    log line anywhere in the hub.
    """
    stop = asyncio.Event()
    calls: list[int] = []

    async def cancelled_from_outside() -> None:
        calls.append(1)
        raise asyncio.CancelledError

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(supervise("retry", cancelled_from_outside, stop))
        await run_until(stop, task, calls=calls, wanted=2)

    assert len(calls) >= 2
    assert "Worker retry was cancelled without a shutdown" in caplog.text


async def test_shutdown_stops_it_without_calling_it_a_fault(caplog) -> None:
    """The one ending that is not a fault: stop set, then cancelled — which is
    exactly what the lifespan does, in that order."""
    stop = asyncio.Event()
    entered = asyncio.Event()

    async def patient() -> None:
        entered.set()
        await stop.wait()

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(supervise("reconcile", patient, stop))
        await entered.wait()
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    assert task.done() and not task.cancelled()
    assert caplog.text == ""


async def test_a_cancel_during_shutdown_is_not_swallowed() -> None:
    """The supervisor must not outlive the shutdown that cancels it.

    Refusing the cancellation would hold `asyncio.gather` in the lifespan's
    finally block — trading a dead worker for a hub that will not stop.
    """
    stop = asyncio.Event()
    entered = asyncio.Event()

    async def blocks_forever() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise("querylog", blocks_forever, stop))
    await entered.wait()
    stop.set()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


async def test_a_long_healthy_run_starts_the_backoff_over(monkeypatch) -> None:
    """Otherwise a worker that ran for three days comes back with the delay it
    earned three days ago."""
    monkeypatch.setattr(supervisor, "RESTART_DELAY", 5.0)
    monkeypatch.setattr(supervisor, "MAX_RESTART_DELAY", 60.0)
    assert supervisor._delay(1) == 5.0
    assert supervisor._delay(4) == 20.0
    # The ceiling holds: a worker dying in a tight loop costs one line a minute.
    assert supervisor._delay(100) == 60.0
