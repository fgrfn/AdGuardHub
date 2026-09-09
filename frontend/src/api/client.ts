import type {
  AuthState,
  BackupRestoreResult,
  ConfigSection,
  ConnectionResult,
  DashboardStats,
  DriftArchivePage,
  DriftEvent,
  FilterList,
  FilterSizes,
  HubSettings,
  ImportResult,
  Instance,
  ListKind,
  LogPage,
  Notifier,
  PushJob,
  QueryLogEntry,
  ReconcileReport,
  Rule,
  RuleKind,
  RuleOrigin,
  SignInActivity,
  SyncResult,
  Traffic,
  UpdateRun,
  UpdateStatus,
  Version,
  VersionDiff,
  VersionRestoreResult,
} from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: init.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') {
        detail = body.detail
      } else if (Array.isArray(body?.detail)) {
        // FastAPI validation errors arrive as a list of {loc, msg}.
        detail = body.detail.map((item: { msg?: string }) => item.msg ?? '').join('; ')
      }
    } catch {
      // Keep the status-line fallback.
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

const get = <T,>(path: string) => request<T>(path)
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
const put = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body) })
const patch = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
const del = <T = void,>(path: string) => request<T>(path, { method: 'DELETE' })

function query(params: Record<string, string | boolean | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue
    search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  health: () => get<{ status: string; version: string }>('/api/health'),
  authState: () => get<AuthState>('/api/auth/state'),
  setup: (username: string, password: string) =>
    post<AuthState>('/api/auth/setup', { username, password }),
  login: (username: string, password: string) =>
    post<AuthState>('/api/auth/login', { username, password }),
  logout: () => post<{ ok: boolean }>('/api/auth/logout'),
  changePassword: (currentPassword: string, newPassword: string) =>
    post<{ ok: boolean }>('/api/auth/password', {
      current_password: currentPassword,
      new_password: newPassword,
    }),

  dashboard: () => get<DashboardStats>('/api/dashboard'),
  traffic: () => get<Traffic>('/api/traffic'),
  sync: () => post<SyncResult>('/api/sync'),
  reconcile: (applyFixes = true) =>
    post<ReconcileReport[]>(`/api/reconcile${query({ apply_fixes: applyFixes })}`),

  instances: () => get<Instance[]>('/api/instances'),
  testConnection: (payload: {
    base_url: string
    username?: string
    password?: string
    verify_tls?: boolean
    adapter?: string
    instance_id?: number
  }) => post<ConnectionResult>('/api/instances/test-connection', payload),
  adapters: () => get<string[]>('/api/instances/adapters'),
  createInstance: (payload: {
    name: string
    base_url: string
    adapter?: string
    username?: string
    password?: string
    verify_tls?: boolean
    enabled?: boolean
  }) => post<Instance>('/api/instances', payload),
  updateInstance: (id: number, payload: Partial<Instance> & { password?: string }) =>
    patch<Instance>(`/api/instances/${id}`, payload),
  deleteInstance: (id: number) => del(`/api/instances/${id}`),
  testInstance: (id: number) => post<{ ok: string; version: string }>(`/api/instances/${id}/test`),
  pushInstance: (id: number) => post<{ ok: string; error: string }>(`/api/instances/${id}/push`),
  importInstance: (id: number, payload: { replace: boolean; sections?: string[] }) =>
    post<ImportResult>(`/api/instances/${id}/import`, { ...payload, push_after_import: true }),

  rules: (params: { kind?: RuleKind; origin?: RuleOrigin; search?: string } = {}) =>
    get<Rule[]>(`/api/rules${query(params)}`),
  createRule: (payload: { text: string; origin?: RuleOrigin; comment?: string }) =>
    post<Rule>('/api/rules', payload),
  updateRule: (id: number, payload: { text?: string; enabled?: boolean; comment?: string }) =>
    patch<Rule>(`/api/rules/${id}`, payload),
  deleteRule: (id: number) => del(`/api/rules/${id}`),
  allowDomain: (domain: string, origin: RuleOrigin = 'allowlist', comment = '') =>
    post<Rule>(`/api/rules/allow${query({ origin })}`, { domain, comment }),
  blockDomain: (domain: string, origin: RuleOrigin = 'custom', comment = '') =>
    post<Rule>(`/api/rules/block${query({ origin })}`, { domain, comment }),
  bulkRules: (text: string, origin: RuleOrigin = 'custom') =>
    post<Rule[]>('/api/rules/bulk', { text, origin }),

  filterLists: (kind?: ListKind) => get<FilterList[]>(`/api/filter-lists${query({ kind })}`),
  filterSizes: () => get<FilterSizes>('/api/filter-lists/sizes'),
  createFilterList: (payload: { name: string; url: string; kind: ListKind }) =>
    post<FilterList>('/api/filter-lists', payload),
  updateFilterList: (id: number, payload: { name?: string; enabled?: boolean }) =>
    patch<FilterList>(`/api/filter-lists/${id}`, payload),
  deleteFilterList: (id: number) => del(`/api/filter-lists/${id}`),

  queryLog: (params: { limit?: number; search?: string; instance?: string; blocked_only?: boolean }) =>
    get<QueryLogEntry[]>(`/api/querylog${query(params)}`),
  refreshQueryLog: () => post<{ new_entries: number }>('/api/querylog/refresh'),

  jobs: (openOnly = true) => get<PushJob[]>(`/api/jobs${query({ open_only: openOnly })}`),
  retryJobs: () => post<{ recovered: number }>('/api/jobs/retry'),
  deleteJob: (id: number) => del(`/api/jobs/${id}`),
  drift: (limit = 100) => get<DriftEvent[]>(`/api/drift${query({ limit })}`),
  clearDrift: () => del<{ deleted: number }>('/api/drift'),

  notifiers: () => get<Notifier[]>('/api/settings/notifiers'),
  notifierMeta: () => get<{ types: string[]; events: string[] }>('/api/settings/notifiers/meta'),
  signIns: () => get<SignInActivity>('/api/settings/sign-ins'),
  createNotifier: (payload: {
    name: string
    type: string
    url: string
    token?: string
    events: string[]
  }) => post<Notifier>('/api/settings/notifiers', payload),
  updateNotifier: (
    id: number,
    payload: { name?: string; url?: string; token?: string; enabled?: boolean; events?: string[] },
  ) => patch<Notifier>(`/api/settings/notifiers/${id}`, payload),
  deleteNotifier: (id: number) => del(`/api/settings/notifiers/${id}`),
  testNotifier: (id: number) =>
    post<{ ok: string; error: string }>(`/api/settings/notifiers/${id}/test`),

  updateStatus: (force = false) =>
    get<UpdateStatus>(`/api/settings/update${query({ force })}`),
  log: (cursor = 0) => get<LogPage>(`/api/settings/log${query({ cursor })}`),
  driftArchive: (after = 0, limit = 100) =>
    get<DriftArchivePage>(`/api/settings/drift-archive${query({ after, limit })}`),
  updateRun: () => get<UpdateRun>('/api/settings/update/run'),
  startUpdate: () => post<UpdateRun>('/api/settings/update/run'),

  hubSettings: () => get<HubSettings>('/api/settings/hub'),
  finishOnboarding: () => post<void>('/api/settings/onboarding-complete'),
  saveHubSettings: (payload: Partial<Omit<HubSettings, 'limits'>>) =>
    put<HubSettings>('/api/settings/hub', payload),

  configSections: () => get<ConfigSection[]>('/api/config/sections'),
  updateSection: (name: string, payload: { managed?: boolean; data?: Record<string, unknown> }) =>
    patch<ConfigSection>(`/api/config/sections/${name}`, payload),

  versions: (limit = 50) => get<Version[]>(`/api/versions${query({ limit })}`),
  versionDiff: (id: number, against?: number) =>
    get<VersionDiff>(`/api/versions/${id}/diff${query({ against })}`),
  restoreVersion: (id: number) =>
    post<VersionRestoreResult>(`/api/versions/${id}/restore`),

  // The download is a plain link rather than a fetch: the endpoint answers with
  // a Content-Disposition, and letting the browser follow it keeps the filename
  // the server chose instead of rebuilding one around a blob.
  backupUrl: () => '/api/backup',
  restoreBackup: (document: unknown) =>
    post<BackupRestoreResult>('/api/backup/restore', document),
  // Same reasoning as the backup download above.
  diagnosticsUrl: () => '/api/diagnostics',
}
