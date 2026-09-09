/**
 * Telling a healthy fleet apart from a reconciler that stopped.
 *
 * Those two have always rendered identically — an empty drift log — because a
 * pass that finds nothing writes nothing. The whole point of the pass record is
 * to separate them, so the rules that do the separating are what these check:
 * how late is late, and the two states that must never be reported as a fault
 * because a person put the hub in them on purpose.
 */

import { describe, expect, it } from 'vitest'
import type { HubSettings, ReconcileRun } from './api/types'
import { STALE_AFTER_INTERVALS, humanGap, verdict } from './reconcileState'

// The identity translator: these tests are about the rules, not the wording.
const t = (text: string, vars?: Record<string, string | number>) =>
  text.replace(/\{(\w+)\}/g, (_, key) => String(vars?.[key] ?? ''))

const NOW = new Date('2026-09-09T00:40:00Z').getTime()
const INTERVAL = 300

function settings(overrides: Partial<HubSettings> = {}): HubSettings {
  return {
    reconcile_enabled: true,
    reconcile_interval: INTERVAL,
    retry_interval: 30,
    querylog_enabled: true,
    querylog_poll_interval: 5,
    querylog_buffer_size: 2000,
    http_timeout: 10,
    external_api_enabled: true,
    update_check_enabled: true,
    limits: {},
    ...overrides,
  }
}

function run(overrides: Partial<ReconcileRun> = {}): ReconcileRun {
  return {
    id: 1,
    started_at: '2026-09-08T19:44:00Z',
    last_at: '2026-09-09T00:38:00Z',
    passes: 60,
    instances: 2,
    unreachable: 0,
    with_differences: 0,
    corrected: 0,
    out_of_sync: 0,
    last_took_ms: 84,
    max_took_ms: 210,
    ...overrides,
  }
}

const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString()

describe('verdict', () => {
  it('says a quiet fleet is quiet, which an empty drift log could not', () => {
    const result = verdict([run()], settings(), t, { now: NOW })
    expect(result.tone).toBe('ok')
    expect(result.headline).toBe('Nothing to correct')
    expect(result.detail).toContain('60 passes')
  })

  it('calls the timer stopped once it has missed several intervals', () => {
    // The fault this exists to catch: nothing in the drift log, nothing wrong
    // with any node, and the reconciler has not run since last Tuesday.
    const result = verdict(
      [run({ last_at: ago(INTERVAL * (STALE_AFTER_INTERVALS + 1)) })],
      settings(),
      t,
      { now: NOW },
    )
    expect(result.tone).toBe('bad')
    expect(result.headline).toBe('Reconciliation may have stopped')
  })

  it('does not cry wolf over a single late pass', () => {
    // One missed pass is a slow node, a restart, or a pass that ran long. At 1×
    // this panel would go red routinely, and a panel that goes red routinely is
    // one people stop reading.
    const result = verdict([run({ last_at: ago(INTERVAL + 30) })], settings(), t, { now: NOW })
    expect(result.tone).toBe('ok')
  })

  it('reports a timer that was switched off as switched off, not as broken', () => {
    // Somebody did this on purpose. Reporting their decision as a fault is how
    // a status panel loses the reader it needs when something is actually wrong.
    const result = verdict(
      [run({ last_at: ago(86_400) })],
      settings({ reconcile_enabled: false }),
      t,
      { now: NOW },
    )
    expect(result.tone).toBe('warn')
    expect(result.headline).toBe('Reconciliation is switched off')
  })

  it('ranks an unreachable node above drift, because it is often the cause', () => {
    const result = verdict(
      [run({ unreachable: 1, with_differences: 1 })],
      settings(),
      t,
      { now: NOW },
    )
    expect(result.headline).toBe('1 node(s) unreachable')
  })

  it('ranks a correction that could not be pushed above one that is being made', () => {
    // Drift being corrected is the safety net working. Drift that will not go
    // is the safety net failing, and only one of those needs somebody.
    const result = verdict(
      [run({ out_of_sync: 1, with_differences: 1 })],
      settings(),
      t,
      { now: NOW },
    )
    expect(result.tone).toBe('bad')
    expect(result.headline).toBe('1 correction(s) could not be pushed')
  })

  it('treats drift that is being corrected as a warning, not a failure', () => {
    const result = verdict([run({ with_differences: 1, corrected: 1 })], settings(), t, { now: NOW })
    expect(result.tone).toBe('warn')
    expect(result.headline).toBe('Correcting drift on 1 node(s)')
  })

  it('says an empty hub has nothing to replicate, ahead of everything else', () => {
    // The state this whole gate exists for: a hub nobody finished setting up has
    // an empty desired state, and full-state pushes read empty as "clear them".
    // It skips its passes on purpose, so the card must not report a stopped
    // timer — and must not report a healthy fleet either.
    const result = verdict([run({ last_at: ago(86_400) })], settings(), t, {
      now: NOW,
      replicating: false,
    })
    expect(result.tone).toBe('warn')
    expect(result.headline).toBe('Nothing to replicate yet')
  })

  it('assumes the hub is replicating while the dashboard is still loading', () => {
    // A card mid-load must not accuse the reconciler of skipping.
    const result = verdict([run()], settings(), t, { now: NOW, replicating: null })
    expect(result.headline).toBe('Nothing to correct')
  })

  it('says nothing has run yet rather than inventing a state', () => {
    expect(verdict([], settings(), t, { now: NOW }).headline).toBe('No pass recorded yet')
  })

  it('waits rather than guessing while the data is still loading', () => {
    expect(verdict(null, settings(), t, { now: NOW }).headline).toBe('Checking…')
    expect(verdict([run()], null, t, { now: NOW }).headline).toBe('Checking…')
  })
})

describe('humanGap', () => {
  // Coarse on purpose: the reader is deciding whether something stopped, and
  // "3 h" answers that where "3 h 14 min 8 s" only makes them read further.
  it.each([
    [30_000, '1 min'],
    [60_000, '1 min'],
    [59 * 60_000, '59 min'],
    [90 * 60_000, '1 h'],
    [47 * 3_600_000, '47 h'],
    [72 * 3_600_000, '3 d'],
  ])('renders %i ms as %s', (ms, expected) => {
    expect(humanGap(ms)).toBe(expected)
  })

  it('never reports a real gap as no gap at all', () => {
    // Rounding 40 seconds down to "0 min" would read as "just now", which is
    // the opposite of what a gap is being measured for.
    expect(humanGap(40_000)).toBe('1 min')
  })
})
