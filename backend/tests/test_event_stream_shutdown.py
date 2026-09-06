"""An event stream has to end when the hub does.

Server-sent events are a request that never finishes on its own, and uvicorn
shuts down gracefully — it waits for the requests still in flight. With a
browser tab open on the hub that wait had no end, so systemd killed the process
ninety seconds later and every upgrade started from the interface took a minute
and a half.

These are all written with a timeout: the failure being guarded against is a
coroutine that never returns, and a test for it that hangs is no better than
the bug.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.services.events import EventBus

#: Long enough not to trip on a slow machine, short enough that a regression
#: fails the suite instead of stalling it.
PATIENCE = 5.0


async def drain(bus: EventBus, seen: list[str], ready: asyncio.Event) -> None:
    ready.set()
    async for chunk in bus.subscribe():
        seen.append(chunk)


async def finish(reader: asyncio.Task) -> None:
    """Wait for the stream to end, and fail — never hang — if it does not.

    The regression these guard against is a coroutine that never returns. Left
    to `wait_for` alone the reader is cancelled but its generator is still
    unwinding when the loop closes, and the suite stalls on it: a test for a
    hang that hangs reports nothing at all. So the timeout is turned into a
    failure with a sentence, and the task is cleaned up either way.
    """
    try:
        await asyncio.wait_for(asyncio.shield(reader), timeout=PATIENCE)
    except TimeoutError:
        pytest.fail("the event stream did not end after the bus was closed")
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


async def test_closing_the_bus_ends_an_idle_stream() -> None:
    """The case that held up every restart: a stream with nothing to say."""
    bus = EventBus()
    seen: list[str] = []
    ready = asyncio.Event()
    reader = asyncio.create_task(drain(bus, seen, ready))
    await ready.wait()
    # Let subscribe() reach its first await, so the subscriber is registered.
    await asyncio.sleep(0)

    await bus.close()

    await finish(reader)
    assert seen == []


async def test_a_stream_with_events_in_flight_still_ends() -> None:
    """Closing must not depend on the queue happening to be empty."""
    bus = EventBus()
    seen: list[str] = []
    ready = asyncio.Event()
    reader = asyncio.create_task(drain(bus, seen, ready))
    await ready.wait()
    await asyncio.sleep(0)

    await bus.publish("instance.status", {"id": 1})
    await bus.close()

    await finish(reader)


async def test_a_subscriber_too_far_behind_to_take_the_sentinel_still_ends() -> None:
    """A full queue cannot accept the wake-up, so the flag has to carry it.

    Without the flag this is the one subscriber that would hang forever: the
    sentinel is dropped on a full queue, and nothing else would ever tell it to
    stop.
    """
    bus = EventBus(queue_size=1)
    ready = asyncio.Event()
    seen: list[str] = []

    # Fill the queue before anything is read, so close() cannot enqueue.
    reader = asyncio.create_task(drain(bus, seen, ready))
    await ready.wait()
    await asyncio.sleep(0)
    await bus.publish("a", {})
    await bus.publish("b", {})  # dropped: the queue is full

    await bus.close()

    await finish(reader)


async def test_closing_forgets_its_subscribers() -> None:
    """A stream that ended must not keep the bus publishing into a dead queue."""
    bus = EventBus()
    ready = asyncio.Event()
    seen: list[str] = []
    reader = asyncio.create_task(drain(bus, seen, ready))
    await ready.wait()
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1

    await bus.close()
    await finish(reader)

    assert bus.subscriber_count == 0


async def test_subscribing_after_a_close_does_not_hang() -> None:
    """The shutdown ordering is not guaranteed: a request can arrive late."""
    bus = EventBus()
    await bus.close()

    async def collect() -> list[str]:
        return [chunk async for chunk in bus.subscribe()]

    # Consumed under a timeout like every other stream here. Iterating it
    # directly is what the name warns about: with no task to cancel, a
    # regression would stall the suite instead of failing this test.
    try:
        seen = await asyncio.wait_for(collect(), timeout=PATIENCE)
    except TimeoutError:
        pytest.fail("subscribing after the bus closed never ended")

    assert seen == []


@pytest.mark.parametrize("size", [1, 200])
async def test_a_live_stream_still_delivers(size: int) -> None:
    """The guard must not cost the feature it guards."""
    bus = EventBus(queue_size=size)
    ready = asyncio.Event()
    seen: list[str] = []
    reader = asyncio.create_task(drain(bus, seen, ready))
    await ready.wait()
    await asyncio.sleep(0)

    await bus.publish("drift.found", {"instance": "node-1"})
    await asyncio.sleep(0)
    await bus.close()
    await finish(reader)

    assert any("drift.found" in chunk for chunk in seen)
