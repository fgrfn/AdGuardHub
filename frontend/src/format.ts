/**
 * Dates and numbers, formatted for the language the interface is actually in.
 *
 * These used to call `toLocaleString()` with no locale, which follows the
 * *browser*, not the UI. So switching the hub to German on a browser set to
 * en-US left every timestamp in American order — `9/1/2026, 2:38:49 PM` under a
 * German heading, which reads as the wrong date rather than a different format.
 *
 * The language lives in React state, but these are plain functions called from
 * dozens of call sites and from inside SVG. Rather than turn each of them into a
 * hook, the provider pushes the active language here whenever it changes, and
 * the formatters read it. Anything rendered before the provider mounts falls
 * back to the browser's own locale, which is what it always did.
 */

let locale: string | undefined

/**
 * Called by the i18n provider. Not part of the public formatting surface.
 *
 * English stays on the browser's own locale rather than being pinned to one
 * English region: `en` is spoken in places that write the date in opposite
 * orders, and an American reading the English UI should keep seeing American
 * dates. German has one convention, so it can be pinned, and pinning it is the
 * whole point — the operator picked German.
 */
export function setFormatLocale(language: string): void {
  locale = language === 'de' ? 'de-DE' : undefined
}

/**
 * A timestamp from the hub's API, as an instant.
 *
 * The hub speaks UTC throughout and now says so — but `new Date(s)` on an ISO
 * string *without* an offset does not guess, it is defined to mean **local
 * time**. So while the backend dropped the offset (SQLite has no timestamp type,
 * and SQLAlchemy's `timezone=True` is a no-op there), every time in this
 * interface was shown two hours early in Berlin — and the *Reconciliation* card,
 * which measures the gap to the last pass, reported a perfectly healthy
 * reconciler as stopped, permanently.
 *
 * The backend is fixed at the source. This stays as the reading half of the same
 * rule, because it is a true statement about this API rather than a patch: a
 * timestamp from it with no offset is UTC. It is a no-op on a value that already
 * carries one, so the two cannot disagree — and one field missed in a schema
 * cannot quietly reintroduce two hours of error.
 */
export function parseApiDate(value: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
  return new Date(hasZone ? value : `${value}Z`)
}

/** Shown in the reader's own zone; the backend speaks UTC throughout. */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = parseApiDate(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(locale)
}

/**
 * Date only, for something that happened on a day rather than at a moment — a
 * release date, where the time it was tagged is noise.
 */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = parseApiDate(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(locale, { year: 'numeric', month: 'long', day: 'numeric' })
}

/** Thousands separators for counts in tiles and lists. */
export function formatCount(value: number): string {
  return value.toLocaleString(locale)
}

/**
 * How long something took, from milliseconds.
 *
 * Three ranges because the interesting spans are that far apart: a push to a
 * healthy node on the LAN is tens of milliseconds, a node fetching a list is
 * seconds, and a large blocklist is a minute or more. One unit across all of
 * that gives either "62000 ms" or "0.0 min".
 *
 * The units are the same word in both languages the interface speaks, so they
 * stay here rather than going through t() — and this is called from table cells
 * where a translated fragment would only add a lookup.
 */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return `${minutes} min ${seconds} s`
}

/**
 * Time of day only, for logs where every row is from the last few minutes and the
 * date would just wrap the column. The full stamp belongs in a title attribute.
 */
export function formatClock(value: string | null | undefined): string {
  if (!value) return '—'
  const date = parseApiDate(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString(locale)
}
