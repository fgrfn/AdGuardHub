/**
 * What the reconciliation history means, as a sentence.
 *
 * The question this exists to answer is not "what drifted" — the drift log has
 * that — but "is the safety net running at all". A pass that finds nothing
 * writes no drift, so an empty drift log has always meant either a healthy
 * fleet or a reconciler that stopped weeks ago, and the two look identical from
 * the dashboard.
 *
 * Its own module rather than logic inside the card, because the rule that
 * matters most is a judgement call about time — how late is late — and a
 * judgement call about time is the one thing that cannot be checked by looking
 * at the page.
 */

import type { HubSettings, ReconcileRun } from './api/types'
import { parseApiDate } from './format'

export type Tone = 'ok' | 'warn' | 'bad'

export interface Verdict {
  tone: Tone
  headline: string
  /** Empty when there is no history to describe. */
  detail: string
}

/**
 * How many intervals may pass before the timer is presumed stopped.
 *
 * One missed pass is a slow pass, a restart, or a node that took its time —
 * none of which is a fault, and all of which would cry wolf at 1×. Three
 * consecutive misses is not a coincidence: at the default five-minute interval
 * that is a quarter of an hour of silence from something that speaks every five
 * minutes.
 */
export const STALE_AFTER_INTERVALS = 3

export interface Strings {
  (text: string, vars?: Record<string, string | number>): string
}

export interface Options {
  now?: number
  /**
   * Whether the hub holds anything to replicate, from `/api/dashboard`.
   * `null` while it is still loading — treated as "assume it does", so a card
   * mid-load never accuses the reconciler of skipping.
   */
  replicating?: boolean | null
}

export function verdict(
  runs: ReconcileRun[] | null,
  settings: HubSettings | null,
  t: Strings,
  { now = Date.now(), replicating = null }: Options = {},
): Verdict {
  if (!runs || !settings) return { tone: 'ok', headline: t('Checking…'), detail: '' }

  // Before everything, including "switched off": a hub with no rule, no
  // subscription and no populated managed section has an empty desired state,
  // and since every push is full state, reconciling against it would clear the
  // nodes. So it does not run — and saying that plainly is the whole point,
  // because the alternative is a stopped-looking timer with no explanation on a
  // hub that is merely new.
  if (replicating === false) {
    return {
      tone: 'warn',
      headline: t('Nothing to replicate yet'),
      detail: t(
        'The hub holds no rule, no subscription and no imported settings, so it is not comparing your nodes against it. Reconciliation starts with the first of them.',
      ),
    }
  }

  // Before staleness: a timer that was switched off is not a timer that broke,
  // and reporting the second when the operator did the first is how a panel
  // teaches people to ignore it.
  if (!settings.reconcile_enabled) {
    return {
      tone: 'warn',
      headline: t('Reconciliation is switched off'),
      detail: t('Drift is not being detected or corrected. Changes you make are still pushed.'),
    }
  }

  const latest = runs[0]
  if (!latest) {
    return { tone: 'warn', headline: t('No pass recorded yet'), detail: '' }
  }

  // parseApiDate, not `new Date`: a timestamp without an offset is *defined* as
  // local time in JavaScript, so on a browser east of Greenwich this measurement
  // was inflated by the whole UTC offset. At any interval below eight hours that
  // alone exceeded the staleness threshold, and this card called a reconciler
  // that had just run "may have stopped" — the fault it exists to report, raised
  // against itself, permanently.
  const since = now - parseApiDate(latest.last_at).getTime()
  const overdue = settings.reconcile_interval * 1000 * STALE_AFTER_INTERVALS
  if (since > overdue) {
    return {
      tone: 'bad',
      headline: t('Reconciliation may have stopped'),
      detail: t('The last pass was {ago} ago, and the interval is {interval} seconds.', {
        ago: humanGap(since),
        interval: settings.reconcile_interval,
      }),
    }
  }

  const detail =
    latest.passes > 1
      ? t('{count} passes over {instances} node(s)', {
          count: latest.passes,
          instances: latest.instances,
        })
      : t('1 pass over {instances} node(s)', { instances: latest.instances })

  if (latest.unreachable > 0) {
    return { tone: 'bad', headline: t('{count} node(s) unreachable', { count: latest.unreachable }), detail }
  }
  if (latest.out_of_sync > 0) {
    return {
      tone: 'bad',
      headline: t('{count} correction(s) could not be pushed', { count: latest.out_of_sync }),
      detail,
    }
  }
  if (latest.with_differences > 0) {
    return {
      tone: 'warn',
      headline: t('Correcting drift on {count} node(s)', { count: latest.with_differences }),
      detail,
    }
  }
  return { tone: 'ok', headline: t('Nothing to correct'), detail }
}

/**
 * A rough age, for a sentence rather than a table.
 *
 * Deliberately coarse: the reader is deciding whether something stopped, and
 * "3 hours" answers that where "3 h 14 min 8 s" only makes them read further.
 */
export function humanGap(ms: number): string {
  const minutes = Math.floor(ms / 60_000)
  if (minutes < 60) return `${Math.max(1, minutes)} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours} h`
  return `${Math.floor(hours / 24)} d`
}
