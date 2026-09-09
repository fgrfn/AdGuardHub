/**
 * Whether the safety net is running, and what it has been finding.
 *
 * It sits above the drift log because it answers the question you have to
 * settle before the drift log means anything: an empty drift log is either a
 * healthy fleet or a reconciler that stopped weeks ago, and until this card
 * existed the dashboard rendered both the same way.
 *
 * One row is a streak, not a pass — consecutive passes with the same outcome
 * are folded and counted — so a healthy hub shows a single line saying "nothing
 * to correct, 4,032 passes since 15 August" rather than a tape of three hundred
 * a day saying nothing happened.
 */

import type { HubSettings, ReconcileRun } from '../api/types'
import { api } from '../api/client'
import { formatCount, formatDuration, formatTime } from '../format'
import { useResource } from '../hooks/useApi'
import { useT } from '../i18n'
import { verdict } from '../reconcileState'
import { Card, Empty } from './ui'

export function ReconcileRuns() {
  const t = useT()
  const runs = useResource<ReconcileRun[]>(() => api.reconcileRuns())
  const settings = useResource<HubSettings>(() => api.hubSettings())

  const { tone, headline, detail } = verdict(runs.data, settings.data, t)
  const rows = runs.data ?? []

  return (
    <Card
      title={t('Reconciliation')}
      hint={t(
        'What the safety net has been doing, including the passes that found nothing. One row is a run of passes that ended the same way.',
      )}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 9, marginBottom: 14 }}>
        <span className={`sync-dot ${tone}`} />
        <strong>{headline}</strong>
        {detail ? <span className="hint">{detail}</span> : null}
      </div>

      {rows.length ? (
        <div className="table-wrap">
          <table className="stack-on-phone">
            <thead>
              <tr>
                <th>{t('Since')}</th>
                <th>{t('Last pass')}</th>
                <th>{t('Passes')}</th>
                <th>{t('Outcome')}</th>
                <th>{t('Duration')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td data-label={t('Since')}>{formatTime(row.started_at)}</td>
                  <td data-label={t('Last pass')}>{formatTime(row.last_at)}</td>
                  <td data-label={t('Passes')}>{formatCount(row.passes)}</td>
                  <td data-label={t('Outcome')}>{outcome(row, t)}</td>
                  <td data-label={t('Duration')}>
                    {formatDuration(row.last_took_ms)}
                    {/* Only when it differs: on a steady streak the two are the
                        same number, and printing it twice reads as noise. */}
                    {row.max_took_ms > row.last_took_ms ? (
                      <span className="hint">
                        {' '}
                        {t('worst {duration}', { duration: formatDuration(row.max_took_ms) })}
                      </span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>{t('No pass recorded yet.')}</Empty>
      )}
    </Card>
  )
}

/** The streak's outcome as a phrase, in the order of what needs somebody first. */
function outcome(
  row: ReconcileRun,
  t: (text: string, vars?: Record<string, string | number>) => string,
): string {
  const parts: string[] = []
  if (row.unreachable) parts.push(t('{count} unreachable', { count: row.unreachable }))
  if (row.out_of_sync) parts.push(t('{count} not corrected', { count: row.out_of_sync }))
  if (row.corrected) parts.push(t('{count} corrected', { count: row.corrected }))
  if (!parts.length) return t('nothing to correct')
  return parts.join(', ')
}
