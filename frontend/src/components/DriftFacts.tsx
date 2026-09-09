/**
 * The two things a drift row's summary cannot say: whether it is still
 * happening, and how long the attempt took.
 *
 * A refusal repeats by definition — the node goes on not keeping the same thing
 * — so the log writes it once and counts the repeats on that row rather than
 * appending five hundred copies of it. The consequence is that the row's own
 * timestamp is when the fault was *first* seen, and without a count and a last
 * sighting beside it, one happening every five minutes right now reads exactly
 * like one that stopped this morning.
 *
 * Its own module rather than a helper inside the page, so the rule that matters
 * can be tested without standing up a dashboard: nothing is rendered for the
 * ordinary entry. A line saying "1×" under every row is noise, and noise under
 * every row is what teaches the eye to skip the place the interesting ones
 * appear.
 */

import type { DriftEvent } from '../api/types'
import { formatClock, formatDuration, formatTime } from '../format'
import { useT } from '../i18n'

export function DriftFacts({ event }: { event: DriftEvent }) {
  const t = useT()
  const parts: string[] = []

  if (event.occurrences > 1) {
    parts.push(t('found {count}×', { count: event.occurrences }))
    // Only alongside the count. On a single sighting the row's own When column
    // already says when, and repeating it would say nothing.
    if (event.last_seen_at) {
      parts.push(t('last {when}', { when: formatClock(event.last_seen_at) }))
    }
  }

  // Zero means nothing was attempted — a dry run, or a difference that cannot be
  // pushed away — which is not the same as an attempt that took no time.
  if (event.took_ms > 0) parts.push(formatDuration(event.took_ms))

  if (!parts.length) return null

  return (
    <div
      className="hint"
      style={{ marginTop: 4 }}
      title={event.last_seen_at ? formatTime(event.last_seen_at) : undefined}
    >
      {parts.join(' · ')}
    </div>
  )
}
