"""One file to attach to a bug report.

Three bugs this week were diagnosed by asking the operator for screenshots, a
version number, and a paste of `journalctl` — four round trips before anybody
knew what the hub actually thought was going on. This produces that in one
click: what version, installed how, which nodes in what state, what the sync
engine has been logging, and what reconciliation keeps finding.

**It is not a backup, and the difference decides what may be in it.** A backup
stays with the person who downloaded it; a diagnostic bundle is pasted into a
public issue where a stranger reads it. So on top of the rule a backup already
follows — no passwords, in the clear or encrypted — this one drops everything
that says *where* a node is or *who* signs in to it:

* **Identities become pseudonyms, consistently.** Every node is `node-1`,
  `node-2`… in the same order everywhere in the document, so a drift entry can
  still be matched to the node's state and to the log lines about it. Naming
  them is what would leak; correlating them is the entire point.
* **The substitution runs over free text too.** A node's address turns up inside
  error strings far more often than in the field meant for it — "connecting to
  http://10.10.10.252/control/status: timed out" is a `last_error`, a job error
  and a log line. Redacting the column and leaving the sentence would be
  security theatre.
* **Secrets that live in a URL's path are dropped whole.** A Discord webhook URL
  *is* its credential, and so is a Home Assistant webhook id. Notifier targets
  are reported by host, never by path.

What deliberately stays in is the filtering content: rule text, subscription
URLs, the domains in a drift entry. That is the subject of nearly every report
this exists to serve — "this allow rule will not stick" is unanswerable without
the rule. The interface says so plainly before the download, because the honest
handling of a private-ish payload is to name it, not to quietly ship it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import (
    ConfigSection,
    ConfigVersion,
    DriftEvent,
    FilterList,
    HubSettings,
    Instance,
    JobStatus,
    NotifierTarget,
    PushJob,
    Rule,
    RuleKind,
    RuleOrigin,
    User,
)
from ..version import VERSION
from . import querylog
from .logbuffer import get_buffer
from .updates import install_method

FORMAT = "adguardhub-diagnostics"
FORMAT_VERSION = 1

#: Recent history, not an archive. Enough to see a pattern repeat, small enough
#: that the file can be read by the person about to post it.
DRIFT_LIMIT = 50
VERSION_LIMIT = 20
LOG_LIMIT = 200

#: Settings columns worth reporting. ``id`` says nothing and ``onboarding_done``
#: is about a walkthrough, not about behaviour.
SETTINGS_FIELDS = (
    "reconcile_enabled",
    "reconcile_interval",
    "retry_interval",
    "querylog_enabled",
    "querylog_poll_interval",
    "querylog_buffer_size",
    "http_timeout",
    "external_api_enabled",
    "update_check_enabled",
)

#: Substitutions shorter than this are matched on word boundaries anyway, but a
#: one- or two-character needle still stands a real chance of rewriting
#: unrelated text into nonsense. A username that short leaks its own length and
#: nothing else; a mangled log line loses the report.
MIN_NEEDLE = 3

_PRIVATE_V4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# Anything shaped like an IPv6 address: hex groups around at least two colons,
# not touching another hex digit, colon or dot on either side, so a clock time
# or the middle of a longer token is not caught. Whether it actually is an
# address, and of what kind, is left to ipaddress — the pattern only finds
# candidates. The optional %zone is what a link-local address carries.
_V6_CANDIDATE = re.compile(
    r"(?<![0-9A-Za-z:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[0-9A-Za-z]+)?"
    r"(?![0-9A-Za-z:])"
)

# A MAC address is a client's hardware identity. AdGuard's clients section
# carries them, so a drift entry about that section does too.
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")


class Redactor:
    """Replaces everything that identifies a node, everywhere it appears.

    Built once per bundle from the instance table, then applied to every string
    that goes in. One object holds the whole mapping so that the same node reads
    as the same pseudonym in its own row, in a drift entry, in a queued job and
    in a log line — the correlation is what makes the file worth reading.
    """

    def __init__(self, instances: list[Instance], admin: str = "") -> None:
        self.alias: dict[int, str] = {}
        replacements: dict[str, str] = {}
        for number, instance in enumerate(instances, start=1):
            alias = f"node-{number}"
            self.alias[instance.id] = alias
            for needle in (instance.base_url, _host(instance.base_url), instance.name):
                _offer(replacements, needle, alias)
            _offer(replacements, instance.username, f"{alias}-user")
        _offer(replacements, admin, "admin")

        # Longest first, so a node named "adguard" cannot chew a hole in the
        # middle of "adguard-primary" before the longer needle is tried.
        needles = sorted(replacements, key=len, reverse=True)
        self._by_lower = {needle.lower(): alias for needle, alias in replacements.items()}
        self._pattern = (
            re.compile("|".join(rf"\b{re.escape(needle)}\b" for needle in needles), re.IGNORECASE)
            if needles
            else None
        )

    def text(self, value: str | None) -> str:
        """Scrub one string. Safe to call on anything, including empty."""
        if not value:
            return ""
        if self._pattern is not None:
            value = self._pattern.sub(self._replace, value)
        # Whatever is left that looks like a private address belongs to a client
        # or to the hub's own host, not to a node — those were replaced above.
        # Neither is diagnostic, and together they map somebody's network.
        # The same rule for both address families: a drift entry about the
        # clients section, or a sign-in line, names IPv6 hosts as readily as
        # IPv4 ones, and a household's link-local and ULA addresses map it just
        # as well. Hardware addresses go too, for the same reason. The MAC
        # pattern runs last: a MAC is also six hex groups around colons, and
        # ipaddress rightly refuses it, so the IPv6 pass leaves it alone.
        value = _PRIVATE_V4.sub(_mask_private, value)
        value = _V6_CANDIDATE.sub(_mask_private_v6, value)
        return _MAC.sub("<mac>", value)

    def _replace(self, match: re.Match[str]) -> str:
        alias = self._by_lower.get(match.group(0).lower())
        return f"<{alias}>" if alias else match.group(0)

    def url(self, value: str) -> str:
        """A URL reduced to the part that describes a service, not a secret.

        The path is dropped rather than scrubbed: a Discord webhook URL and a
        Home Assistant webhook trigger both carry their credential there, and
        "keep the bit that identifies the target, drop the bit that authorises
        against it" is the only rule that stays correct as targets are added.
        """
        parts = urlsplit(value or "")
        if not parts.scheme:
            return ""
        return self.text(f"{parts.scheme}://{parts.hostname or ''}") + (
            f":{parts.port}" if parts.port else ""
        )


def _offer(replacements: dict[str, str], needle: str | None, alias: str) -> None:
    cleaned = (needle or "").strip().rstrip("/")
    if len(cleaned) >= MIN_NEEDLE:
        replacements.setdefault(cleaned, alias)


def _host(base_url: str) -> str:
    return urlsplit(base_url or "").hostname or ""


def _mask_private(match: re.Match[str]) -> str:
    try:
        address = ipaddress.ip_address(match.group(0))
    except ValueError:
        # Four dotted numbers that are not an address — a version string like
        # 1.2.3.4444, or part of a longer token. Leave it alone.
        return match.group(0)
    if address.is_private or address.is_loopback or address.is_link_local:
        return "<local-ip>"
    return match.group(0)


def _mask_private_v6(match: re.Match[str]) -> str:
    text = match.group(0)
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        # Colons and hex that are not an address — a MAC, a hash, a time with
        # too many fields. Leave it alone.
        return text
    if address.is_private or address.is_loopback or address.is_link_local:
        return "<local-ipv6>"
    return text


def _host_kind(base_url: str) -> str:
    """What sort of address a node is reached at, without saying which.

    Worth keeping: "name" against "ipv4" is the difference between a DNS
    resolution problem and a routing one, and it is the first thing to ask when
    a node the hub cannot reach answers fine in a browser.
    """
    host = _host(base_url)
    if not host:
        return "none"
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "localhost"
    try:
        return f"ipv{ipaddress.ip_address(host).version}"
    except ValueError:
        return "name"


def _stamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _count(session: AsyncSession, statement) -> int:
    return int((await session.execute(statement)).scalar_one() or 0)


async def build(session: AsyncSession) -> dict[str, Any]:
    """The whole bundle, already redacted."""
    instances = list(
        (await session.execute(select(Instance).order_by(Instance.id.asc()))).scalars().all()
    )
    admin = (await session.execute(select(User.username).limit(1))).scalar_one_or_none() or ""
    redactor = Redactor(instances, admin)

    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "hub": _hub(),
        "settings": await _settings(session),
        "counts": await _counts(session),
        "instances": [_instance(row, redactor) for row in instances],
        "sections": await _sections(session),
        "filter_lists": await _filter_lists(session),
        "notifiers": await _notifiers(session, redactor),
        "jobs": await _jobs(session, redactor),
        "drift": await _drift(session, redactor),
        "versions": await _versions(session, redactor),
        "log": _log(redactor),
    }


def _hub() -> dict[str, Any]:
    settings = get_settings()
    return {
        "version": VERSION,
        "install_method": install_method(),
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "log_level": settings.log_level,
        # The path itself can name a home directory, and whether one is
        # configured is the whole diagnostic content of the answer.
        "log_file_configured": bool(settings.log_file),
        # An unset key means credentials are encrypted with a per-boot random
        # one, which explains "my nodes lost their passwords on restart" without
        # anybody having to ask.
        "secret_key_set": bool(settings.secret_key),
        "data_dir_is_default": os.path.abspath(settings.data_dir)
        == os.path.abspath("./data"),
        "serves_frontend": os.path.isdir(settings.static_dir),
        "tz": datetime.now().astimezone().tzname() or "",
        "argv0": os.path.basename(sys.argv[0]) if sys.argv else "",
    }


async def _settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(HubSettings, 1)
    if row is None:
        return {}
    return {field: getattr(row, field) for field in SETTINGS_FIELDS}


async def _counts(session: AsyncSession) -> dict[str, int]:
    def count_of(model, *conditions):
        return select(func.count()).select_from(model).where(*conditions)

    return {
        "rules": await _count(session, count_of(Rule)),
        "rules_allow": await _count(session, count_of(Rule, Rule.kind == RuleKind.allow.value)),
        "rules_block": await _count(session, count_of(Rule, Rule.kind == RuleKind.block.value)),
        "rules_from_querylog": await _count(
            session, count_of(Rule, Rule.origin == RuleOrigin.querylog.value)
        ),
        "rules_disabled": await _count(session, count_of(Rule, Rule.enabled.is_(False))),
        "filter_lists": await _count(session, count_of(FilterList)),
        "filter_lists_enabled": await _count(
            session, count_of(FilterList, FilterList.enabled.is_(True))
        ),
        "versions": await _count(session, count_of(ConfigVersion)),
        "drift_events": await _count(session, count_of(DriftEvent)),
        "jobs_pending": await _count(
            session, count_of(PushJob, PushJob.status == JobStatus.pending.value)
        ),
        "jobs_failed": await _count(
            session, count_of(PushJob, PushJob.status == JobStatus.failed.value)
        ),
        "querylog_buffered": len(querylog.buffer),
    }


def _instance(row: Instance, redactor: Redactor) -> dict[str, Any]:
    return {
        "id": redactor.alias.get(row.id, f"node-{row.id}"),
        "adapter": row.adapter,
        "scheme": urlsplit(row.base_url or "").scheme,
        "host_kind": _host_kind(row.base_url),
        "port": urlsplit(row.base_url or "").port,
        "verify_tls": row.verify_tls,
        "has_password": bool(row.password_encrypted),
        "enabled": row.enabled,
        "maintenance": row.maintenance,
        "status": row.status,
        "version": row.version,
        "update_version": row.update_version,
        "update_error": redactor.text(row.update_error),
        "last_error": redactor.text(row.last_error),
        "last_seen_at": _stamp(row.last_seen_at),
        "last_synced_at": _stamp(row.last_synced_at),
    }


async def _sections(session: AsyncSession) -> list[dict[str, Any]]:
    """Which sections are replicated, and the shape of each — never the values.

    A section document holds upstream resolvers, client names and MAC
    addresses, and blocked hostnames: a map of the household. The keys alone
    answer what this is for, which is whether a section is managed, whether the
    hub has anything stored for it, and whether a node is missing a key the hub
    is sending.
    """
    rows = (
        (await session.execute(select(ConfigSection).order_by(ConfigSection.name.asc())))
        .scalars()
        .all()
    )
    out = []
    for row in rows:
        try:
            document = json.loads(row.data or "{}")
        except ValueError:
            document = {}
        out.append(
            {
                "name": row.name,
                "managed": row.managed,
                "keys": sorted(document) if isinstance(document, dict) else [],
                "bytes": len(row.data or ""),
                "updated_at": _stamp(row.updated_at),
            }
        )
    return out


async def _filter_lists(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (await session.execute(select(FilterList).order_by(FilterList.id.asc()))).scalars().all()
    )
    # Subscription URLs stay: which lists somebody runs is the answer to half of
    # the filtering reports, and they are public addresses. The query string
    # goes, because that is where a self-hosted list would carry a token.
    return [
        {
            "name": row.name,
            "url": urlsplit(row.url or "")._replace(query="", fragment="").geturl(),
            "kind": row.kind,
            "enabled": row.enabled,
        }
        for row in rows
    ]


async def _notifiers(session: AsyncSession, redactor: Redactor) -> list[dict[str, Any]]:
    rows = (
        (await session.execute(select(NotifierTarget).order_by(NotifierTarget.id.asc())))
        .scalars()
        .all()
    )
    return [
        {
            "type": row.type,
            "enabled": row.enabled,
            "events": row.events,
            "has_token": bool(row.token),
            "url_host": redactor.url(row.url),
            "last_error": redactor.text(row.last_error),
        }
        for row in rows
    ]


async def _jobs(session: AsyncSession, redactor: Redactor) -> list[dict[str, Any]]:
    """Everything still owed to a node. Applied jobs are history, not a symptom."""
    rows = (
        (
            await session.execute(
                select(PushJob)
                .where(PushJob.status != JobStatus.applied.value)
                .order_by(PushJob.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "instance": redactor.alias.get(row.instance_id, "node-?"),
            "payload_kind": row.payload_kind,
            "status": row.status,
            "attempts": row.attempts,
            "reason": redactor.text(row.reason),
            "last_error": redactor.text(row.last_error),
            "updated_at": _stamp(row.updated_at),
        }
        for row in rows
    ]


async def _drift(session: AsyncSession, redactor: Redactor) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(DriftEvent).order_by(DriftEvent.id.desc()).limit(DRIFT_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            # A drift entry outlives the node it is about. Once that node is
            # gone its name is no longer in the map, so scrubbing it would be a
            # no-op and the name would ship — hence a fixed placeholder rather
            # than a best effort.
            "instance": redactor.alias.get(row.instance_id or 0, "removed-node"),
            "payload_kind": row.payload_kind,
            "summary": redactor.text(row.summary),
            "details": redactor.text(row.details),
            "corrected": row.corrected,
            # The two numbers that separate a fault still happening from one that
            # stopped hours ago — which, in a bundle attached to a report, is the
            # difference between a live problem and a historical one.
            "occurrences": row.occurrences,
            "last_seen_at": _stamp(row.last_seen_at) if row.last_seen_at else "",
            "took_ms": row.took_ms,
            "created_at": _stamp(row.created_at),
        }
        for row in rows
    ]


async def _versions(session: AsyncSession, redactor: Redactor) -> list[dict[str, Any]]:
    """Labels only. The snapshots are the configuration itself, and huge."""
    rows = (
        (
            await session.execute(
                select(ConfigVersion).order_by(ConfigVersion.id.desc()).limit(VERSION_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "label": redactor.text(row.label),
            "kind": row.kind,
            "author": redactor.text(row.author),
            "bytes": len(row.snapshot or ""),
            "created_at": _stamp(row.created_at),
        }
        for row in rows
    ]


def _log(redactor: Redactor) -> list[str]:
    return [redactor.text(line.message) for line in get_buffer().since(0, limit=LOG_LIMIT)]
