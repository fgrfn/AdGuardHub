"""How many rules each subscription actually contains.

The hub deliberately does not store the contents of a blocklist — it tracks the
URL and whether it is enabled, and AdGuard Home downloads and parses the file
itself (spec §12). So the hub cannot answer "how big is this list?" from its own
database: the only place that number exists is on the nodes, as the
``rules_count`` AdGuard reports for each subscription it has fetched.

That has two consequences worth stating rather than hiding:

* **It needs a reachable node.** With every instance down, the sizes are simply
  unknown, and the interface says so rather than showing zeros.
* **Nodes can legitimately disagree.** They refresh on their own schedule, so one
  may have yesterday's copy of a list that grew overnight. The highest count is
  reported as the answer — a node that has not fetched a list yet reports 0, and
  a stale copy is smaller than a fresh one, so the maximum is the most recent
  size any node has actually seen — and every node's own number is kept beside
  it so a disagreement can be looked at rather than averaged away.

None of this feeds reconciliation. A count is an observation about a file, not
configuration the hub owns, so a difference in it is never drift.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from ..adapters import AdapterError, build_adapter
from ..db import session_scope
from ..models import FilterList, Instance
from ..runtime import get_crypto

logger = logging.getLogger(__name__)

# Sizes change only when a node refreshes its lists, which AdGuard does on an
# interval measured in hours. Holding the fan-out for a minute keeps an open
# Filter lists tab from polling every node for a number that cannot have moved.
CACHE_TTL = 60.0

_cache: tuple[float, FilterSizes] | None = None
_cache_lock = asyncio.Lock()


@dataclass(slots=True)
class InstanceCount:
    instance_id: int
    instance_name: str
    rules_count: int
    #: When this node last downloaded the list, as the node reports it. Empty
    #: when it never has.
    last_updated: str = ""


@dataclass(slots=True)
class ListSize:
    url: str
    kind: str
    rules_count: int
    per_instance: list[InstanceCount] = field(default_factory=list)
    #: The most recent download any node reports, which is the answer to "is this
    #: list current". Empty when no node has ever fetched it — which is the
    #: interesting case, because AdGuard keeps a subscription it cannot download
    #: rather than dropping it, so a list that has never arrived looks exactly
    #: like one that has, until somebody reads this.
    last_updated: str = ""

    @property
    def agreed(self) -> bool:
        """Whether every node that answered reported the same size."""
        return len({item.rules_count for item in self.per_instance}) <= 1


@dataclass(slots=True)
class FilterSizes:
    lists: list[ListSize] = field(default_factory=list)
    total_rules: int = 0
    instances_reporting: int = 0
    instances_total: int = 0


async def collect() -> FilterSizes:
    """Ask every reachable instance for its subscription sizes and fold them together.

    Only subscriptions the hub knows about are reported: anything else on a node
    is drift for reconciliation to remove, and listing it here would give it a
    permanence it should not have.
    """
    async with session_scope() as session:
        instances = list(
            (
                await session.execute(select(Instance).where(Instance.enabled.is_(True)))
            ).scalars().all()
        )
        known = {
            (item.kind, item.url): item.enabled
            for item in (
                await session.execute(select(FilterList).order_by(FilterList.id.asc()))
            ).scalars().all()
        }

    crypto = get_crypto()
    counts: dict[tuple[str, str], list[InstanceCount]] = {}
    reporting = 0

    for instance in instances:
        adapter = build_adapter(instance, crypto)
        try:
            remote = await adapter.pull_filter_lists()
        except (AdapterError, ValueError) as exc:
            logger.debug("Filter list sizes unavailable from %s: %s", instance.name, exc)
            continue
        finally:
            await adapter.aclose()

        reporting += 1
        for item in remote:
            key = (item.kind, item.url)
            if key not in known:
                continue
            counts.setdefault(key, []).append(
                InstanceCount(instance.id, instance.name, item.rules_count, item.last_updated)
            )

    sizes = [
        ListSize(
            url=url,
            kind=kind,
            rules_count=max(
                (entry.rules_count for entry in counts.get((kind, url), [])), default=0
            ),
            per_instance=counts.get((kind, url), []),
            # Lexicographic max over ISO-8601 timestamps, which orders them
            # correctly as long as they carry the same offset — AdGuard sends
            # every one of these in UTC. The newest for the same reason the
            # largest count is taken: a node that has not fetched the list yet
            # says nothing, and a stale copy is older than a fresh one, so the
            # maximum is the most recent state any node has actually reached.
            last_updated=max(
                (entry.last_updated for entry in counts.get((kind, url), [])), default=""
            ),
        )
        for kind, url in known
    ]

    # "Active" is the hub's enabled state, not a node's: a subscription the hub
    # has switched off is off everywhere as soon as the push lands, and counting
    # it would inflate the total by a list nothing is filtering against.
    total = sum(size.rules_count for size in sizes if known[(size.kind, size.url)])
    return FilterSizes(
        lists=sizes,
        total_rules=total,
        instances_reporting=reporting,
        instances_total=len(instances),
    )


async def cached() -> FilterSizes:
    """``collect()`` behind a TTL, with concurrent callers collapsed onto one fan-out."""
    global _cache
    now = time.monotonic()
    held = _cache
    if held is not None and now - held[0] < CACHE_TTL:
        return held[1]

    async with _cache_lock:
        held = _cache
        if held is not None and time.monotonic() - held[0] < CACHE_TTL:
            return held[1]
        data = await collect()
        _cache = (time.monotonic(), data)
        return data


def invalidate() -> None:
    """Drop the held result, so the next read reflects a changed subscription list."""
    global _cache
    _cache = None


@dataclass(slots=True)
class RefreshResult:
    """What one node did when asked to re-download its subscriptions."""

    instance_id: int
    instance_name: str
    updated: int = 0
    error: str = ""


async def refresh_all() -> list[RefreshResult]:
    """Tell every reachable node to fetch its subscriptions now.

    The hub cannot refresh a list itself — it holds URLs, never contents (spec
    §12) — so "check for updates" means asking each node to do it. Which is why
    this lives here rather than looking like replication: nothing is pushed,
    nothing is compared, and the hub's own state does not change.

    Best effort across the fleet, like every other fan-out (spec §6): a node that
    refuses is named and the rest still go. Each is asked for both kinds, because
    AdGuard refreshes blocklists and allowlists through separate calls and an
    allowlist that never updates is the harder fault to notice.

    A node held in maintenance is skipped. Maintenance means "leave this one
    alone", and a refresh is the hub reaching into it.
    """
    async with session_scope() as session:
        instances = list(
            (
                await session.execute(
                    select(Instance).where(
                        Instance.enabled.is_(True), Instance.maintenance.is_(False)
                    )
                )
            ).scalars().all()
        )

    crypto = get_crypto()
    results: list[RefreshResult] = []
    for instance in instances:
        adapter = build_adapter(instance, crypto)
        result = RefreshResult(instance.id, instance.name)
        try:
            for allowlists in (False, True):
                result.updated += await adapter.refresh_filter_lists(allowlists=allowlists)
        except (AdapterError, ValueError) as exc:
            result.error = str(exc)
            logger.warning("Could not refresh the lists on %s: %s", instance.name, exc)
        finally:
            await adapter.aclose()
        results.append(result)

    if any(not item.error for item in results):
        # The sizes and the last-updated times just changed on at least one node,
        # and the whole point of pressing this is to see that.
        invalidate()
    return results
