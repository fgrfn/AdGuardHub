/**
 * Blocklists and allowlists in one table read as one thing, and they are the
 * opposite of each other. These check the split, and the count that answers
 * "why is my exception list doing nothing" without a click.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { FilterList } from '../api/types'
import { I18nProvider } from '../i18n/provider'
import Blocklists from './Blocklists'

const LISTS: FilterList[] = [
  { id: 1, name: 'AdGuard DNS filter', url: 'https://e.test/1.txt', kind: 'blocklist', enabled: true },
  { id: 2, name: '1Hosts (Lite)', url: 'https://e.test/24.txt', kind: 'blocklist', enabled: true },
  { id: 3, name: "HaGeZi's Allowlist", url: 'https://e.test/45.txt', kind: 'allowlist', enabled: true },
] as FilterList[]

const { filterLists, filterSizes, refreshFilterLists } = vi.hoisted(() => ({
  filterLists: vi.fn(),
  filterSizes: vi.fn(),
  refreshFilterLists: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: { filterLists, filterSizes, refreshFilterLists },
}))
// The sub-tab bar needs a router; the page's own tabs are plain state.
vi.mock('../components/SubTabs', () => ({ SubTabs: () => null }))

function show() {
  filterLists.mockResolvedValue(LISTS)
  filterSizes.mockResolvedValue({
    lists: [],
    total_rules: 0,
    instances_reporting: 1,
    instances_total: 1,
  })
  refreshFilterLists.mockResolvedValue({ instances: [], updated: 0 })
  return render(
    <I18nProvider>
      <Blocklists />
    </I18nProvider>,
  )
}

const names = (container: HTMLElement) =>
  [...container.querySelectorAll('tbody tr td:first-child')].map((cell) => cell.textContent)

describe('Blocklists', () => {
  it('shows every kind until a tab narrows it', async () => {
    const { container } = show()
    await waitFor(() => expect(names(container)).toHaveLength(3))
  })

  it('a tab keeps only its own kind', async () => {
    const { container } = show()
    await waitFor(() => expect(names(container)).toHaveLength(3))

    fireEvent.click(screen.getByRole('button', { name: /Allowlists/ }))
    expect(names(container)).toEqual(["HaGeZi's Allowlist"])

    fireEvent.click(screen.getByRole('button', { name: /Blocklists/ }))
    expect(names(container)).toEqual(['AdGuard DNS filter', '1Hosts (Lite)'])
  })

  it('every tab carries its count, so an empty allowlist is visible without clicking', async () => {
    show()
    const allowlists = await screen.findByRole('button', { name: /Allowlists/ })
    expect(allowlists.textContent).toContain('(1)')
    expect(screen.getByRole('button', { name: /Blocklists/ }).textContent).toContain('(2)')
  })

  it('an empty tab says the filter did it, not that nothing is subscribed', async () => {
    filterLists.mockResolvedValue(LISTS.filter((item) => item.kind === 'blocklist'))
    filterSizes.mockResolvedValue({
      lists: [],
      total_rules: 0,
      instances_reporting: 1,
      instances_total: 1,
    })
    const { container } = render(
      <I18nProvider>
        <Blocklists />
      </I18nProvider>,
    )
    await waitFor(() => expect(names(container)).toHaveLength(2))

    fireEvent.click(screen.getByRole('button', { name: /Allowlists/ }))

    expect(container.textContent).toContain('No subscriptions of this kind.')
    expect(container.textContent).not.toContain('No subscriptions yet.')
  })
})

describe('when a list last arrived', () => {
  function withSizes(lists: unknown[]) {
    filterLists.mockResolvedValue(LISTS.slice(0, 1))
    filterSizes.mockResolvedValue({
      lists,
      total_rules: 0,
      instances_reporting: 1,
      instances_total: 1,
    })
    refreshFilterLists.mockResolvedValue({ instances: [], updated: 0 })
    return render(
      <I18nProvider>
        <Blocklists />
      </I18nProvider>,
    )
  }

  const SIZE = {
    url: 'https://e.test/1.txt',
    kind: 'blocklist',
    rules_count: 10,
    agreed: true,
    per_instance: [
      { instance_id: 1, instance_name: 'node-a', rules_count: 10, last_updated: null },
    ],
    last_updated: null,
  }

  it('says never when no node has ever downloaded it', async () => {
    // The fault worth catching: AdGuard keeps a subscription it cannot fetch, so
    // a rotted URL stays on the node and simply stops changing.
    const { container } = withSizes([SIZE])
    await waitFor(() => expect(container.textContent).toContain('AdGuard DNS filter'))

    expect(container.textContent).toContain('never')
  })

  it('shows the age when a node has one', async () => {
    const when = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString()
    const { container } = withSizes([
      {
        ...SIZE,
        last_updated: when,
        per_instance: [
          { instance_id: 1, instance_name: 'node-a', rules_count: 10, last_updated: when },
        ],
      },
    ])
    await waitFor(() => expect(container.textContent).toContain('AdGuard DNS filter'))

    expect(container.textContent).toContain('3 h ago')
  })
})

describe('checking for updates', () => {
  it('asks the nodes and says how many lists changed', async () => {
    show()
    refreshFilterLists.mockResolvedValue({
      instances: [{ instance_id: 1, instance_name: 'node-a', updated: 2, error: '' }],
      updated: 2,
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Check for updates' }))

    await waitFor(() => expect(refreshFilterLists).toHaveBeenCalled())
    await screen.findByText(/2 list\(s\) updated\./)
  })

  it('names a node that could not be asked rather than reporting a clean run', async () => {
    show()
    refreshFilterLists.mockResolvedValue({
      instances: [
        { instance_id: 1, instance_name: 'node-a', updated: 1, error: '' },
        { instance_id: 2, instance_name: 'node-b', updated: 0, error: 'unreachable' },
      ],
      updated: 1,
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Check for updates' }))

    await screen.findByText(/node-b could not be asked\./)
  })
})
