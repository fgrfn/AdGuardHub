/**
 * The summary line, which used to say "All nodes in sync" while one was not.
 *
 * A node answering every request is *online*, and that was the whole of what
 * this component knew. One spent four hours refusing every correction under a
 * green heading. These pin the wording to the state.
 */

import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { DashboardStats, Instance } from '../api/types'
import { I18nProvider } from '../i18n/provider'
import { SyncStatus } from './SyncStatus'

const { instances, dashboard } = vi.hoisted(() => ({
  instances: vi.fn(),
  dashboard: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: { instances, dashboard } }))

function node(overrides: Partial<Instance> = {}): Instance {
  return {
    id: 1,
    name: 'Primary',
    status: 'online',
    enabled: true,
    maintenance: false,
    last_synced_at: '2026-09-06T22:00:00Z',
    out_of_sync_since: null,
    ...overrides,
  } as Instance
}

const QUIET: Partial<DashboardStats> = { pending_jobs: 0, failed_jobs: 0 }

function show(nodes: Instance[], stats: Partial<DashboardStats> = QUIET) {
  instances.mockResolvedValue(nodes)
  dashboard.mockResolvedValue(stats as DashboardStats)
  return render(
    <MemoryRouter>
      <I18nProvider>
        <SyncStatus />
      </I18nProvider>
    </MemoryRouter>,
  )
}

describe('SyncStatus', () => {
  it('says all nodes are in sync only when they are', async () => {
    show([node()])
    expect(await screen.findByText('All nodes in sync')).toBeTruthy()
  })

  it('a node answering but not holding is not "in sync"', async () => {
    // The reported case: online, reachable, and four hours behind.
    show([node({ out_of_sync_since: '2026-09-06T19:44:00Z' })])

    expect(await screen.findByText('1 node out of sync')).toBeTruthy()
    expect(screen.queryByText('All nodes in sync')).toBeNull()
  })

  it('counts them when more than one has drifted', async () => {
    show([
      node({ out_of_sync_since: '2026-09-06T19:44:00Z' }),
      node({ id: 2, name: 'Secondary', out_of_sync_since: '2026-09-06T20:10:00Z' }),
    ])

    expect(await screen.findByText('2 nodes out of sync')).toBeTruthy()
  })

  it('an unreachable node still wins the headline', async () => {
    // It is the more urgent fact, and the drifting one is a consequence of it
    // often enough that leading with the drift would bury the cause.
    show([
      node({ status: 'unreachable' }),
      node({ id: 2, name: 'Secondary', out_of_sync_since: '2026-09-06T19:44:00Z' }),
    ])

    expect(await screen.findByText('1 node unreachable')).toBeTruthy()
  })

  it('drifting outranks a queued push', async () => {
    // A queue drains on its own; a refused correction does not.
    show([node({ out_of_sync_since: '2026-09-06T19:44:00Z' })], {
      pending_jobs: 1,
      failed_jobs: 0,
    })

    expect(await screen.findByText('1 node out of sync')).toBeTruthy()
  })

  it('marks the drifting node itself, not only the summary', async () => {
    const { container } = show([node({ out_of_sync_since: '2026-09-06T19:44:00Z' })])
    await waitFor(() => expect(screen.getByText('1 node out of sync')).toBeTruthy())

    // Open the panel to reach the per-node rows.
    screen.getByRole('button').click()

    await waitFor(() => expect(container.querySelector('.node-dot.drifting')).not.toBeNull())
  })
})
