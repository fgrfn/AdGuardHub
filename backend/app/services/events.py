"""In-process pub/sub feeding the SSE stream that the UI subscribes to."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any


class EventBus:
    """Fan-out to every connected SSE client.

    Subscribers get a bounded queue; a client that cannot keep up drops events
    rather than stalling the producer.
    """

    #: Sent to every subscriber by ``close`` so an idle stream ends at once
    #: rather than on its next keep-alive. Its identity is what matters, so it
    #: can never collide with a payload.
    _CLOSED = object()

    def __init__(self, queue_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._queue_size = queue_size
        self._lock = asyncio.Lock()
        self._closing = asyncio.Event()

    async def publish(self, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data}, default=str)
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def close(self) -> None:
        """End every open stream, so shutting down does not wait on one.

        An SSE response is a request that never finishes on its own, and uvicorn
        shuts down gracefully — it waits for the requests still in flight. With
        a browser tab open on the hub, that wait had no end: uvicorn waited for
        the stream, the stream waited for the next event, and systemd killed the
        process ninety seconds later. Restarting the service took a minute and a
        half whenever anyone was looking at it, which is exactly when a restart
        happens: an upgrade is started from that very page.

        The sentinel is what makes this immediate. Setting the flag alone would
        end the stream on its next keep-alive, up to twenty seconds later; the
        sentinel wakes it now. A subscriber too far behind to take it still sees
        the flag on the next turn of its loop, which by then is running as fast
        as it can drain.
        """
        self._closing.set()
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(self._CLOSED)

    async def subscribe(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while not self._closing.is_set():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    if self._closing.is_set():
                        break
                    yield ": keep-alive\n\n"
                    continue
                if payload is self._CLOSED:
                    break
                yield f"data: {payload}\n\n"
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


bus = EventBus()
