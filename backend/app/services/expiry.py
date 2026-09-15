"""Rules that clean up after themselves.

Most of a real hub's rule set is archaeology. Something broke, a domain was
allowed from the query log to find out whether that was the cause, it worked, and
the allow stayed — because nobody goes back to a thing that is working again. An
allow rule kept past its purpose is a hole in the filtering that nobody remembers
opening, and the reason they are hard to clean up later is that by then nothing
says which ones were meant to be temporary.

So a rule can be given an expiry when it is written, and the hub removes it when
it falls due. The removal is an ordinary edit: the rule is deleted, a version is
recorded, and the new rule set is pushed to every instance the way any other
deletion is. Nothing about it is special except that nobody had to remember.

Two things are deliberately *not* here:

* **No notification.** An expiry firing is the plan working. A message every time
  a thirty-minute allow lapses is the kind of traffic that makes people stop
  reading the ones that matter. It is logged, and it appears in the history.
* **No renewal, no grace.** A rule that is still needed is re-added, which takes
  one press and restates the decision. A silent extension would rebuild exactly
  the problem this removes.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import session_scope
from ..models import PayloadKind, Rule, utcnow
from .sync import schedule_sync
from .versions import record as record_version

logger = logging.getLogger(__name__)

#: How often the hub looks for rules that have fallen due.
#:
#: A minute, because the shortest offer is fifteen and a thirty-minute allow that
#: lapses at thirty-one is still thirty minutes. Cheaper than it sounds: the
#: query is indexed by a NULL check that nearly every row satisfies, and a hub
#: with no expiring rules does nothing at all.
SWEEP_INTERVAL = 60.0

RULE_KINDS: tuple[PayloadKind, ...] = (PayloadKind.rules,)


async def due(session: AsyncSession) -> list[Rule]:
    """Rules whose expiry has passed, oldest first."""
    result = await session.execute(
        select(Rule)
        .where(Rule.expires_at.is_not(None))
        # Naive on both sides: SQLite drops the offset on write, so a stored
        # timestamp comes back without one and cannot be compared to an aware
        # value at all. See schemas._stamp_utc for the other half of this.
        .where(Rule.expires_at <= utcnow().replace(tzinfo=None))
        .order_by(Rule.id.asc())
    )
    return list(result.scalars().all())


async def sweep(session: AsyncSession) -> list[str]:
    """Delete every rule that has fallen due. Returns what was removed.

    The push is scheduled once for the whole batch rather than per rule: the rule
    set is pushed whole anyway, so three expiries in the same minute are one
    change to every node, not three.
    """
    expired = await due(session)
    if not expired:
        return []

    removed = [rule.text for rule in expired]
    for rule in expired:
        await session.delete(rule)
    await session.commit()

    label = (
        f"expired: {removed[0]}"
        if len(removed) == 1
        else f"{len(removed)} rule(s) expired"
    )
    await record_version(session, label, author="<expiry>")
    logger.info("Removed %d expired rule(s): %s", len(removed), ", ".join(removed))
    schedule_sync(RULE_KINDS, label)
    return removed


async def expiry_worker(stop: asyncio.Event) -> None:  # pragma: no cover - background loop
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL)
            return
        except TimeoutError:
            pass
        try:
            async with session_scope() as session:
                await sweep(session)
        except Exception:
            logger.exception("Expired-rule sweep failed")
