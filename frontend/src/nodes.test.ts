/**
 * The one distinction this feature exists to keep: a node the hub could not ask
 * is not a node that is up to date.
 */

import { describe, expect, it } from 'vitest'
import type { Instance } from './api/types'
import { nodeUpdateState, nodesBehind } from './nodes'

function node(overrides: Partial<Instance>): Instance {
  return {
    id: 1,
    name: 'a',
    base_url: 'http://a.local',
    adapter: 'adguard',
    username: 'admin',
    has_password: true,
    verify_tls: true,
    enabled: true,
    maintenance: false,
    status: 'online',
    version: 'v0.107.60',
    update_version: '',
    update_url: '',
    update_error: '',
    last_error: '',
    last_seen_at: null,
    last_synced_at: null,
    out_of_sync_since: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('nodeUpdateState', () => {
  it('reports a node with a newer build as behind', () => {
    expect(nodeUpdateState(node({ update_version: 'v0.107.64' }))).toBe('behind')
  })

  it('reports a node that answered with nothing as current', () => {
    expect(nodeUpdateState(node({}))).toBe('current')
  })

  it('does not call a node current when nobody could ask it', () => {
    // A node with its own update check switched off answers this way. Calling
    // that "up to date" is the one wrong answer available here.
    const state = nodeUpdateState(
      node({ update_error: 'this node has its own update check switched off' }),
    )
    expect(state).toBe('unknown')
    expect(state).not.toBe('current')
  })

  it('prefers a real offer over a stale error beside it', () => {
    const state = nodeUpdateState(node({ update_version: 'v0.107.64', update_error: 'timeout' }))
    expect(state).toBe('behind')
  })
})

describe('nodesBehind', () => {
  it('picks only the nodes with something to install', () => {
    const behind = nodesBehind([
      node({ id: 1, name: 'a', update_version: 'v0.107.64' }),
      node({ id: 2, name: 'b' }),
      node({ id: 3, name: 'c', update_error: 'unreachable' }),
    ])
    expect(behind.map((item) => item.name)).toEqual(['a'])
  })

  it('copes with the list not having loaded yet', () => {
    expect(nodesBehind(null)).toEqual([])
    expect(nodesBehind(undefined)).toEqual([])
  })
})
