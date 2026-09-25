"""Adapter interface every DNS filtering backend must implement.

AdGuard Home is the only backend and no other is planned (spec §3). The interface
is not a promise of one: it is what keeps sync, reconcile and import free of
AdGuard's HTTP API, so they can be reasoned about — and tested against a fake —
without a node in the loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class AdapterError(RuntimeError):
    """Raised when a backend is unreachable or rejects a request.

    ``status`` carries the HTTP status when there was one, so callers can tell an
    unsupported endpoint (404/405) from a real failure.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class RemoteFilterList:
    name: str
    url: str
    enabled: bool
    kind: str  # "blocklist" | "allowlist"
    # How many rules the node parsed out of the downloaded list. Read-only: the
    # hub never stores or pushes it — it is the node's own count of a file only
    # the node has — and it is deliberately not part of the drift comparison,
    # because two nodes on different refresh schedules legitimately disagree.
    rules_count: int = 0
    #: The node's own id for this list, which is what its query log cites when a
    #: rule from it matches. Read-only and **per node**: AdGuard assigns it from
    #: a clock-seeded counter at `add_url`, so the same subscription has
    #: different ids on two nodes. Like ``rules_count`` it is never pushed and
    #: never compared — it belongs to the node, not to the subscription.
    remote_id: int = 0
    #: When the node last downloaded this list, as the node reports it. Empty
    #: when it never has — AdGuard keeps a subscription it cannot fetch rather
    #: than dropping it, so a broken list does not disappear, it goes quietly
    #: stale. Read-only and per node for the same reasons as the two above.
    last_updated: str = ""


@dataclass(slots=True)
class RemoteState:
    """The subset of a backend's configuration that AdGuardHub manages.

    ``sections`` maps a section name to its document, or to ``None`` when this
    backend does not implement that area at all.
    """

    rules: list[str] = field(default_factory=list)
    filter_lists: list[RemoteFilterList] = field(default_factory=list)
    sections: dict[str, dict[str, Any] | None] = field(default_factory=dict)


@dataclass(slots=True)
class QueryLogEntry:
    time: str
    question: str
    question_type: str
    client: str
    answer_status: str
    blocked: bool
    rule: str = ""
    elapsed_ms: float = 0.0
    upstream: str = ""
    #: Which list the matched rule came from, as the node numbers its lists.
    #: Meaningless on its own — see ``services.filtersource``, which turns it
    #: into a name. ``None`` when the node cited no rule at all.
    filter_list_id: int | None = None


@dataclass(slots=True)
class RemoteUpdate:
    """What a node says about its own version, as far as it will say anything.

    Every field is optional on purpose. A node may have its update check turned
    off, may be a build that predates the endpoint, or may simply be unreachable
    — and "the hub could not find out" has to be distinguishable from "there is
    nothing to install", because only one of them is worth showing.
    """

    current: str = ""
    latest: str = ""
    available: bool = False
    url: str = ""
    #: Why there is no answer. Empty when the node answered.
    error: str = ""


class DnsAdapter(ABC):
    """Contract for pushing and pulling managed state."""

    name: str = "base"

    @abstractmethod
    async def check(self) -> str:
        """Return a version/identity string, or raise AdapterError."""

    @abstractmethod
    async def pull_rules(self) -> list[str]:
        """Return the backend's current custom filtering rules, in order."""

    @abstractmethod
    async def push_rules(self, rules: list[str]) -> None:
        """Replace the backend's custom filtering rules with ``rules``."""

    @abstractmethod
    async def pull_filter_lists(self) -> list[RemoteFilterList]:
        """Return the backend's subscribed block/allow lists."""

    @abstractmethod
    async def push_filter_lists(self, lists: list[RemoteFilterList]) -> None:
        """Make the backend's subscriptions match ``lists`` exactly."""

    async def refresh_filter_lists(self, *, allowlists: bool = False) -> int:
        """Ask the backend to re-download its subscriptions now. Returns how many changed.

        Not part of replication and never called by a push or a reconciliation
        pass: the hub owns which lists a node carries, and the node owns their
        contents. This is the operator saying "go and look" ahead of the node's
        own schedule, which is the one thing about a subscription's freshness the
        hub can do from here.

        Default ``0`` rather than an error: a backend that cannot be asked has
        nothing to refresh, which is not a failure to report.
        """
        return 0

    @abstractmethod
    async def query_log(self, limit: int) -> list[QueryLogEntry]:
        """Return the most recent query log entries, newest first."""

    @abstractmethod
    async def stats(self) -> dict[str, Any]:
        """Return the backend's own statistics document."""

    @abstractmethod
    async def pull_section(self, name: str) -> dict[str, Any] | None:
        """Read one configuration section, or ``None`` if the backend lacks it."""

    @abstractmethod
    async def push_section(self, name: str, data: dict[str, Any]) -> None:
        """Apply one configuration section."""

    async def check_update(self, current: str = "") -> RemoteUpdate:
        """Whether the backend has a newer version of itself available.

        ``current`` is the version the hub already believes this node is
        running, for backends whose update endpoint reports what is available
        without saying what is installed. Deciding that from the two together is
        the caller's contribution: the hub asked the node its version moments
        earlier and would otherwise throw that away.

        Not abstract: a backend that cannot answer is a backend with nothing to
        report, not one that fails to load. The default says exactly that.
        """
        return RemoteUpdate(error="this backend does not report updates")

    def supported_sections(self) -> tuple[str, ...]:
        return ()

    async def pull_state(self, sections: tuple[str, ...] = ()) -> RemoteState:
        return RemoteState(
            rules=await self.pull_rules(),
            filter_lists=await self.pull_filter_lists(),
            sections={name: await self.pull_section(name) for name in sections},
        )

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
