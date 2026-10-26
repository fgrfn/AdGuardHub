"""Draining the background pushes, and the spin that cost a CI run two minutes.

``schedule_sync`` keeps its tasks in a process-global set and removes each one
through ``add_done_callback(discard)``. That callback is scheduled on the loop
which created the task — so a loop torn down in the moment the task finishes
never runs it, and the finished task stays in the set for the rest of the
process. Every test gets its own loop, so "the rest of the process" means every
later test.

``drain_background`` used to loop on ``while _background_tasks``, which such a
leftover never clears: ``gather`` returns immediately because the task is already
done, round it goes, for ever. What makes it worse than a slow test is that
nothing outside can stop it — the spin never yields long enough for a timer to
fire, so even an ``asyncio.wait_for`` around the call does not time out. Only the
suite's thread-based timeout ends it, 120 seconds later, in a test whose own work
had finished.

Each test here carries a short timeout of its own: without the fix they do not
fail, they hang, and the point of a regression test is to say what is wrong
rather than to stop the run.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.sync import _background_tasks, drain_background


async def nothing() -> None:
    return None


@pytest.mark.timeout(15)
async def test_a_finished_task_nobody_discarded_does_not_hang_the_drain() -> None:
    """The failure as CI met it: the set is not empty and never will be.

    The leftover is built by hand rather than by racing a loop shutdown, because
    the race is only how such a task *arrives*. What hangs the drain is the state
    it leaves: a task that is done, in the set, with nothing left to remove it.
    """
    leftover = asyncio.create_task(nothing())
    await leftover
    _background_tasks.add(leftover)
    assert leftover.done(), "the premise: it finished, it was simply never removed"

    await drain_background()

    assert _background_tasks == set(), "and the drain is what clears it"


@pytest.mark.timeout(15)
async def test_the_drain_still_waits_for_work_that_has_not_finished() -> None:
    """The guard against fixing the spin by draining nothing at all."""
    finished = False

    async def slow() -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    task = asyncio.create_task(slow())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    await drain_background()

    assert finished, "a pending task has to be awaited, not dropped"
    assert _background_tasks == set()


@pytest.mark.timeout(15)
async def test_a_task_scheduled_while_draining_is_waited_for_too() -> None:
    """A push that schedules another one must not escape the drain.

    This is why the loop goes round again rather than gathering once.
    """
    second_ran = False

    async def second() -> None:
        nonlocal second_ran
        await asyncio.sleep(0)
        second_ran = True

    async def first() -> None:
        await asyncio.sleep(0)
        task = asyncio.create_task(second())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    task = asyncio.create_task(first())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    await drain_background()

    assert second_ran
    assert _background_tasks == set()
