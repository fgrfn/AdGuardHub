/**
 * The drift log as it was written, rather than as it is kept.
 *
 * The table on the dashboard is built to stay readable: 500 rows, one entry per
 * finding however often it repeats, rule lists trimmed to 25, and a button that
 * empties it. Each of those is right for a live view and wrong for evidence, so
 * the same findings are appended to a file as they happen — every sighting, in
 * order, untrimmed — and this is that file.
 *
 * Paged rather than followed. The application log beside it polls, because it is
 * a tail of what is happening now; this one is history, and history does not
 * need a two-second refresh. The cursor counts back from the newest entry so it
 * stays valid as the file grows and across a rotation.
 */

import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { DriftArchiveEntry } from '../../api/types'
import { Card, Empty } from '../../components/ui'
import { formatDuration, formatTime } from '../../format'
import { errorMessage } from '../../hooks/useApi'
import { useT } from '../../i18n'

const PAGE = 100

/**
 * What makes two rows the same finding.
 *
 * The cursor counts back from the newest entry, so if the hub archives
 * something between one page and the next, the window shifts and the older page
 * can repeat an entry the reader already has. One pass writes at most one entry
 * per node and payload kind, so this quartet is unique — and a duplicated row in
 * something people read as evidence is worth two lines to avoid.
 */
function identity(entry: DriftArchiveEntry): string {
  return `${entry.at}|${entry.instance}|${entry.payload_kind}|${entry.summary}`
}

export function DriftArchive() {
  const t = useT()
  const [entries, setEntries] = useState<DriftArchiveEntry[]>([])
  const [more, setMore] = useState(false)
  const [enabled, setEnabled] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(
    async (after: number) => {
      setBusy(true)
      try {
        const body = await api.driftArchive(after, PAGE)
        setEnabled(body.enabled)
        setMore(body.more)
        // Replacing on the first page rather than merging, so *Reload* is a
        // reload and not a second copy of everything.
        setEntries((current) => {
          if (after === 0) return body.entries
          const held = new Set(current.map(identity))
          return [...current, ...body.entries.filter((item) => !held.has(identity(item)))]
        })
        setError('')
      } catch (problem) {
        setError(errorMessage(problem))
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  useEffect(() => {
    void load(0)
  }, [load])

  return (
    <Card
      title={t('Drift archive')}
      hint={t(
        'Every reconciliation finding as it happened, written to a file beside the database. Unlike the drift log on the dashboard it keeps the repeats, keeps the full rule lists, and is not emptied by Clear log.',
      )}
      actions={
        <button className="ghost" onClick={() => void load(0)} disabled={busy}>
          {t('Reload')}
        </button>
      }
    >
      {error ? <p className="hint">{error}</p> : null}

      {!enabled ? (
        <Empty>
          {t('The drift archive is switched off. Set ADGUARDHUB_DRIFT_LOG_ENABLED=true to keep one.')}
        </Empty>
      ) : entries.length ? (
        <>
          <div className="table-wrap">
            <table className="stack-on-phone">
              <thead>
                <tr>
                  <th>{t('When')}</th>
                  <th>{t('Instance')}</th>
                  <th>{t('Payload')}</th>
                  <th>{t('Summary')}</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={identity(entry)}>
                    <td data-label={t('When')}>{formatTime(entry.at)}</td>
                    <td data-label={t('Instance')}>{entry.instance}</td>
                    <td data-label={t('Payload')}>{t(entry.payload_kind)}</td>
                    <td data-label={t('Summary')}>
                      {entry.summary}
                      <details className="details">
                        <summary>{t('details')}</summary>
                        <pre>{JSON.stringify(entry.details, null, 2)}</pre>
                      </details>
                      {entry.took_ms > 0 ? (
                        <div className="hint" style={{ marginTop: 4 }}>
                          {formatDuration(entry.took_ms)}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {more ? (
            <div className="actions" style={{ marginTop: 12 }}>
              <button className="small" onClick={() => void load(entries.length)} disabled={busy}>
                {t('Load older entries')}
              </button>
            </div>
          ) : null}
        </>
      ) : (
        <Empty>{t('Nothing archived yet — the hub has found no drift.')}</Empty>
      )}
    </Card>
  )
}
