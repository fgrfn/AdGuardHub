/**
 * Dates and numbers, and the locale they follow.
 *
 * This file exists because of a real bug: the formatters called
 * `toLocaleString()` with no argument, which follows the *browser* rather than
 * the interface. A German operator on an en-US browser read `9/1/2026` under a
 * German heading — not a different format, a different date.
 *
 * The property to pin is therefore not "German uses dots". It is that the
 * language the user picked wins over the one their browser happens to be in,
 * *and* that English still defers to the browser, because `en` is written in
 * both date orders and an American should keep seeing American dates.
 */

import { afterEach, describe, expect, it } from 'vitest'
import {
  formatClock,
  formatCount,
  formatDuration,
  formatTime,
  parseApiDate,
  setFormatLocale,
} from './format'

// A fixed instant, expressed so the assertions below cannot drift with the
// runner's timezone: local midday on a day whose parts differ in every locale.
const INSTANT = new Date(2026, 8, 1, 14, 38, 49).toISOString()

afterEach(() => {
  // Module-level state: one test must not decide the next one's locale.
  setFormatLocale('en')
})

describe('setFormatLocale', () => {
  it('formats dates for German once German is chosen', () => {
    setFormatLocale('de')
    const shown = formatTime(INSTANT)
    expect(shown).toContain('1.9.2026')
    expect(shown).not.toMatch(/\d{1,2}\/\d{1,2}\/\d{4}/)
  })

  it('groups thousands the German way too', () => {
    setFormatLocale('de')
    expect(formatCount(4085)).toBe('4.085')
  })

  it('leaves English on the browser locale rather than pinning a region', () => {
    // The test runner's locale is en-US, so this is the case that regressed
    // when an early draft pinned English to en-GB.
    setFormatLocale('en')
    expect(formatTime(INSTANT)).toMatch(/\d{1,2}\/\d{1,2}\/\d{4}/)
    expect(formatCount(4085)).toBe('4,085')
  })

  it('switches back when the language does', () => {
    setFormatLocale('de')
    setFormatLocale('en')
    expect(formatCount(4085)).toBe('4,085')
  })

  it('treats an unknown language as English rather than throwing', () => {
    setFormatLocale('fr')
    expect(() => formatCount(1000)).not.toThrow()
  })
})

describe('the empty and broken cases', () => {
  it.each([null, undefined, ''])('renders %p as a dash', (value) => {
    expect(formatTime(value)).toBe('—')
    expect(formatClock(value)).toBe('—')
  })

  it('hands back text it cannot parse instead of showing "Invalid Date"', () => {
    expect(formatTime('not a timestamp')).toBe('not a timestamp')
    expect(formatClock('not a timestamp')).toBe('not a timestamp')
  })
})

describe('formatClock', () => {
  it('drops the date, because every row in the log is from the last few minutes', () => {
    const shown = formatClock(INSTANT)
    expect(shown).not.toContain('2026')
    expect(shown).toMatch(/\d{1,2}:\d{2}/)
  })
})

describe('formatDuration', () => {
  // The three spans this actually has to cover: a push to a healthy node on the
  // LAN, a node fetching a list, and a large blocklist. One unit across all of
  // them reads as either "62000 ms" or "0.0 min".
  it.each([
    [0, '0 ms'],
    [84, '84 ms'],
    [999, '999 ms'],
    [1000, '1.0 s'],
    // The reported fault: a push that died on the ten-second default.
    [10_000, '10.0 s'],
    [59_940, '59.9 s'],
    [60_000, '1 min 0 s'],
    // A threat feed on a small host, which is what the timeout was raised for.
    [125_400, '2 min 5 s'],
  ])('renders %i ms as %s', (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected)
  })

  it('never rounds a real measurement down to nothing', () => {
    // "0 ms" against a push that took half a millisecond would read as "not
    // measured", which is what a zero means everywhere else in the drift row.
    expect(formatDuration(0.4)).toBe('0 ms')
    expect(formatDuration(0.6)).toBe('1 ms')
  })
})

describe('parseApiDate', () => {
  // The hub speaks UTC. A timestamp without an offset is not ambiguous in
  // JavaScript — it is *defined* as local time — so every time in the interface
  // was shown two hours early in Berlin, and the Reconciliation card called a
  // healthy reconciler stopped.
  it('reads a timestamp without an offset as UTC, not as local time', () => {
    expect(parseApiDate('2026-09-12T18:23:36').toISOString()).toBe('2026-09-12T18:23:36.000Z')
  })

  it('leaves a timestamp that already carries its zone alone', () => {
    // So the backend fix and this one cannot disagree.
    expect(parseApiDate('2026-09-12T18:23:36+00:00').toISOString()).toBe(
      '2026-09-12T18:23:36.000Z',
    )
    expect(parseApiDate('2026-09-12T20:23:36+02:00').toISOString()).toBe(
      '2026-09-12T18:23:36.000Z',
    )
    expect(parseApiDate('2026-09-12T18:23:36Z').toISOString()).toBe('2026-09-12T18:23:36.000Z')
  })

  it('keeps sub-second precision, which the hub sends', () => {
    expect(parseApiDate('2026-09-12T17:54:37.315189').toISOString()).toBe(
      '2026-09-12T17:54:37.315Z',
    )
  })
})
