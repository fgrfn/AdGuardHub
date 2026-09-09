import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { DriftEvent, Instance, PushJob, ReconcileReport, Traffic } from '../api/types'
import { Badge, Banner, Card, Empty, PageHeader } from '../components/ui'
import { BlockRateRing, RankList, SeriesChart } from '../components/charts'
import { DriftFacts } from '../components/DriftFacts'
import { ReconcileRuns } from '../components/ReconcileRuns'
import { formatCount, formatTime } from '../format'
import { errorMessage, useResource } from '../hooks/useApi'
import { NodeUpdate } from '../components/NodeUpdate'
import { useT } from '../i18n'

/** How often the traffic numbers are re-read. The backend holds them for ~10s. */
const TRAFFIC_POLL_MS = 15_000

export default function Dashboard() {
  const t = useT()
  const stats = useResource(() => api.dashboard())
  const traffic = useResource<Traffic>(() => api.traffic())
  const instances = useResource<Instance[]>(() => api.instances())
  const jobs = useResource<PushJob[]>(() => api.jobs(true))
  const drift = useResource<DriftEvent[]>(() => api.drift(25))
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const reloadTraffic = traffic.reload
  useEffect(() => {
    const timer = setInterval(() => void reloadTraffic(), TRAFFIC_POLL_MS)
    return () => clearInterval(timer)
  }, [reloadTraffic])

  async function run(action: () => Promise<string>) {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      setMessage(await action())
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
      await Promise.all([stats.reload(), jobs.reload(), drift.reload(), instances.reload()])
    }
  }

  const forceSync = () =>
    run(async () => {
      const result = await api.sync()
      const failed = Object.keys(result.failed)
      return failed.length
        ? t('Pushed to {count} instance(s); {names} failed and were queued for retry.', {
            count: result.instances,
            names: failed.join(', '),
          })
        : t('Pushed the full configuration to {count} instance(s).', { count: result.instances })
    })

  const reconcileNow = (applyFixes = true) =>
    run(async () => {
      const reports: ReconcileReport[] = await api.reconcile(applyFixes)
      // Report what was *found*, not just what was corrected: a difference that could
      // not be pushed away still belongs in the message, or the drift log below it
      // contradicts what this line says.
      const withDrift = reports.filter((report) => report.differences.length > 0)
      const corrected = withDrift.filter((report) => report.corrected)
      const failed = withDrift.filter((report) => report.checked && !report.corrected)
      const unreachable = reports.filter((report) => !report.checked)

      if (!withDrift.length && !unreachable.length) {
        return t('No drift found — every instance matches.')
      }

      // A check that changed nothing must not borrow the wording of one that did.
      // Reusing the branches below would report drift it never attempted to push
      // as drift that "could not be applied", which reads as a failure.
      if (!applyFixes) {
        const names = (list: ReconcileReport[]) => list.map((item) => item.instance_name).join(', ')
        const parts: string[] = []
        if (withDrift.length) parts.push(t('drift on {names}', { names: names(withDrift) }))
        if (unreachable.length)
          parts.push(t('could not reach {names}', { names: names(unreachable) }))
        return t('Checked only, nothing was changed: {parts}. Use Reconcile to apply.', {
          parts: parts.join('; '),
        })
      }

      const names = (list: ReconcileReport[]) => list.map((item) => item.instance_name).join(', ')
      const parts: string[] = []
      if (corrected.length) parts.push(t('corrected {names}', { names: names(corrected) }))
      if (failed.length) {
        const detail = failed[0].error ? ` (${failed[0].error})` : ''
        parts.push(
          t('found drift on {names} that could not be applied{detail}', {
            names: names(failed),
            detail,
          }),
        )
      }
      if (unreachable.length)
        parts.push(t('could not reach {names}', { names: names(unreachable) }))
      return t('Reconciliation: {parts}.', { parts: parts.join('; ') })
    })

  const retryQueue = () =>
    run(async () => {
      const result = await api.retryJobs()
      return result.recovered
        ? t('Retried the queue — {count} instance(s) are back in sync.', {
            count: result.recovered,
          })
        : t('Nothing in the retry queue could be applied yet.')
    })

  // Clearing the log resolves nothing — the next run finds a difference that is
  // still there and writes it again. It is for the opposite case: findings whose
  // cause is already fixed, left behind as noise.
  const clearDrift = () => {
    if (
      !confirm(
        t('Delete every entry in the drift log?') +
          '\n\n' +
          t('This removes the record, not the cause. Anything still out of step is found again by the next reconciliation run.'),
      )
    ) {
      return
    }
    return run(async () => {
      const { deleted } = await api.clearDrift()
      return t('Drift log cleared ({count} removed).', { count: deleted })
    })
  }

  const value = stats.data
  const live = traffic.data
  const silent = live ? live.instances_total - live.instances_reporting : 0

  return (
    <>
      <PageHeader
        title={t('Dashboard')}
        description={t('AdGuardHub is the single source of truth. Every change here is pushed to all instances immediately; reconciliation catches anything that drifts.')}
        actions={
          <>
            <button onClick={forceSync} disabled={busy}>
              {t('Push everything now')}
            </button>
            {/* Before Reconcile, because that is the order you would use them:
                see what a run would touch, then let it. */}
            <button
              onClick={() => reconcileNow(false)}
              disabled={busy}
              title={t('Compare every instance against the hub and report differences without correcting them.')}
            >
              {t('Check only')}
            </button>
            <button onClick={() => reconcileNow()} disabled={busy}>
              {t('Reconcile')}
            </button>
          </>
        }
      />

      {error ? <Banner kind="error">{error}</Banner> : null}
      {message ? <Banner kind="ok">{message}</Banner> : null}
      {stats.error ? <Banner kind="error">{stats.error}</Banner> : null}

      {value && value.instances_total === 0 ? (
        <Banner kind="warn">
          {t('No instances yet.')}{' '}
          <Link to="/instances">{t('Add your AdGuard Home instances')}</Link>
          {t(', then import one of them as the master to seed the hub.')}
        </Banner>
      ) : null}

      {live && live.instances_total > 0 ? (
        <Card>
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 16,
              flexWrap: 'wrap',
              marginBottom: 18,
            }}
          >
            <div>
              <h2>{t('Traffic across all nodes')}</h2>
              <p className="hint" style={{ margin: '3px 0 0' }}>
                {t('Summed from every instance that answered.')}
              </p>
            </div>
            <Badge tone={silent ? 'pending' : 'applied'}>
              {t('{reporting} of {total} nodes reporting', {
                reporting: live.instances_reporting,
                total: live.instances_total,
              })}
            </Badge>
          </div>

          {/* A total short by one node looks like a quiet day, so never let it pass silently. */}
          {silent ? (
            <Banner kind="warn">
              {silent > 1
                ? t(
                    '{count} nodes are not reporting, so these numbers are lower than what your network actually did.',
                    { count: silent },
                  )
                : t(
                    '1 node is not reporting, so these numbers are lower than what your network actually did.',
                  )}
            </Banner>
          ) : null}

          <div className="grid" style={{ marginBottom: 22 }}>
            <div className="stat">
              <div className="value">{formatCount(live.queries)}</div>
              <div className="label">{t('DNS queries')}</div>
            </div>
            <div className="stat">
              <div className="value" style={{ color: 'var(--series-b-ink)' }}>
                {formatCount(live.blocked)}
              </div>
              <div className="label">{t('Blocked by filters')}</div>
            </div>
            <div className="stat">
              <div className="value">{formatCount(live.replaced_safebrowsing)}</div>
              <div className="label">{t('Blocked by safe browsing')}</div>
            </div>
            <div className="stat">
              <div className="value">
                {live.avg_processing_time_ms.toFixed(live.avg_processing_time_ms < 1 ? 2 : 1)} ms
              </div>
              <div className="label">{t('Average response time')}</div>
            </div>
          </div>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 1fr) 240px',
              gap: 30,
              alignItems: 'center',
            }}
            className="traffic-row"
          >
            <div>
              <div className="legend">
                <span>
                  <i style={{ background: 'var(--series-a)' }} />
                  {t('DNS queries')}
                </span>
                <span>
                  <i style={{ background: 'var(--series-b)' }} />
                  {t('Blocked')}
                </span>
              </div>
              <SeriesChart
                queries={live.series_queries}
                blocked={live.series_blocked}
                unit={live.time_units}
              />
            </div>
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 12,
              }}
            >
              <BlockRateRing rate={live.block_rate} />
              <p
                style={{
                  margin: 0,
                  fontSize: 12.5,
                  color: 'var(--dim)',
                  textAlign: 'center',
                  maxWidth: '30ch',
                }}
              >
                {t('{blocked} of {queries} queries never left the network.', {
                  blocked: formatCount(live.blocked),
                  queries: formatCount(live.queries),
                })}
              </p>
            </div>
          </div>
        </Card>
      ) : null}

      {live && live.instances_reporting > 0 ? (
        <div className="cards-3" style={{ marginBottom: 16 }}>
          <Card title={t('Top queried domains')}>
            <RankList entries={live.top_queried} tone="a" />
          </Card>
          <Card title={t('Top blocked domains')}>
            <RankList entries={live.top_blocked} tone="b" />
          </Card>
          <Card title={t('Top clients')}>
            <RankList entries={live.top_clients} tone="a" />
          </Card>
        </div>
      ) : null}

      <div className="split">
        <Card>
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 16,
              marginBottom: 12,
              flexWrap: 'wrap',
            }}
          >
            <div>
              <h2>{t('Nodes')}</h2>
              <p className="hint" style={{ margin: '3px 0 0' }}>
                {t('Last successful push {when}.', {
                  when: value?.last_sync_at ? formatTime(value.last_sync_at) : t('never'),
                })}
              </p>
            </div>
            <button className="small" onClick={retryQueue} disabled={busy}>
              {t('Retry queue')}
            </button>
          </div>

          {instances.data?.length ? (
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              {instances.data.map((instance) => (
                <div
                  key={instance.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 14,
                    padding: '11px 2px',
                    borderBottom: '1px solid var(--line)',
                    flexWrap: 'wrap',
                  }}
                >
                  <span
                    className={`node-dot${
                      instance.status === 'unreachable'
                        ? ' down'
                        : instance.status === 'disabled'
                          ? ' off'
                          : ''
                    }`}
                    style={{ width: 8, height: 8 }}
                  />
                  <span style={{ fontWeight: 600, minWidth: 130 }}>{instance.name}</span>
                  <span className="mono" style={{ color: 'var(--dim)', minWidth: 170 }}>
                    {instance.base_url}
                  </span>
                  <Badge tone={instance.status}>{t(instance.status)}</Badge>
                  <NodeUpdate instance={instance} />
                  <span style={{ marginLeft: 'auto', color: 'var(--dim)', fontSize: 13 }}>
                    {formatTime(instance.last_synced_at)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Empty>{t('No instances configured yet.')}</Empty>
          )}
        </Card>

        <Card title={t('Hub')}>
          {value ? (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                gap: '16px 12px',
              }}
            >
              <Figure value={value.rules_total} label={t('Rules')} />
              <Figure value={value.managed_sections} label={t('Replicated settings')} to="/config" />
              <Figure value={value.filter_lists_enabled} label={t('Active subscriptions')} />
              <Figure
                value={value.pending_jobs + value.failed_jobs}
                label={t('Queued pushes')}
                alert={value.pending_jobs + value.failed_jobs > 0}
              />
              <Figure value={value.versions_total} label={t('Config versions')} to="/history" />
              <Figure value={value.recent_drift} label={t('Drift events')} />
            </div>
          ) : (
            <Empty>{t('Loading…')}</Empty>
          )}
        </Card>
      </div>

      {jobs.data && jobs.data.length ? (
        <Card
          title={t('Retry queue')}
          hint={t('A push that could not be delivered waits here and is retried automatically until it lands.')}
        >
          <div className="table-wrap">
            <table className="stack-on-phone">
              <thead>
                <tr>
                  <th>{t('Instance')}</th>
                  <th>{t('Payload')}</th>
                  <th>{t('Status')}</th>
                  <th>{t('Attempts')}</th>
                  <th>{t('Last error')}</th>
                  <th>{t('Updated')}</th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.map((job) => (
                  <tr key={job.id}>
                    <td data-label={t('Instance')}>{job.instance_name}</td>
                    <td data-label={t('Payload')}>{t(job.payload_kind)}</td>
                    <td data-label={t('Status')}>
                      <Badge tone={job.status}>{t(job.status)}</Badge>
                    </td>
                    <td data-label={t('Attempts')}>{job.attempts}</td>
                    <td data-label={t('Last error')} className="mono">
                      {job.last_error || '—'}
                    </td>
                    <td data-label={t('Updated')}>{formatTime(job.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}

      {/* Above the drift log, because it settles the question the drift log
          cannot: an empty one is either a healthy fleet or a reconciler that
          stopped, and those used to render identically. */}
      <ReconcileRuns />

      <Card
        title={t('Drift log')}
        hint={t('Differences found by reconciliation. Corrections are applied automatically and never happen silently.')}
        actions={
          <button
            className="ghost"
            onClick={clearDrift}
            disabled={busy || !drift.data?.length}
            title={t('Delete the entries. A difference that still exists is found again on the next run.')}
          >
            {t('Clear log')}
          </button>
        }
      >
        {drift.data && drift.data.length ? (
          <div className="table-wrap">
            <table className="stack-on-phone">
              <thead>
                <tr>
                  <th>{t('When')}</th>
                  <th>{t('Instance')}</th>
                  <th>{t('Payload')}</th>
                  <th>{t('Summary')}</th>
                  <th>{t('Fixed')}</th>
                </tr>
              </thead>
              <tbody>
                {drift.data.map((event) => (
                  <tr key={event.id}>
                    <td data-label={t('When')}>{formatTime(event.created_at)}</td>
                    <td data-label={t('Instance')}>{event.instance_name}</td>
                    <td data-label={t('Payload')}>{t(event.payload_kind)}</td>
                    <td data-label={t('Summary')}>
                      {event.summary}
                      {event.details && event.details !== '{}' ? (
                        <details className="details">
                          <summary>{t('details')}</summary>
                          <pre>{prettyJson(event.details)}</pre>
                        </details>
                      ) : null}
                      <DriftFacts event={event} />
                    </td>
                    <td data-label={t('Fixed')}>
                      <Badge tone={event.corrected ? 'applied' : 'pending'}>
                        {event.corrected ? t('corrected') : t('detected')}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>{t('No drift recorded yet.')}</Empty>
        )}
      </Card>
    </>
  )
}

function Figure({
  value,
  label,
  to,
  alert = false,
}: {
  value: number | string
  label: string
  to?: string
  alert?: boolean
}) {
  const body = (
    <div
      style={{
        fontSize: 22,
        fontWeight: 680,
        lineHeight: 1.1,
        fontVariantNumeric: 'tabular-nums',
        color: alert ? 'var(--danger-ink)' : undefined,
      }}
    >
      {value}
    </div>
  )
  return (
    <div>
      {to ? <Link to={to}>{body}</Link> : body}
      <div style={{ color: 'var(--dim)', fontSize: 12, marginTop: 5 }}>{label}</div>
    </div>
  )
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}
