"""Pydantic request/response models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from .models import ListKind, RuleKind, RuleOrigin


def _stamp_utc(value: datetime) -> datetime:
    """Say that a stored timestamp is UTC, because the database cannot.

    Everything is written with ``utcnow()``, which is timezone-aware — but SQLite
    has no timestamp type and SQLAlchemy's ``DateTime(timezone=True)`` is a no-op
    there: the offset is dropped on write and the value comes back naive. Pydantic
    then serialises `2026-09-12T18:23:36`, and an ISO string with no offset is not
    ambiguous in JavaScript — it is defined as **local time**. A browser in UTC+2
    therefore reads every timestamp this hub sends as two hours earlier than it is.

    That is not a cosmetic error. The *Reconciliation* card decides whether the
    safety net has stopped by comparing the last pass against the clock, so a
    constant two-hour offset made a healthy reconciler read "may have stopped" —
    permanently, at every interval below eight hours. It cost an evening of
    looking for a fault in the worker, which was running the whole time.

    So the offset is put back on the way out, where it can be stated once and
    where every reader of this API gets it.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


#: A timestamp that leaves the hub saying which zone it is in.
UtcDatetime = Annotated[datetime, AfterValidator(_stamp_utc)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# -- auth ------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1)


class SetupRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8)


class ControlLogin(BaseModel):
    """AdGuard Home's login body, which names the field ``name``."""

    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class AuthState(BaseModel):
    authenticated: bool
    username: str | None = None
    setup_required: bool = False
    ephemeral_secret: bool = False
    # False until the first-run walkthrough is finished or skipped.
    onboarding_done: bool = False


# -- instances -------------------------------------------------------------


class InstanceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    adapter: str = "adguard"
    username: str = ""
    password: str = ""
    verify_tls: bool = True
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value


class ConnectionTest(BaseModel):
    """Probe credentials before an instance is saved (or with edited values)."""

    base_url: str = Field(min_length=1, max_length=500)
    adapter: str = "adguard"
    username: str = ""
    password: str = ""
    verify_tls: bool = True
    # Set when re-testing a saved instance whose password was left untouched.
    instance_id: int | None = None

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value


class ConnectionResult(BaseModel):
    ok: bool
    version: str = ""
    error: str = ""


class InstanceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = None
    username: str | None = None
    # Omit to keep the stored password; send a new value to replace it.
    password: str | None = None
    verify_tls: bool | None = None
    enabled: bool | None = None
    maintenance: bool | None = None

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value


class InstanceOut(ORMModel):
    """Credentials are deliberately never serialised — only whether one is stored."""

    id: int
    name: str
    base_url: str
    adapter: str
    username: str
    has_password: bool
    verify_tls: bool
    enabled: bool
    maintenance: bool
    status: str
    # AdGuard Home's own version, as last reported by the instance.
    version: str
    # Empty when there is nothing to install. `update_error` is the separate
    # case of the hub not having been able to find out.
    update_version: str
    update_url: str
    update_error: str
    last_error: str
    last_seen_at: UtcDatetime | None
    last_synced_at: UtcDatetime | None
    # Set while the node is answering but holding something other than what
    # the hub wants; NULL when it matches. `status` cannot say this — such a
    # node is genuinely online.
    out_of_sync_since: UtcDatetime | None
    created_at: UtcDatetime


class ImportRequest(BaseModel):
    replace: bool = True
    # Empty means every section the master exposes.
    sections: list[str] = Field(default_factory=list)
    push_after_import: bool = True


# -- rules -----------------------------------------------------------------


class RuleCreate(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    origin: RuleOrigin = RuleOrigin.custom
    enabled: bool = True
    comment: str = ""

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Rule text must not be empty")
        return value


class RuleUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool | None = None
    comment: str | None = None


class RuleOut(ORMModel):
    id: int
    text: str
    kind: RuleKind
    origin: RuleOrigin
    enabled: bool
    comment: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class DomainRuleRequest(BaseModel):
    """Query-log "Unblock"/"Block" action — the hub builds the AdGuard rule itself."""

    domain: str = Field(min_length=1, max_length=253)
    comment: str = ""

    @field_validator("domain")
    @classmethod
    def _clean(cls, value: str) -> str:
        value = value.strip().lower().rstrip(".")
        if not value or " " in value:
            raise ValueError("Not a valid domain")
        return value


class BulkRulesRequest(BaseModel):
    text: str = ""
    origin: RuleOrigin = RuleOrigin.custom


# -- filter lists ----------------------------------------------------------


class FilterListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=1000)
    kind: ListKind = ListKind.blocklist
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("Subscription URL must start with http:// or https://")
        return value


class FilterListUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None


class FilterListOut(ORMModel):
    id: int
    name: str
    url: str
    kind: ListKind
    enabled: bool
    created_at: UtcDatetime


class ListSizeInstanceOut(BaseModel):
    instance_id: int
    instance_name: str
    rules_count: int


class ListSizeOut(BaseModel):
    url: str
    kind: str
    rules_count: int
    agreed: bool
    per_instance: list[ListSizeInstanceOut]


class FilterSizesOut(BaseModel):
    """Subscription sizes, which only the nodes know: the hub stores URLs, not lists."""

    lists: list[ListSizeOut]
    total_rules: int
    instances_reporting: int
    instances_total: int


# -- DNS settings ----------------------------------------------------------


class ConfigFieldOut(BaseModel):
    key: str
    label: str
    type: str
    help: str
    unit: str
    options: list[list[str]]
    # Heading this field belongs under, from sections.py.
    group: str


class ConfigSectionOut(BaseModel):
    name: str
    title: str
    description: str
    notes: str
    risky: bool
    # This area has a page of its own; the combined list links there instead.
    own_page: bool
    fields: list[ConfigFieldOut]
    managed: bool
    has_data: bool
    keys: list[str]
    data: dict[str, Any]
    # Non-empty when the section is managed but cannot safely be pushed.
    skipped_reason: str
    updated_at: UtcDatetime


class ConfigSectionUpdate(BaseModel):
    managed: bool | None = None
    data: dict[str, Any] | None = None


class VersionOut(BaseModel):
    id: int
    label: str
    author: str
    kind: str
    summary: str
    created_at: UtcDatetime


class VersionDetail(BaseModel):
    id: int
    label: str
    author: str
    kind: str
    created_at: UtcDatetime
    snapshot: dict[str, Any]


class VersionDiff(BaseModel):
    from_id: int
    to_id: int | None
    to_label: str
    summary: str
    changes: dict[str, Any]


class VersionRestoreResult(BaseModel):
    version_id: int
    rules: int
    filter_lists: int
    sections: int
    pushed: bool


class HubSettingsOut(BaseModel):
    reconcile_enabled: bool
    reconcile_interval: int
    retry_interval: int
    querylog_enabled: bool
    querylog_poll_interval: int
    querylog_buffer_size: int
    http_timeout: int
    external_api_enabled: bool
    update_check_enabled: bool
    # Accepted ranges, so the form can bound its inputs rather than guess.
    limits: dict[str, list[int]]


class HubSettingsUpdate(BaseModel):
    reconcile_enabled: bool | None = None
    reconcile_interval: int | None = None
    retry_interval: int | None = None
    querylog_enabled: bool | None = None
    querylog_poll_interval: int | None = None
    querylog_buffer_size: int | None = None
    http_timeout: int | None = None
    external_api_enabled: bool | None = None
    update_check_enabled: bool | None = None


# -- notifiers -------------------------------------------------------------


NotifierType = Literal["homeassistant", "discord", "gotify"]


class NotifierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: NotifierType
    url: str = Field(min_length=1, max_length=1000)
    token: str = ""
    enabled: bool = True
    events: list[str] = Field(default_factory=list)


class NotifierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = None
    token: str | None = None
    enabled: bool | None = None
    events: list[str] | None = None


class NotifierOut(BaseModel):
    id: int
    name: str
    type: str
    url: str
    has_token: bool
    enabled: bool
    events: list[str]
    last_error: str


# -- operations ------------------------------------------------------------


class PushJobOut(BaseModel):
    id: int
    instance_id: int
    instance_name: str
    payload_kind: str
    status: str
    attempts: int
    last_error: str
    reason: str
    updated_at: UtcDatetime


class DriftEventOut(ORMModel):
    id: int
    instance_id: int | None
    instance_name: str
    payload_kind: str
    summary: str
    details: str
    corrected: bool
    occurrences: int
    last_seen_at: UtcDatetime | None
    took_ms: int
    created_at: UtcDatetime


class ReconcileRunOut(ORMModel):
    """One streak of reconciliation passes that ended the same way."""

    id: int
    started_at: UtcDatetime
    last_at: UtcDatetime
    passes: int
    instances: int
    unreachable: int
    with_differences: int
    corrected: int
    out_of_sync: int
    last_took_ms: int
    max_took_ms: int


class SyncResult(BaseModel):
    instances: int
    failed: dict[str, str]


class DashboardStats(BaseModel):
    #: Whether the hub holds anything to replicate. False means reconciliation is
    #: skipping its passes deliberately, which the Reconciliation card has to be
    #: able to say — otherwise it reads as a timer that stopped.
    replicating: bool = True
    instances_total: int
    # Most recent successful push across all instances, and how many are current.
    last_sync_at: UtcDatetime | None = None
    instances_synced: int = 0
    managed_sections: int = 0
    versions_total: int = 0
    instances_online: int
    instances_unreachable: int
    instances_disabled: int
    rules_total: int
    rules_allow: int
    rules_block: int
    filter_lists_total: int
    filter_lists_enabled: int
    pending_jobs: int
    failed_jobs: int
    recent_drift: int
    querylog_buffered: int


class TopEntryOut(BaseModel):
    name: str
    count: int


class TrafficOut(BaseModel):
    """Live DNS statistics summed across every instance that answered."""

    queries: int
    blocked: int
    block_rate: float
    replaced_safebrowsing: int
    # In whatever unit the instances report; see services/aggregate.py.
    avg_processing_time_ms: float
    series_queries: list[int]
    series_blocked: list[int]
    time_units: str
    top_queried: list[TopEntryOut]
    top_blocked: list[TopEntryOut]
    top_clients: list[TopEntryOut]
    # A total that is short by one node reads as a quiet day, so say who answered.
    instances_reporting: int
    instances_total: int


class QueryLogEntryOut(BaseModel):
    instance: str
    time: str
    question: str
    question_type: str
    client: str
    answer_status: str
    blocked: bool
    rule: str
    elapsed_ms: float
    upstream: str


class ReconcileReportOut(BaseModel):
    instance_id: int
    instance_name: str
    checked: bool
    error: str
    corrected: bool
    differences: list[dict[str, Any]]
    took_ms: int = 0
