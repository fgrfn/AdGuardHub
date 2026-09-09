export type RuleKind = 'allow' | 'block' | 'comment'
export type RuleOrigin = 'custom' | 'allowlist' | 'querylog'
export type ListKind = 'blocklist' | 'allowlist'
export type NotifierType = 'homeassistant' | 'discord' | 'gotify'

export interface AuthState {
  authenticated: boolean
  username: string | null
  setup_required: boolean
  ephemeral_secret: boolean
  /** False until the first-run walkthrough is finished or skipped. */
  onboarding_done: boolean
}

export interface Instance {
  id: number
  name: string
  base_url: string
  adapter: string
  username: string
  has_password: boolean
  verify_tls: boolean
  enabled: boolean
  /** Held back on purpose while somebody works on the node. Not a fault: what
   *  the hub would have pushed is queued and replayed on release. */
  maintenance: boolean
  status: 'unknown' | 'online' | 'unreachable' | 'disabled' | 'maintenance'
  /** AdGuard Home's own version, as last reported by the instance. */
  version: string
  /** A newer AdGuard Home the node knows about. Empty means nothing to install. */
  update_version: string
  update_url: string
  /** Why the node could not be asked — not the same as "nothing to install". */
  update_error: string
  last_error: string
  last_seen_at: string | null
  last_synced_at: string | null
  /** Answering, but not holding what the hub wants. Null when it matches. */
  out_of_sync_since: string | null
  created_at: string
}

export interface Rule {
  id: number
  text: string
  kind: RuleKind
  origin: RuleOrigin
  enabled: boolean
  comment: string
  created_at: string
  updated_at: string
}

export interface FilterList {
  id: number
  name: string
  url: string
  kind: ListKind
  enabled: boolean
  created_at: string
}

export interface QueryLogEntry {
  instance: string
  time: string
  question: string
  question_type: string
  client: string
  answer_status: string
  blocked: boolean
  rule: string
  elapsed_ms: number
  upstream: string
}

export interface PushJob {
  id: number
  instance_id: number
  instance_name: string
  payload_kind: string
  status: 'pending' | 'applied' | 'failed'
  attempts: number
  last_error: string
  reason: string
  updated_at: string
}

export interface DriftEvent {
  id: number
  instance_id: number | null
  instance_name: string
  payload_kind: string
  summary: string
  details: string
  corrected: boolean
  /** How many passes have found exactly this. 1 unless it kept repeating. */
  occurrences: number
  /** When the last of those passes was. Null while it has only happened once. */
  last_seen_at: string | null
  /** The correction attempt's duration in ms; 0 when nothing was attempted. */
  took_ms: number
  created_at: string
}

export interface ListSize {
  url: string
  kind: string
  rules_count: number
  /** Whether every node that answered reported the same size for this list. */
  agreed: boolean
  per_instance: { instance_id: number; instance_name: string; rules_count: number }[]
}

export interface FilterSizes {
  lists: ListSize[]
  total_rules: number
  instances_reporting: number
  instances_total: number
}

export interface Notifier {
  id: number
  name: string
  type: NotifierType
  url: string
  has_token: boolean
  enabled: boolean
  events: string[]
  last_error: string
}

export interface DashboardStats {
  /**
   * Whether the hub holds anything to replicate — a rule, a subscription, or a
   * managed section with something in it. False means reconciliation is skipping
   * its passes deliberately, and the card has to say so: an unconfigured hub
   * would otherwise look exactly like a timer that stopped.
   */
  replicating: boolean
  instances_total: number
  last_sync_at: string | null
  instances_synced: number
  managed_sections: number
  versions_total: number
  instances_online: number
  instances_unreachable: number
  instances_disabled: number
  rules_total: number
  rules_allow: number
  rules_block: number
  filter_lists_total: number
  filter_lists_enabled: number
  pending_jobs: number
  failed_jobs: number
  recent_drift: number
  querylog_buffered: number
}

export interface TopEntry {
  name: string
  count: number
}

export interface Traffic {
  queries: number
  blocked: number
  block_rate: number
  replaced_safebrowsing: number
  /** In whatever unit the instances report it; rendered as AdGuard labels it. */
  avg_processing_time_ms: number
  series_queries: number[]
  series_blocked: number[]
  time_units: string
  top_queried: TopEntry[]
  top_blocked: TopEntry[]
  top_clients: TopEntry[]
  /** A total short by one node reads as a quiet day, so the page says who answered. */
  instances_reporting: number
  instances_total: number
}

export interface ReconcileReport {
  instance_id: number
  instance_name: string
  checked: boolean
  error: string
  corrected: boolean
  differences: { payload_kind: string; summary: string; details: Record<string, unknown> }[]
}

export interface ImportResult {
  instance: string
  rules_imported: number
  rules_skipped: number
  filter_lists_imported: number
  sections_imported: string[]
  sections_unsupported: string[]
  /** Adopted but left switched off — enabling them can lock a node out. */
  sections_needing_review: string[]
  replaced: boolean
}

export interface SyncResult {
  instances: number
  failed: Record<string, string>
}

export interface ConnectionResult {
  ok: boolean
  version: string
  error: string
}

export interface ConfigField {
  key: string
  label: string
  type: 'bool' | 'int' | 'text' | 'lines' | 'select' | 'pairs' | 'clients'
  help: string
  unit: string
  options: [string, string][]
  /** Heading this field belongs under, defined in sections.py. */
  group: string
}

export interface ConfigSection {
  name: string
  title: string
  description: string
  notes: string
  /** Enabling this can lock the operator out of a node; the UI warns and confirms. */
  risky: boolean
  /** This area has a page of its own; the combined list links there instead. */
  own_page: boolean
  fields: ConfigField[]
  managed: boolean
  has_data: boolean
  keys: string[]
  data: Record<string, unknown>
  /** Non-empty when the section is managed but cannot safely be pushed. */
  skipped_reason: string
  updated_at: string
}

export interface Version {
  id: number
  label: string
  author: string
  kind: string
  summary: string
  created_at: string
}

export interface VersionDiff {
  from_id: number
  to_id: number | null
  to_label: string
  summary: string
  changes: {
    rules: { added: string[]; removed: string[]; changed: { key: string }[] }
    filter_lists: { added: string[]; removed: string[]; changed: { key: string }[] }
    sections: Record<
      string,
      {
        keys: Record<string, { before: unknown; after: unknown }>
        managed?: { before: boolean; after: boolean }
      }
    >
    empty: boolean
  }
}

export interface VersionRestoreResult {
  version_id: number
  rules: number
  filter_lists: number
  sections: number
  pushed: boolean
}

export interface BackupRestoreResult {
  rules: number
  filter_lists: number
  sections: number
  instances_added: number
  /** Restored nodes carry no password: the backup deliberately holds none. */
  instances_need_password: number
  pushed: boolean
}

export interface HubSettings {
  reconcile_enabled: boolean
  reconcile_interval: number
  retry_interval: number
  querylog_enabled: boolean
  querylog_poll_interval: number
  querylog_buffer_size: number
  http_timeout: number
  external_api_enabled: boolean
  update_check_enabled: boolean
  /** Accepted [min, max] per field, so the form can bound its inputs. */
  limits: Record<string, [number, number]>
}

/**
 * One streak of reconciliation passes that ended the same way.
 *
 * A row is a streak, not a pass: consecutive passes with the same outcome are
 * folded and counted, so a healthy hub answers with one row rather than three
 * hundred a day saying nothing happened.
 */
export interface ReconcileRun {
  id: number
  /** The first pass of the streak — since when it has been like this. */
  started_at: string
  /** The most recent one — whether the timer is still running. */
  last_at: string
  passes: number
  instances: number
  unreachable: number
  with_differences: number
  corrected: number
  out_of_sync: number
  last_took_ms: number
  /** The worst pass of the streak; an average would hide the outlier. */
  max_took_ms: number
}

/** One refused sign-in, as Settings shows it. */
export interface FailedSignIn {
  source: string
  door: string
  reason: string
  at: string
}

export interface SignInActivity {
  failures: FailedSignIn[]
  lockouts: { source: string; seconds_left: number }[]
  max_failures: number
  window_seconds: number
}

/** How the hub was installed, which decides what it can do about an update. */
export type InstallMethod = 'docker' | 'native' | 'source'

export interface UpdateStatus {
  current: string
  /** Empty when the check is off, found nothing, or could not be made. */
  latest: string
  update_available: boolean
  release_url: string
  published_at: string
  install_method: InstallMethod
  /** Whether this install could apply the update itself. */
  self_update: boolean
  checked_at: number
  /** Why the last check produced no answer. Empty is not an error. */
  error: string
  enabled: boolean
}

/** An upgrade the hub asked systemd to perform, as far as the hub can see it. */
export interface UpdateRun {
  requested: boolean
  running: boolean
  finished: boolean
  /** Asked for, but nothing picked it up — the update units are not installed. */
  stalled: boolean
  exit_status: number | null
  log: string
}

/** One line of the hub's own application log. */
export interface LogLine {
  /** Monotonic within a run of the hub — the cursor for "what is new". */
  seq: number
  level: string
  logger: string
  message: string
  at: string
}

export interface LogPage {
  lines: LogLine[]
  cursor: number
  latest: number
  capacity: number
}

/** One archived reconciliation finding, as written to disk when it happened. */
export interface DriftArchiveEntry {
  /** Position counted back from the newest entry — the paging cursor. */
  offset: number
  at: string
  instance: string
  payload_kind: string
  summary: string
  corrected: boolean
  took_ms: number
  /** Untrimmed, unlike the drift table's: the file has no reason to cap it. */
  details: Record<string, unknown>
}

export interface DriftArchivePage {
  entries: DriftArchiveEntry[]
  /** Whether anything older sits behind this page. */
  more: boolean
  /** False when the archive is switched off, so the page can say so. */
  enabled: boolean
}
