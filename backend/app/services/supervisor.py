"""Keeping the background workers alive, and saying so when one is not.

The hub's three timers — retry queue, reconciliation, query log — were started
with a bare ``create_task`` and then trusted. Each of them catches ``Exception``
around its own body, which covers the pass that goes wrong and leaves the loop
running. What it does not cover is the loop itself ending: ``BaseException`` is
not ``Exception``, so a ``CancelledError`` that is not a shutdown ends the
coroutine, and a ``return`` from an unexpected branch ends it just as quietly.

Either way the task completes, nothing awaits it until shutdown, and the hub
carries on answering requests exactly as if all three were still running. That is
how a reconciler stopped at 15:50 on a Saturday and was noticed two hours later —
by the dashboard card, which was the only thing in the system that knew.

So a worker that ends for any reason other than the stop event is a fault: it is
logged at ERROR, named, and started again. There is no attempt cap. A worker that
keeps dying is broken and should keep saying so — a cap would restore the silence
this module exists to remove — but the delay grows so that a tight crash loop
costs one line a minute rather than a busy CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: A run at least this long counts as healthy, so the next failure starts the
#: backoff from the beginning. Without it a worker that ran for three days would
#: come back with the delay it earned three days ago.
HEALTHY_AFTER = 60.0

#: Backoff per consecutive failure, and its ceiling.
RESTART_DELAY = 5.0
MAX_RESTART_DELAY = 60.0


def _delay(failures: int) -> float:
    return min(MAX_RESTART_DELAY, RESTART_DELAY * failures)


async def supervise(
    name: str, factory: Callable[[], Awaitable[None]], stop: asyncio.Event
) -> None:
    """Run ``factory()`` until ``stop`` is set, restarting it if it ends early.

    ``factory`` rather than a coroutine, because a coroutine can only be awaited
    once and the whole point here is to await it again.

    Cancellation is the one ending that is not a fault — but only when the stop
    event is set, which is what shutdown does before it cancels anything. A
    cancellation without that is somebody cancelling a worker nobody meant to
    cancel, and it is reported like any other unexpected ending.
    """
    failures = 0
    while not stop.is_set():
        started = asyncio.get_running_loop().time()
        try:
            await factory()
        except asyncio.CancelledError:
            if stop.is_set():
                # Shutdown. Re-raised rather than swallowed: the caller gathers
                # these tasks and a worker that refuses to be cancelled would
                # hold the shutdown it was asked to join.
                raise
            logger.error("Worker %s was cancelled without a shutdown; restarting it", name)
        except Exception:
            logger.exception("Worker %s stopped with an error; restarting it", name)
        else:
            if stop.is_set():
                return
            # It returned on its own, without being asked to. Its loop condition
            # is the stop event, so there is no branch where this is correct.
            logger.error("Worker %s returned unexpectedly; restarting it", name)

        if stop.is_set():
            return

        ran_for = asyncio.get_running_loop().time() - started
        failures = 1 if ran_for >= HEALTHY_AFTER else failures + 1
        # Waited on the stop event rather than slept, so a shutdown during the
        # backoff does not have to wait it out. Timing out *is* the delay
        # elapsing; either way the loop condition decides what happens next.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_delay(failures))
