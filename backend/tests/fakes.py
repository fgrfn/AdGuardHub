"""In-memory adapter double, standing in for a real AdGuard Home instance."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from app.adapters.base import AdapterError, DnsAdapter, QueryLogEntry, RemoteFilterList
from app.adapters.sections import SECTION_NAMES


class FakeInstanceState:
    def __init__(self) -> None:
        self.rules: list[str] = []
        self.filter_lists: list[RemoteFilterList] = []
        # Section name -> document, or absent when this fake does not implement it.
        self.sections: dict[str, dict[str, Any]] = {}
        self.query_log: list[QueryLogEntry] = []
        self.offline = False
        # Rules this node accepts and then does not keep — a 2xx followed by
        # nothing stored. Real AdGuard does this; a double that always stores
        # what it is given cannot express the fault at all, which is why the
        # loop it causes went unnoticed through two releases.
        self.refuses: set[str] = set()
        # Whether writing a settings section wipes the rule set, the way a node
        # reconfiguring itself can undo a write that arrived moments earlier.
        # Real AdGuard reconfigures on every configuration change, so the three
        # payloads of one push are not independent — and a double that treats
        # them as independent cannot express the fault where a rule is confirmed
        # as landed and then overwritten by the rest of the same push.
        self.section_push_drops_rules = False
        # Payload kinds whose push raises, while the node stays reachable for
        # everything else. A node that is simply offline cannot express this:
        # the interesting case is one that answers a read, takes some writes and
        # rejects one — which is what makes a pass correct part of its findings.
        self.push_errors: set[str] = set()
        self.push_calls = 0
        self.unsupported_sections: set[str] = set()
        self.stats: dict[str, Any] = {}
        # How often the node was actually asked — the cache in front of the
        # aggregation is only worth having if this stops climbing.
        self.stats_calls = 0


class FakeAdapter(DnsAdapter):
    """Every adapter instance for the same base_url shares one state object."""

    name = "fake"
    VERSION = "v0.107.fake"
    states: ClassVar[dict[str, FakeInstanceState]] = {}

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        *,
        verify_tls: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.state = self.states.setdefault(self.base_url, FakeInstanceState())

    @classmethod
    def reset(cls) -> None:
        cls.states.clear()

    @classmethod
    def state_for(cls, base_url: str) -> FakeInstanceState:
        return cls.states.setdefault(base_url.rstrip("/"), FakeInstanceState())

    def _guard(self) -> None:
        if self.state.offline:
            raise AdapterError(f"{self.base_url} is unreachable")

    def _refuse_push(self, kind: str) -> None:
        if kind in self.state.push_errors:
            raise AdapterError(f"{self.base_url} rejected the {kind} push")

    async def check(self) -> str:
        self._guard()
        return self.VERSION

    async def pull_rules(self) -> list[str]:
        self._guard()
        return list(self.state.rules)

    async def push_rules(self, rules: list[str]) -> None:
        self._guard()
        self._refuse_push("rules")
        self.state.rules = [rule for rule in rules if rule not in self.state.refuses]
        self.state.push_calls += 1

    async def pull_filter_lists(self) -> list[RemoteFilterList]:
        self._guard()
        return list(self.state.filter_lists)

    async def push_filter_lists(self, lists: list[RemoteFilterList]) -> None:
        self._guard()
        self._refuse_push("filters")
        # ``rules_count`` belongs to the node, not to the push: a real AdGuard does
        # not forget how many rules it parsed because the hub renamed a list or
        # toggled it. The hub always sends 0 (it never stores list contents), so
        # replacing wholesale would wipe the counts on every sync.
        # `remote_id` belongs to the node for the same reason `rules_count` does:
        # AdGuard assigns it when the list is added, and the hub never sends one.
        # A double that dropped it on every push could not express the query log's
        # central fact — that an id identifies a list *on one node*.
        held = {
            (item.kind, item.url): (item.rules_count, item.remote_id)
            for item in self.state.filter_lists
        }
        self.state.filter_lists = [
            replace(
                item,
                rules_count=held.get((item.kind, item.url), (item.rules_count, 0))[0],
                remote_id=held.get((item.kind, item.url), (0, item.remote_id))[1],
            )
            for item in lists
        ]
        self.state.push_calls += 1

    def supported_sections(self) -> tuple[str, ...]:
        return SECTION_NAMES

    async def pull_section(self, name: str) -> dict[str, Any] | None:
        self._guard()
        if name in self.state.unsupported_sections:
            return None
        return dict(self.state.sections.get(name, {})) or None

    async def push_section(self, name: str, data: dict[str, Any]) -> None:
        self._guard()
        if name in self.state.unsupported_sections:
            raise AdapterError(f"{name} is not supported by {self.base_url}", status=404)
        self._refuse_push("settings")
        self.state.sections[name] = dict(data)
        if self.state.section_push_drops_rules:
            self.state.rules = []
        self.state.push_calls += 1

    async def stats(self) -> dict[str, Any]:
        self._guard()
        self.state.stats_calls += 1
        return dict(self.state.stats)

    async def query_log(self, limit: int) -> list[QueryLogEntry]:
        self._guard()
        return list(self.state.query_log[:limit])
