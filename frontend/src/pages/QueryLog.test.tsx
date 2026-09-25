/**
 * The filter bar's job is to turn a question into a request. These check the two
 * that the old bar could not express at all — "which of my allow rules are
 * firing" and "what did this one list block" — arrive at the backend as the
 * parameters that mean them, rather than as a view filtered in the browser over
 * the two hundred rows it happens to be holding.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { QueryLogEntry } from '../api/types'
import { I18nProvider } from '../i18n/provider'
import QueryLog from './QueryLog'

const ENTRIES: QueryLogEntry[] = [
  {
    instance: 'node-a',
    time: '2026-01-01T10:00:00Z',
    question: 'ads.example.com',
    question_type: 'A',
    client: '10.0.0.5',
    answer_status: 'FilteredBlackList',
    blocked: true,
    rule: '||ads.example.com^',
    filter_list: 'HaGeZi Threat Feeds',
    elapsed_ms: 2,
    upstream: '',
  },
]

const { queryLog, queryLogLists, instances } = vi.hoisted(() => ({
  queryLog: vi.fn(),
  queryLogLists: vi.fn(),
  instances: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: { queryLog, queryLogLists, instances } }))
// The live badge needs a stream; nothing here is about what arrives over it.
vi.mock('../hooks/useEventStream', () => ({ useEventStream: () => false }))

function show() {
  queryLog.mockResolvedValue(ENTRIES)
  queryLogLists.mockResolvedValue({ lists: ['HaGeZi Threat Feeds', 'Your own rules'] })
  instances.mockResolvedValue([])
  return render(
    <I18nProvider>
      <QueryLog />
    </I18nProvider>,
  )
}

const lastRequest = () => queryLog.mock.calls.at(-1)?.[0]

describe('the query log filter bar', () => {
  it('asks for the queries an allow rule let through', async () => {
    show()
    await waitFor(() => expect(queryLog).toHaveBeenCalled())

    fireEvent.change(screen.getByLabelText('Filter by response'), {
      target: { value: 'allowlisted' },
    })

    await waitFor(() => expect(lastRequest()?.status).toBe('allowlisted'))
  })

  it('asks for one list at a time', async () => {
    show()
    await waitFor(() => expect(queryLogLists).toHaveBeenCalled())

    const select = await screen.findByLabelText('Filter by list')
    fireEvent.change(select, { target: { value: 'HaGeZi Threat Feeds' } })

    await waitFor(() => expect(lastRequest()?.filter_list).toBe('HaGeZi Threat Feeds'))
  })

  it('offers no list filter until an entry has named one', async () => {
    queryLog.mockResolvedValue([])
    queryLogLists.mockResolvedValue({ lists: [] })
    instances.mockResolvedValue([])
    render(
      <I18nProvider>
        <QueryLog />
      </I18nProvider>,
    )
    await waitFor(() => expect(queryLogLists).toHaveBeenCalled())

    expect(screen.queryByLabelText('Filter by list')).toBeNull()
  })

  it('leaves the filtering to the backend rather than the loaded page', async () => {
    show()
    await waitFor(() => expect(queryLog).toHaveBeenCalled())
    const before = queryLog.mock.calls.length

    fireEvent.change(screen.getByLabelText('Filter by response'), {
      target: { value: 'blocked' },
    })

    // The buffer holds far more than the page does, so narrowing has to be a new
    // request: filtering the rows already fetched would silently search only the
    // most recent few hundred.
    await waitFor(() => expect(queryLog.mock.calls.length).toBeGreaterThan(before))
  })
})
