"""Aggregated, near-real-time query log across all instances (spec §9).

Entries are held in a bounded in-memory buffer and streamed to the UI over SSE. They
are deliberately never written to SQLite: the DB holds configuration only (spec §12).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from ..adapters import AdapterError, build_adapter
from ..config import get_settings
from ..db import session_scope
from ..models import Instance
from ..runtime import get_crypto
from . import filtersource, hubsettings
from .events import bus

logger = logging.getLogger(__name__)


def _entry_key(instance_name: str, entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return (instance_name, entry["time"], entry["question"], entry["client"])


#: AdGuard's reason for a query that one of *our own* allow rules let through.
#: See ``adapters.adguard._ALLOWED_REASONS``: it is one of the values that make
#: ``blocked`` false, which is why an allowlist hit used to be indistinguishable
#: from an ordinary unfiltered query.
WHITELISTED = "NotFilteredWhiteList"

#: What ``status`` accepts. ``""`` is everything.
#:
#: Three values rather than a checkbox because ``blocked`` is a boolean over a
#: field that is not one: the node sends a reason, and collapsing it lost the
#: answer this hub exists to give. A query let through by an allow rule is
#: ``blocked=False`` — identical, in every column, to a query nothing touched —
#: so "which of my allowances are actually firing" could not be asked at all.
STATUSES = ("blocked", "allowlisted", "processed")


def matches_status(entry: dict[str, Any], status: str) -> bool:
    """Whether one entry belongs in the chosen status."""
    if status == "blocked":
        return bool(entry["blocked"])
    if status == "allowlisted":
        return entry.get("answer_status") == WHITELISTED
    if status == "processed":
        # Not filtered *and* not let through by a rule: the queries nothing in
        # the hub's configuration had an opinion about.
        return not entry["blocked"] and entry.get("answer_status") != WHITELISTED
    return True


class QueryLogBuffer:
    """Bounded ring buffer of merged log entries, newest last."""

    def __init__(self, maxlen: int) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._keys: set[tuple[str, str, str, str]] = set()
        self._lock = asyncio.Lock()

    async def add(self, instance_name: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert entries not seen before; returns the ones that were new."""
        added: list[dict[str, Any]] = []
        async with self._lock:
            for entry in entries:
                key = _entry_key(instance_name, entry)
                if key in self._keys:
                    continue
                if len(self._entries) == self._entries.maxlen:
                    evicted = self._entries[0]
                    self._keys.discard(_entry_key(evicted["instance"], evicted))
                record = {**entry, "instance": instance_name}
                self._entries.append(record)
                self._keys.add(key)
                added.append(record)
        return added

    async def snapshot(
        self,
        limit: int = 200,
        *,
        search: str = "",
        instance: str = "",
        status: str = "",
        filter_list: str = "",
        blocked_only: bool = False,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            entries = list(self._entries)
        needle = search.lower().strip()
        if needle:
            # The rule and the list it came from are searched too. They were not,
            # while the field's own placeholder offered "a domain, a client or a
            # rule" — so typing `@@||` or a list's name found nothing, on the one
            # control that says what it searches.
            entries = [
                entry
                for entry in entries
                if needle in entry["question"].lower()
                or needle in entry["client"].lower()
                or needle in entry.get("rule", "").lower()
                or needle in entry.get("filter_list", "").lower()
            ]
        if instance:
            entries = [entry for entry in entries if entry["instance"] == instance]
        # Kept working alongside ``status``. It has been a query parameter of every
        # released hub, and something on somebody's network may be passing it;
        # breaking that to tidy up a signature is not worth a line of code.
        # ``status`` wins when both are given, being the more specific of the two.
        if not status and blocked_only:
            status = "blocked"
        if status:
            entries = [entry for entry in entries if matches_status(entry, status)]
        if filter_list:
            entries = [entry for entry in entries if entry.get("filter_list") == filter_list]
        entries.sort(key=lambda entry: entry["time"], reverse=True)
        return entries[:limit]

    async def filter_lists(self) -> list[str]:
        """Every list named in the buffer right now, for the filter's options.

        Read from the buffer rather than from the hub's subscriptions, because the
        two are not the same set: the built-in modules ("Safe browsing", "Your own
        rules") name themselves here and are not subscriptions at all, and a list
        whose rows have all aged out of the buffer is not worth offering as a
        filter that would come back empty.
        """
        async with self._lock:
            entries = list(self._entries)
        return sorted({entry.get("filter_list", "") for entry in entries} - {""})

    def resize(self, maxlen: int) -> None:
        """Change the retained-entry cap, keeping the newest entries."""
        if maxlen == self._entries.maxlen:
            return
        kept = list(self._entries)[-maxlen:]
        self._entries = deque(kept, maxlen=maxlen)
        self._keys = {_entry_key(entry["instance"], entry) for entry in kept}

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
            self._keys.clear()

    def __len__(self) -> int:
        return len(self._entries)


buffer = QueryLogBuffer(get_settings().querylog_buffer_size)


async def _name_the_lists(adapter, instance: Instance, rows: list[dict[str, Any]]) -> None:
    """Add the *name* of the list each row's rule came from, where one is knowable.

    Never raises and never fails a poll. This is an explanation attached to a log
    entry: worth having, never worth losing the entry over — and a node that will
    not answer ``/control/filtering/status`` is still perfectly able to say what
    it blocked.
    """
    if not any(row.get("filter_list_id") is not None for row in rows):
        return
    if filtersource.sources.needs_refresh(instance.id, filtersource.cited_ids(rows)):
        filtersource.sources.note_attempt(instance.id)
        try:
            filtersource.sources.remember(instance.id, await adapter.pull_filter_lists())
        except (AdapterError, ValueError) as exc:
            logger.debug("Could not read the filter lists of %s: %s", instance.name, exc)
    for row in rows:
        row["filter_list"] = filtersource.sources.label(instance.id, row.get("filter_list_id"))


async def poll_once() -> int:
    """Fetch the latest entries from every enabled instance. Returns the new-entry count."""
    settings = get_settings()
    async with session_scope() as session:
        result = await session.execute(select(Instance).where(Instance.enabled.is_(True)))
        instances = list(result.scalars().all())

    crypto = get_crypto()
    total_new = 0
    for instance in instances:
        adapter = build_adapter(instance, crypto)
        try:
            entries = await adapter.query_log(settings.querylog_fetch_limit)
            rows = [asdict(entry) for entry in entries]
            # Resolved here, against the node that wrote these rows, because an
            # AdGuard filter id means nothing without one: the same subscription
            # carries a different number on every node. Doing it at poll time
            # also means it is done once per entry rather than on every render.
            await _name_the_lists(adapter, instance, rows)
        except (AdapterError, ValueError) as exc:
            logger.debug("Query log poll failed for %s: %s", instance.name, exc)
            continue
        finally:
            await adapter.aclose()
        added = await buffer.add(instance.name, rows)
        if added:
            total_new += len(added)
            await bus.publish("querylog", added[-settings.querylog_fetch_limit :])
    return total_new


async def querylog_worker(stop: asyncio.Event) -> None:  # pragma: no cover - background loop
    while not stop.is_set():
        settings = hubsettings.current()
        if settings.querylog_enabled:
            try:
                await poll_once()
            except Exception:
                logger.exception("Query log poll failed")
        try:
            # Re-read the interval every cycle: it is editable at runtime.
            await asyncio.wait_for(stop.wait(), timeout=settings.querylog_poll_interval)
            return
        except TimeoutError:
            continue
