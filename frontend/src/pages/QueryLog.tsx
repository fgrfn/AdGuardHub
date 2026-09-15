import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Instance, QueryLogEntry } from '../api/types'
import { Badge, Banner, Card, Empty, PageHeader } from '../components/ui'
import { IconChevron, IconSearch } from '../components/icons'
import { formatClock, formatTime } from '../format'
import { errorMessage, useResource } from '../hooks/useApi'
import { useEventStream } from '../hooks/useEventStream'
import { useT } from '../i18n'

const MAX_ROWS = 500

/**
 * Identity of a row, for remembering which one is expanded.
 *
 * Deliberately free of the array index: new entries arrive over SSE every few
 * seconds and shift every index down, which would silently collapse the row the
 * operator is reading — or worse, expand a different one under their cursor.
 */
const rowKey = (entry: QueryLogEntry) =>
  `${entry.instance}|${entry.time}|${entry.question}|${entry.client}`

/**
 * The aggregated view across every instance. The "Allow"/"Block" buttons write to the
 * central rule model and push to all instances — no matter which instance logged the row.
 */
export default function QueryLog() {
  const t = useT()
  const instances = useResource<Instance[]>(() => api.instances())
  const [rows, setRows] = useState<QueryLogEntry[]>([])
  const [search, setSearch] = useState('')
  const [instance, setInstance] = useState('')
  const [blockedOnly, setBlockedOnly] = useState(false)
  const [live, setLive] = useState(true)
  const [open, setOpen] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  const filters = useRef({ search, instance, blockedOnly, live })
  filters.current = { search, instance, blockedOnly, live }

  const load = useCallback(async () => {
    try {
      setRows(
        await api.queryLog({
          limit: MAX_ROWS,
          search,
          instance,
          blocked_only: blockedOnly,
        }),
      )
      setError('')
    } catch (caught) {
      setError(errorMessage(caught))
    }
  }, [search, instance, blockedOnly])

  useEffect(() => {
    void load()
  }, [load])

  // New entries arrive over SSE; re-reading the filtered snapshot keeps the
  // filter logic in one place (the backend) rather than duplicating it here.
  const connected = useEventStream((event) => {
    if (event.event === 'querylog' && filters.current.live) void load()
  })

  async function act(entry: QueryLogEntry, mode: 'allow' | 'block') {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const rule =
        mode === 'allow'
          ? await api.allowDomain(entry.question, 'querylog')
          : await api.blockDomain(entry.question, 'querylog')
      setMessage(t('{rule} added to the hub and pushed to every instance.', { rule: rule.text }))
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  async function refresh() {
    setBusy(true)
    try {
      await api.refreshQueryLog()
      await load()
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title={t('Query log')}
        description={t('Every instance\'s DNS queries in one stream. Allowing a domain here writes one rule that reaches all instances, so a failover cannot undo it.')}
        actions={
          <>
            <span style={{ alignSelf: 'center', color: 'var(--dim)', fontSize: 13 }}>
              <span className={`live-dot${connected && live ? ' on' : ''}`} />
              {connected ? (live ? 'live' : 'paused') : 'disconnected'}
            </span>
            <button onClick={() => setLive(!live)}>{live ? t('Pause') : t('Resume')}</button>
            <button onClick={refresh} disabled={busy}>
              {t('Poll now')}
            </button>
          </>
        }
      />

      {error ? <Banner kind="error">{error}</Banner> : null}
      {message ? <Banner kind="ok">{message}</Banner> : null}

      <Card>
        <div className="filters" style={{ marginBottom: 14 }}>
          <div className="search">
            <IconSearch />
            <input
              value={search}
              aria-label={t('Search the query log')}
              placeholder={t('Search a domain, a client or a rule')}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <select
            aria-label={t('Filter by instance')}
            style={{ flex: '0 0 190px' }}
            value={instance}
            onChange={(event) => setInstance(event.target.value)}
          >
            <option value="">{t('All nodes')}</option>
            {(instances.data ?? []).map((item) => (
              <option key={item.id} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
          <label className="checkbox" style={{ marginBottom: 0, flex: '0 0 auto' }}>
            <input
              type="checkbox"
              checked={blockedOnly}
              onChange={(event) => setBlockedOnly(event.target.checked)}
            />
            {t('Blocked only')}
          </label>
          <span style={{ marginLeft: 'auto', color: 'var(--dim)', fontSize: 12.5 }}>
            {rows.length === 1 ? t('1 entry') : t('{count} entries', { count: rows.length })}
          </span>
        </div>

        {rows.length ? (
          <div className="table-wrap">
            <table className="stack-on-phone">
              <thead>
                <tr>
                  <th style={{ width: 112 }}>{t('Time')}</th>
                  <th>{t('Domain')}</th>
                  <th style={{ width: 110 }}>{t('Response')}</th>
                  <th style={{ width: 190 }}>{t('Client')}</th>
                  <th style={{ width: 150 }}>{t('Node')}</th>
                  <th style={{ width: 180 }} />
                </tr>
              </thead>
              <tbody>
                {rows.map((entry, index) => {
                  const key = rowKey(entry)
                  const isOpen = open === key
                  return (
                    <Fragment key={`${key}|${index}`}>
                      <tr
                        className={`hoverable${isOpen ? ' open' : ''}`}
                        onClick={() => setOpen(isOpen ? '' : key)}
                        style={{ cursor: 'pointer' }}
                      >
                        <td data-label={t('Time')} className="mono" title={formatTime(entry.time)}>
                          {formatClock(entry.time)}
                        </td>
                        <td data-label={t('Domain')} className="mono">
                          {entry.question}
                        </td>
                        <td data-label={t('Response')}>
                          <Badge tone={entry.blocked ? 'blocked' : 'processed'}>
                            {entry.blocked ? t('blocked') : t('processed')}
                          </Badge>
                        </td>
                        <td data-label={t('Client')} className="mono">
                          {entry.client}
                        </td>
                        <td data-label={t('Node')} style={{ color: 'var(--dim)' }}>
                          {entry.instance}
                        </td>
                        <td className="right">
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: 6,
                              color: 'var(--dim)',
                              fontSize: 12,
                            }}
                          >
                            {isOpen ? t('collapse') : t('details')}
                            <IconChevron up={isOpen} size={13} />
                          </span>
                        </td>
                      </tr>

                      {isOpen ? (
                        <tr className="open">
                          <td colSpan={6} style={{ padding: '4px 12px 18px' }}>
                            <div className="detail">
                              <div className="detail-grid">
                                <Fact label={t('Matched rule')} value={entry.rule || '—'} mono />
                                {/* Directly under the rule, because the two are
                                    one answer: the rule says what matched, this
                                    says which of your lists to go and change. */}
                                <Fact label={t('From list')} value={entry.filter_list || '—'} />
                                <Fact label={t('Question type')} value={entry.question_type || '—'} />
                                <Fact label={t('Filtering result')} value={entry.answer_status || '—'} mono />
                                <Fact
                                  label={t('Processed in')}
                                  value={entry.elapsed_ms ? `${entry.elapsed_ms.toFixed(2)} ms` : '—'}
                                />
                                <Fact label={t('Upstream')} value={entry.upstream || '—'} mono />
                              </div>
                              <div
                                style={{
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: 10,
                                  flexWrap: 'wrap',
                                  borderTop: '1px solid var(--line)',
                                  paddingTop: 14,
                                }}
                              >
                                <button
                                  className="primary"
                                  onClick={() => act(entry, entry.blocked ? 'allow' : 'block')}
                                  disabled={busy || !entry.question}
                                >
                                  {entry.blocked ? t('Allow everywhere') : t('Block everywhere')}
                                </button>
                                <button
                                  onClick={() => act(entry, entry.blocked ? 'block' : 'allow')}
                                  disabled={busy || !entry.question}
                                >
                                  {entry.blocked ? t('Block everywhere') : t('Allow everywhere')}
                                </button>
                                <p
                                  style={{
                                    margin: '0 0 0 6px',
                                    color: 'var(--dim)',
                                    fontSize: 12.5,
                                    maxWidth: '62ch',
                                  }}
                                >
                                  {t(
                                    'The rule is written to the hub and pushed to every instance at once. It survives a failover, and reconciliation puts it back if a node loses it.',
                                  )}
                                </p>
                              </div>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>
            {t(
              'Nothing buffered yet. The hub polls each instance every few seconds — add an instance and give it a moment.',
            )}
          </Empty>
        )}
      </Card>
    </>
  )
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="dl">{label}</div>
      <div className={mono ? 'mono' : undefined}>{value}</div>
    </div>
  )
}
