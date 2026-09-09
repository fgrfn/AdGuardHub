/**
 * The hub's own log, followed live.
 *
 * Not the query log — that is DNS traffic and has its own page. This is the hub
 * talking about itself: what it did on start, a push that failed, a refused
 * sign-in. Reading it used to mean `docker logs` or a shell, which is what you
 * do not have when you are looking at a hub from a phone, or helping somebody
 * else look at theirs.
 *
 * Polled with a cursor rather than streamed. The hub already runs one event
 * stream for the shell, and putting log lines through it would mean the act of
 * delivering a line producing a line — so this asks, on a timer, for whatever is
 * newer than the last sequence number it holds. Following along costs one small
 * response; nothing is re-sent.
 *
 * Narrowing happens here rather than in the API on purpose. The hub keeps 500
 * lines and this view keeps the same 500, so filtering server-side could not
 * reach one line further back — it would only mean a round trip every time the
 * operator changes their mind, and a cursor that means something different
 * depending on the filter that fetched it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { LogLine } from '../../api/types'
import { IconSearch } from '../../components/icons'
import { Card, Empty } from '../../components/ui'
import { useT } from '../../i18n'
import { DriftArchive } from './DriftArchive'
import { type LogFilter, NO_FILTER, isFiltering, matches, sourceLabel } from './logFilter'

const POLL_MS = 2000

/** Enough to scroll back through a restart, matching the buffer the hub keeps. */
const KEEP = 500

/**
 * Two registers of the same question — what did the hub do — kept on one page
 * because that is where somebody goes to ask it. The application log is the
 * tail of right now; the archive is what was written down and kept.
 */
export default function SettingsLog() {
  const t = useT()
  const [tab, setTab] = useState<'app' | 'archive'>('app')

  return (
    <>
      <div className="tabs">
        <button
          className={`tab${tab === 'app' ? ' active' : ''}`}
          onClick={() => setTab('app')}
        >
          {t('Application log')}
        </button>
        <button
          className={`tab${tab === 'archive' ? ' active' : ''}`}
          onClick={() => setTab('archive')}
        >
          {t('Drift archive')}
        </button>
      </div>
      {tab === 'app' ? <ApplicationLog /> : <DriftArchive />}
    </>
  )
}

function ApplicationLog() {
  const t = useT()
  const [lines, setLines] = useState<LogLine[]>([])
  const [following, setFollowing] = useState(true)
  const [filter, setFilter] = useState<LogFilter>(NO_FILTER)
  const [error, setError] = useState('')
  const cursor = useRef(0)
  const box = useRef<HTMLDivElement>(null)

  const poll = useCallback(async () => {
    try {
      const body = await api.log(cursor.current)
      cursor.current = body.cursor
      setError('')
      if (body.lines.length) {
        setLines((current) => [...current, ...body.lines].slice(-KEEP))
      }
    } catch {
      // A hub mid-restart stops answering for a moment. That is not worth a
      // banner — the next poll picks up where this one left off, and the cursor
      // means nothing is missed in between.
      setError(t('Waiting for the hub…'))
    }
  }, [t])

  useEffect(() => {
    void poll()
    if (!following) return
    const timer = setInterval(() => void poll(), POLL_MS)
    return () => clearInterval(timer)
  }, [poll, following])

  const shown = useMemo(() => lines.filter((line) => matches(line, filter)), [lines, filter])

  // Follow the tail, unless the operator has scrolled up to read something.
  useEffect(() => {
    const node = box.current
    if (node && following) node.scrollTop = node.scrollHeight
  }, [shown, following])

  const levels = [
    { value: 0, label: t('All levels') },
    { value: 20, label: t('Info and above') },
    { value: 30, label: t('Warnings and above') },
    { value: 40, label: t('Errors only') },
  ]

  // Built from the lines actually held, so the list never offers a source that
  // has said nothing. The current selection is kept in it regardless: a logger
  // whose last line has just scrolled out of the buffer must not silently drop
  // the filter the operator set.
  const sources = useMemo(() => {
    const present = new Set(lines.map((line) => line.logger).filter(Boolean))
    if (filter.logger) present.add(filter.logger)
    return [...present].sort()
  }, [lines, filter.logger])

  const filtering = isFiltering(filter)

  return (
    <Card
      title={t('Application log')}
      hint={t(
        'What the hub itself is doing. Kept in memory only — the last {count} lines, cleared on restart. Your container or systemd log remains the record.',
        { count: String(KEEP) },
      )}
    >
      <div className="actions" style={{ marginBottom: 12 }}>
        <label className="checkbox" style={{ marginBottom: 0 }}>
          <input
            type="checkbox"
            checked={following}
            onChange={(event) => setFollowing(event.target.checked)}
          />
          {t('Follow')}
        </label>
        <button type="button" className="small" onClick={() => setLines([])}>
          {t('Clear view')}
        </button>
        {error ? <span className="hint">{error}</span> : null}
      </div>

      <div className="filters" style={{ marginBottom: 12 }}>
        <div className="search">
          <IconSearch />
          <input
            value={filter.text}
            aria-label={t('Search the log')}
            placeholder={t('Search the lines')}
            onChange={(event) => setFilter({ ...filter, text: event.target.value })}
          />
        </div>
        <select
          aria-label={t('Filter by level')}
          style={{ flex: '0 0 175px' }}
          value={filter.floor}
          onChange={(event) => setFilter({ ...filter, floor: Number(event.target.value) })}
        >
          {levels.map((level) => (
            <option key={level.value} value={level.value}>
              {level.label}
            </option>
          ))}
        </select>
        <select
          aria-label={t('Filter by source')}
          style={{ flex: '0 0 190px' }}
          value={filter.logger}
          onChange={(event) => setFilter({ ...filter, logger: event.target.value })}
        >
          <option value="">{t('All sources')}</option>
          {sources.map((logger) => (
            <option key={logger} value={logger}>
              {sourceLabel(logger)}
            </option>
          ))}
        </select>
        {filtering ? (
          <button
            type="button"
            className="small"
            style={{ flex: '0 0 auto' }}
            onClick={() => setFilter(NO_FILTER)}
          >
            {t('Show all')}
          </button>
        ) : null}
        <span style={{ marginLeft: 'auto', color: 'var(--dim)', fontSize: 12.5 }}>
          {filtering
            ? t('{shown} of {total} lines', {
                shown: String(shown.length),
                total: String(lines.length),
              })
            : t('{count} lines', { count: String(lines.length) })}
        </span>
      </div>

      {shown.length ? (
        <div className="run-log" ref={box}>
          {shown.map((line) => (
            <div key={line.seq} className={`log-line ${line.level.toLowerCase()}`}>
              {line.message}
            </div>
          ))}
        </div>
      ) : (
        <Empty>{filtering ? t('No line matches this filter.') : t('Nothing logged yet.')}</Empty>
      )}
    </Card>
  )
}
