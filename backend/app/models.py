"""SQLAlchemy models — the central source of truth for all managed AdGuard config."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RuleKind(StrEnum):
    allow = "allow"
    block = "block"
    # A `!` or `#` line. Stored rather than dropped: it is usually the note
    # saying *why* the rule under it exists, and the hub is the source of truth
    # for the whole rule set, not for the parts of it it happens to understand.
    comment = "comment"


class RuleOrigin(StrEnum):
    """Which of the three AdGuard entry points (spec §5) created the rule."""

    custom = "custom"
    allowlist = "allowlist"
    querylog = "querylog"


class ListKind(StrEnum):
    blocklist = "blocklist"
    allowlist = "allowlist"


class InstanceStatus(StrEnum):
    unknown = "unknown"
    online = "online"
    unreachable = "unreachable"
    disabled = "disabled"
    # Deliberately paused while somebody works on the node. Not a fault, and not
    # the same as disabled: the hub keeps collecting what it owes this instance.
    maintenance = "maintenance"


class JobStatus(StrEnum):
    pending = "pending"
    applied = "applied"
    failed = "failed"


class PayloadKind(StrEnum):
    """What a push job synchronises. Every push is full-state and idempotent."""

    rules = "rules"
    filters = "filters"
    settings = "settings"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    base_url: Mapped[str] = mapped_column(String(500))
    adapter: Mapped[str] = mapped_column(String(40), default="adguard")
    username: Mapped[str] = mapped_column(String(255), default="")
    # Fernet ciphertext — see security.Crypto. Never returned by the API.
    password_encrypted: Mapped[str] = mapped_column(Text, default="")
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Hands off this node for a while: no pushes, no reconciliation, no outage
    # alerts. What the hub would have sent is queued instead of dropped, so
    # switching it back off replays the work rather than losing it.
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    status: Mapped[str] = mapped_column(String(20), default=InstanceStatus.unknown.value)
    # The AdGuard Home version reported by /control/status, refreshed on every probe.
    version: Mapped[str] = mapped_column(String(40), default="")
    # What the node says about its own updates, refreshed on the reconcile timer.
    # Empty means nothing to install; update_error means the hub could not find
    # out, which is deliberately not the same thing.
    #
    # server_default as well as default: the auto-migration writes these columns
    # with a SQL-level DEFAULT, so without it a freshly created database and an
    # upgraded one would carry different definitions of the same column.
    update_version: Mapped[str] = mapped_column(String(40), default="", server_default="")
    update_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    update_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this node was first found holding something other than what the hub
    # wants, and still is. NULL means it matches.
    #
    # A node that answers every request is *online*, and until this existed that
    # was the whole of what the interface said about it. One went four hours
    # refusing every correction — twenty subscriptions missing, the same failure
    # every five minutes — while its card read "online" and nothing anywhere was
    # amber. The drift log had it, but only if you went looking, and the one
    # notification it sent was correctly suppressed as a repeat after the first.
    out_of_sync_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Rule(Base):
    """One AdGuard-native filtering rule. Storage is AdGuard syntax (spec §5)."""

    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("text", name="uq_rules_text"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(String(1000))
    kind: Mapped[str] = mapped_column(String(10), default=RuleKind.block.value)
    origin: Mapped[str] = mapped_column(String(20), default=RuleOrigin.custom.value)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FilterList(Base):
    """A blocklist/allowlist *subscription URL*. Resolved domains stay in AdGuard (spec §12)."""

    __tablename__ = "filter_lists"
    __table_args__ = (UniqueConstraint("url", "kind", name="uq_filter_lists_url_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(1000))
    kind: Mapped[str] = mapped_column(String(20), default=ListKind.blocklist.value)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConfigSection(Base):
    """One managed AdGuard configuration area (adapters/sections.py).

    ``data`` is the section's document as JSON — deliberately opaque, so a new
    AdGuard setting needs no schema change here.
    """

    __tablename__ = "config_sections"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class HubSettings(Base):
    """Singleton (id=1) of the operational settings editable from the UI."""

    __tablename__ = "hub_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    reconcile_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reconcile_interval: Mapped[int] = mapped_column(Integer, default=300)
    retry_interval: Mapped[int] = mapped_column(Integer, default=30)
    querylog_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    querylog_poll_interval: Mapped[int] = mapped_column(Integer, default=5)
    querylog_buffer_size: Mapped[int] = mapped_column(Integer, default=2000)
    http_timeout: Mapped[int] = mapped_column(Integer, default=10)
    external_api_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # One request to github.com every few hours, asking only what the newest
    # release is. Off is a legitimate answer on an air-gapped network.
    update_check_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # First-run walkthrough. Kept here rather than in the browser: an operator who
    # sets the hub up on one machine should not be walked through it again on the
    # next, and a configured hub is a property of the hub, not of a browser.
    onboarding_done: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConfigVersion(Base):
    """A point-in-time snapshot of everything AdGuardHub manages.

    One row per change, so the operator can see what a sync actually carried,
    compare two points, and roll back to either.
    """

    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    author: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(20), default="change")
    snapshot: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushJob(Base):
    """Retry queue entry (spec §6).

    Every push is full-state, so an open job for the same instance + payload kind is
    reused rather than duplicated — the queue never grows one entry per edit.
    """

    __tablename__ = "push_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id", ondelete="CASCADE"))
    payload_kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.pending.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DriftEvent(Base):
    """A reconciliation finding. Every correction is logged, never silent (spec §6)."""

    __tablename__ = "drift_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL"), nullable=True
    )
    instance_name: Mapped[str] = mapped_column(String(120), default="")
    payload_kind: Mapped[str] = mapped_column(String(20), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[str] = mapped_column(Text, default="")
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotifierTarget(Base):
    """A webhook notification target (spec §10)."""

    __tablename__ = "notifier_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(String(1000))
    token: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    events: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
