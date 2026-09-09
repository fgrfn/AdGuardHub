/**
 * A repeated finding is written once and counted, not appended.
 *
 * That keeps the log readable and costs the two facts an operator actually asks
 * about a standing fault: how often, and is it still happening. The row's own
 * timestamp is the *first* sighting, so without these a fault repeating every
 * five minutes and one that stopped hours ago render identically — which is
 * precisely the case that sat unnoticed for four hours.
 *
 * The property that has to hold in both directions: the facts appear when there
 * is something to say, and nothing appears when there is not.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { DriftEvent } from '../api/types'
import { I18nProvider } from '../i18n/provider'
import { DriftFacts } from './DriftFacts'

function event(overrides: Partial<DriftEvent> = {}): DriftEvent {
  return {
    id: 1,
    instance_id: 1,
    instance_name: 'node-a',
    payload_kind: 'rules',
    summary: 'the node did not keep this correction — 1 rule(s) missing',
    details: '{}',
    corrected: false,
    occurrences: 1,
    last_seen_at: null,
    took_ms: 0,
    created_at: '2026-09-08T19:44:00Z',
    ...overrides,
  }
}

function show(value: DriftEvent) {
  return render(
    <I18nProvider>
      <DriftFacts event={value} />
    </I18nProvider>,
  )
}

describe('DriftFacts', () => {
  it('says nothing about an entry seen once with nothing measured', () => {
    // The ordinary row. "1×" under every entry is noise, and noise under every
    // entry is what teaches the eye to skip the rows that matter.
    const { container } = show(event())
    expect(container.textContent).toBe('')
  })

  it('counts a finding that keeps coming back', () => {
    show(event({ occurrences: 340, last_seen_at: '2026-09-08T23:12:00Z' }))
    expect(screen.getByText(/found 340×/)).toBeTruthy()
  })

  it('says when it was last seen, which the row itself cannot', () => {
    // The When column holds the first sighting on purpose — it answers "since
    // when". This is the other half: "and it happened again just now".
    show(event({ occurrences: 12, last_seen_at: '2026-09-08T23:12:00Z' }))
    expect(screen.getByText(/last /)).toBeTruthy()
  })

  it('does not claim a last sighting the backend did not send', () => {
    // occurrences without last_seen_at is what an upgraded database looks like
    // on its first pass: the count moved, the timestamp has not been set yet.
    show(event({ occurrences: 2, last_seen_at: null }))
    expect(screen.getByText(/found 2×/)).toBeTruthy()
    expect(screen.queryByText(/last /)).toBeNull()
  })

  it('shows how long the attempt took', () => {
    // The reported fault, as it would have read: a push that died on the
    // ten-second default, with the number naming the setting that caused it.
    show(event({ took_ms: 10_000 }))
    expect(screen.getByText(/10\.0 s/)).toBeTruthy()
  })

  it('treats an unmeasured attempt as nothing to say, not as zero', () => {
    // A dry run attempts nothing, so it has no duration to report — and "0 ms"
    // would read as a push that was instant rather than one that never happened.
    show(event({ occurrences: 3, took_ms: 0 }))
    expect(screen.queryByText(/ms/)).toBeNull()
  })
})
