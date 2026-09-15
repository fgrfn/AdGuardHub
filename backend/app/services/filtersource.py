"""Which list blocked this — the question a filtering hub exists to answer.

A query log row says *what* matched (`||ads.example.com^`) and never *where it
came from*. With twenty subscriptions on a node that is the difference between
"the banking app is broken" and "turn off HaGeZi's Threat Intelligence Feeds, or
add one exception". The operator can read the rule and still have no idea which
of their lists to go and fix.

AdGuard does send the answer. Every entry's ``rules[0]`` carries a
``filter_list_id`` beside the text, and the adapter simply never read it.

Turning that number into a name is the part only the hub can do, and the reason
is worth stating because it is also the trap: **the id belongs to the node, not
to the subscription.** AdGuard assigns it from a clock-seeded counter when the
list is added, so the same URL is a different number on node-1 and node-2, and a
list removed and re-added changes its own id. A single global map would therefore
be confidently wrong — the aggregated log mixes rows from every node, and each
row has to be read against the node that wrote it.

Built-in ids are the exception: they are constants in AdGuard's source, the same
everywhere, and they need no lookup at all.
"""

from __future__ import annotations

import logging
import time

from ..adapters.base import RemoteFilterList

logger = logging.getLogger(__name__)

#: AdGuard's own reserved ids, from internal/filtering/rulelist (verified against
#: v0.107.79). ``0`` is the custom rule set — which is exactly the set AdGuardHub
#: owns, so "a rule you wrote here" is an answer the hub can give with certainty.
BUILT_IN: dict[int, str] = {
    0: "Your own rules",
    -1: "The node's hosts file",
    -2: "Blocked services",
    -3: "Parental control",
    -4: "Safe browsing",
    -5: "Safe search",
}

#: How long a node's id→name map is trusted before it is fetched again.
#:
#: Long, because the map only changes when a subscription is added or removed —
#: which the hub does itself, and which is rare. An unknown id refreshes the map
#: on demand anyway (see ``needs_refresh``), so this interval is the floor on how
#: often the *unchanged* case costs a request, not the lag on a change.
CACHE_TTL = 900.0

#: The shortest gap between two on-demand refreshes for one node. Without it, a
#: node citing an id the hub cannot explain — a list added in its native UI, say,
#: which reconciliation is about to remove anyway — would fetch the whole filter
#: status on every poll, five seconds apart, for as long as that row kept
#: arriving.
MIN_REFRESH_GAP = 60.0


class FilterSources:
    """Per-node maps of AdGuard's filter ids to the names an operator reads."""

    def __init__(self) -> None:
        self._names: dict[int, dict[int, str]] = {}
        self._fetched_at: dict[int, float] = {}
        self._attempted_at: dict[int, float] = {}

    def remember(self, instance_id: int, lists: list[RemoteFilterList]) -> None:
        """Record what a node's subscriptions are currently numbered."""
        self._names[instance_id] = {
            item.remote_id: item.name or item.url for item in lists if item.remote_id
        }
        self._fetched_at[instance_id] = time.monotonic()

    def forget(self, instance_id: int) -> None:
        """Drop a node's map — it is about to be re-read, or the node is gone."""
        self._names.pop(instance_id, None)
        self._fetched_at.pop(instance_id, None)
        self._attempted_at.pop(instance_id, None)

    def reset(self) -> None:
        self._names.clear()
        self._fetched_at.clear()
        self._attempted_at.clear()

    def label(self, instance_id: int, filter_list_id: int | None) -> str:
        """The list's name, or "" when the hub cannot honestly name one.

        Empty rather than a guess: "unknown list" in the column that exists to
        tell you which list to edit is worse than no column, because it reads as
        an answer.
        """
        if filter_list_id is None:
            return ""
        if filter_list_id in BUILT_IN:
            return BUILT_IN[filter_list_id]
        return self._names.get(instance_id, {}).get(filter_list_id, "")

    def needs_refresh(self, instance_id: int, seen_ids: set[int]) -> bool:
        """Whether this node's map should be fetched again before the next poll.

        Two reasons, and the second is what makes the long TTL affordable: the
        map is stale, or a row cited an id it cannot explain. A subscription the
        hub has just added is the ordinary case of the second — the operator
        should not have to wait out the TTL to see its name.
        """
        now = time.monotonic()
        if now - self._attempted_at.get(instance_id, 0.0) < MIN_REFRESH_GAP:
            return False
        if now - self._fetched_at.get(instance_id, 0.0) > CACHE_TTL:
            return True
        known = self._names.get(instance_id, {})
        return any(item not in BUILT_IN and item not in known for item in seen_ids)

    def note_attempt(self, instance_id: int) -> None:
        """Mark a refresh as tried, whether or not it worked.

        Recorded separately from ``_fetched_at`` so that a node refusing the
        request is retried on the gap above rather than on every poll.
        """
        self._attempted_at[instance_id] = time.monotonic()


#: Process-wide, like the query log buffer it serves.
sources = FilterSources()


def cited_ids(entries: list[dict[str, object]]) -> set[int]:
    """Every filter id a batch of log entries mentions."""
    found: set[int] = set()
    for entry in entries:
        value = entry.get("filter_list_id")
        if isinstance(value, int):
            found.add(value)
    return found
