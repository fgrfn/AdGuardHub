/**
 * Reading the drift log's archive.
 *
 * The dashboard's table is built to stay readable — capped, folded, trimmed, and
 * emptied by a button. The archive is the other half: what was written down as
 * it happened. These pin the parts that would otherwise be easy to get wrong —
 * the paging cursor, and saying so plainly when there is nothing to read,
 * because "empty" and "switched off" are different answers and a page that
 * conflates them sends somebody looking for a fault that is not there.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DriftArchiveEntry } from '../../api/types'
import { I18nProvider } from '../../i18n/provider'
import { DriftArchive } from './DriftArchive'

const { archiveMock } = vi.hoisted(() => ({ archiveMock: vi.fn() }))

vi.mock('../../api/client', () => ({ api: { driftArchive: archiveMock } }))

function entry(overrides: Partial<DriftArchiveEntry> = {}): DriftArchiveEntry {
  return {
    offset: 0,
    at: '2026-09-08T19:44:00Z',
    instance: 'node-b',
    payload_kind: 'filters',
    summary: '20 subscription(s) missing, 0 unexpected, 0 with a different enabled state',
    corrected: false,
    took_ms: 10_000,
    details: { missing: ['blocklist:https://example.test/list.txt'] },
    ...overrides,
  }
}

function show() {
  return render(
    <I18nProvider>
      <DriftArchive />
    </I18nProvider>,
  )
}

beforeEach(() => {
  archiveMock.mockReset()
})

describe('DriftArchive', () => {
  it('shows what was archived', async () => {
    archiveMock.mockResolvedValue({ entries: [entry()], more: false, enabled: true })
    show()
    expect(await screen.findByText(/20 subscription\(s\) missing/)).toBeTruthy()
  })

  it('shows how long the attempt took, which is what names a timeout', async () => {
    // Ten seconds on the nose is the reported fault, and the number is the part
    // that points at the setting rather than at the node.
    archiveMock.mockResolvedValue({ entries: [entry()], more: false, enabled: true })
    show()
    expect(await screen.findByText('10.0 s')).toBeTruthy()
  })

  it('asks for older entries by counting back from the newest', async () => {
    // Counting forward from the oldest would renumber everything on a rotation,
    // so a cursor taken before one would silently skip or repeat a page.
    archiveMock.mockResolvedValue({
      entries: [entry({ at: '2026-09-08T19:44:00Z' }), entry({ at: '2026-09-08T19:39:00Z', offset: 1 })],
      more: true,
      enabled: true,
    })
    show()

    fireEvent.click(await screen.findByText('Load older entries'))

    await waitFor(() => expect(archiveMock).toHaveBeenCalledTimes(2))
    expect(archiveMock.mock.calls[0][0]).toBe(0)
    expect(archiveMock.mock.calls[1][0]).toBe(2)
  })

  it('does not repeat a row when the window shifts under the cursor', async () => {
    // The cursor counts back from the newest entry, so a finding archived
    // between the two clicks pushes everything down one and the older page
    // hands back an entry the reader already has. A duplicated row in
    // something people read as evidence is worth avoiding.
    const first = entry({ at: '2026-09-08T19:44:00Z' })
    const second = entry({ at: '2026-09-08T19:39:00Z', offset: 1 })
    archiveMock.mockResolvedValueOnce({ entries: [first, second], more: true, enabled: true })
    archiveMock.mockResolvedValueOnce({
      // Shifted: the page starts one entry later than the reader expects.
      entries: [second, entry({ at: '2026-09-08T19:34:00Z', offset: 3 })],
      more: false,
      enabled: true,
    })
    show()

    fireEvent.click(await screen.findByText('Load older entries'))

    await waitFor(() => expect(screen.getAllByText(/20 subscription\(s\) missing/)).toHaveLength(3))
  })

  it('offers nothing to load when there is nothing behind the page', async () => {
    archiveMock.mockResolvedValue({ entries: [entry()], more: false, enabled: true })
    show()
    await screen.findByText(/20 subscription\(s\) missing/)
    expect(screen.queryByText('Load older entries')).toBeNull()
  })

  it('says an empty archive is empty', async () => {
    archiveMock.mockResolvedValue({ entries: [], more: false, enabled: true })
    show()
    expect(await screen.findByText(/the hub has found no drift/)).toBeTruthy()
  })

  it('says a switched-off archive is switched off, which is not the same thing', async () => {
    // Told apart on purpose: "no drift found" is good news, "nothing is being
    // written" is a setting — and reading the first as the second is how
    // somebody concludes their fleet is healthy while nothing is watching it.
    archiveMock.mockResolvedValue({ entries: [], more: false, enabled: false })
    show()
    expect(await screen.findByText(/switched off/)).toBeTruthy()
  })
})
