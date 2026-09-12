"""A reconciliation pass that hangs must not hang for ever.

Supervision (#108) covers a worker that *ends*: it is logged and restarted. It
cannot cover one that hangs, and that asymmetry is easy to miss — a coroutine
waiting for something that never comes simply never ends, so there is nothing to
notice, nothing to restart, and nothing in any log. It would sit there for weeks
while every page in the hub rendered perfectly.

Nothing inside a pass is known to hang today: the adapter's timeout covers
connect, read, write and pool, and both push locks are context-managed so they
release even on cancellation. This is the backstop for the one that is not
known — and the fault it catches is the one that is hardest to find without it.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from app.services import reconcile
from app.services.reconcile import (
    MIN_PASS_DEADLINE,
    PASS_DEADLINE_FRACTION,
    pass_deadline,
)


def test_a_pass_may_use_most_of_its_interval_but_not_all_of_it() -> None:
    """Two passes overlapping is worse than one cut short: both want the same
    per-node push lock, so the second would block behind the first."""
    assert pass_deadline(900) == 900 * PASS_DEADLINE_FRACTION
    assert pass_deadline(900) < 900


def test_a_short_interval_still_leaves_time_for_real_work() -> None:
    """The interval goes down to 30 s, and 24 of those is shorter than a single
    add_url on a large blocklist. A deadline tighter than the work is a loop
    that never completes."""
    assert pass_deadline(30) == MIN_PASS_DEADLINE
    assert pass_deadline(60) == MIN_PASS_DEADLINE


async def test_a_hanging_pass_is_abandoned_and_named(fresh_db, monkeypatch, caplog) -> None:
    """The worker itself, with a pass that genuinely never returns.

    Driven through `reconcile_worker` rather than through a bare
    `asyncio.timeout`, because what is being tested is that the worker *has* a
    deadline and survives hitting it — not that asyncio works.

    The wording matters as much as the recovery: a pass that failed and a pass
    that never came back need different answers, and letting the generic handler
    log the second as "failed" would hide the only thing worth knowing about it.
    """
    entered = asyncio.Event()

    async def never_returns(session, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(reconcile, "reconcile_all", never_returns)
    monkeypatch.setattr(reconcile, "pass_deadline", lambda interval: 0.05)
    monkeypatch.setattr(
        reconcile.hubsettings,
        "current",
        lambda: SimpleNamespace(reconcile_interval=0.01, reconcile_enabled=True),
    )

    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR):
        worker = asyncio.create_task(reconcile.reconcile_worker(stop))
        await asyncio.wait_for(entered.wait(), timeout=2)
        # Long enough for the deadline to fire and the handler to log it.
        for _ in range(200):
            if "was abandoned" in caplog.text:
                break
            await asyncio.sleep(0.01)
        stop.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert entered.is_set(), "the pass has to have begun for this to prove anything"
    assert "was abandoned" in caplog.text, "a pass that never returns has to be reported"
    assert "never answering" in caplog.text
    # And not as a crash: the distinction is the whole point of the branch.
    assert "Reconciliation pass failed" not in caplog.text


async def test_without_the_deadline_the_worker_never_comes_back(
    fresh_db, monkeypatch
) -> None:
    """The shape of the fault, so the test above cannot pass vacuously.

    With the deadline removed the same worker sits inside one pass for ever: it
    logs nothing, ends nothing, and supervision has nothing to catch.
    """
    entered = asyncio.Event()

    async def never_returns(session, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(reconcile, "reconcile_all", never_returns)
    # An hour: standing in for "no deadline at all".
    monkeypatch.setattr(reconcile, "pass_deadline", lambda interval: 3600.0)
    monkeypatch.setattr(
        reconcile.hubsettings,
        "current",
        lambda: SimpleNamespace(reconcile_interval=0.01, reconcile_enabled=True),
    )

    stop = asyncio.Event()
    worker = asyncio.create_task(reconcile.reconcile_worker(stop))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await asyncio.sleep(0.2)

    assert not worker.done(), "it is stuck, which is exactly the fault"
    stop.set()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
