import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { DashboardStats, Instance } from '../api/types'
import { formatTime } from '../format'
import { useResource } from '../hooks/useApi'
import { useT } from '../i18n'
import { IconChevron } from './icons'

type Tone = 'ok' | 'warn' | 'bad'

function verdict(
  instances: Instance[] | null,
  stats: DashboardStats | null,
  t: (text: string, vars?: Record<string, string | number>) => string,
): { tone: Tone; text: string } {
  if (!instances || !stats) return { tone: 'ok', text: t('Checking…') }
  if (instances.length === 0) return { tone: 'warn', text: t('No nodes yet') }

  const unreachable = instances.filter((item) => item.status === 'unreachable').length
  if (unreachable) {
    return {
      tone: 'bad',
      text:
        unreachable > 1
          ? t('{count} nodes unreachable', { count: unreachable })
          : t('1 node unreachable'),
    }
  }
  // Before the queue, after unreachability: a node that answers every request
  // and holds none of them is not "in sync", and saying so was the gap that let
  // one sit wrong for four hours under a green summary.
  const drifting = instances.filter((item) => item.out_of_sync_since).length
  if (drifting) {
    return {
      tone: 'bad',
      text:
        drifting > 1
          ? t('{count} nodes out of sync', { count: drifting })
          : t('1 node out of sync'),
    }
  }
  const queued = stats.pending_jobs + stats.failed_jobs
  if (queued) {
    return {
      tone: 'warn',
      text: queued > 1 ? t('{count} pushes queued', { count: queued }) : t('1 push queued'),
    }
  }
  return { tone: 'ok', text: t('All nodes in sync') }
}

/**
 * The one element that is never off screen.
 *
 * The whole point of the hub is that every node carries the same configuration.
 * When that stops being true it has to find the operator, not wait to be looked up.
 */
export function SyncStatus({ connected, tick }: { connected: boolean; tick: number }) {
  const t = useT()
  const instances = useResource<Instance[]>(() => api.instances())
  const stats = useResource(() => api.dashboard())
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)

  const reloadInstances = instances.reload
  const reloadStats = stats.reload
  useEffect(() => {
    if (tick === 0) return
    void reloadInstances()
    void reloadStats()
  }, [tick, reloadInstances, reloadStats])

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  const { tone, text } = verdict(instances.data, stats.data, t)

  return (
    <div className="sync" ref={wrap}>
      <button
        className={`sync-pill ${tone}`}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        title={connected ? t('Live updates connected') : t('Reconnecting to the hub…')}
      >
        <span className="sync-dot" />
        {text}
        <IconChevron up={open} />
      </button>

      {open ? (
        <div className="sync-panel">
          {instances.data?.length ? (
            instances.data.map((instance) => (
              <div className="sync-row" key={instance.id}>
                <span
                  className={`node-dot${
                    instance.status === 'unreachable'
                      ? ' down'
                      : instance.status === 'disabled'
                        ? ' off'
                        : instance.out_of_sync_since
                          ? ' drifting'
                          : ''
                  }`}
                  style={{ width: 7, height: 7 }}
                />
                <span style={{ fontWeight: 600 }}>{instance.name}</span>
                <span style={{ marginLeft: 'auto', color: 'var(--dim)' }}>
                  {instance.status !== 'online'
                    ? instance.status
                    : instance.out_of_sync_since
                      ? t('out of sync since {when}', {
                          when: formatTime(instance.out_of_sync_since),
                        })
                      : formatTime(instance.last_synced_at)}
                </span>
              </div>
            ))
          ) : (
            <div className="sync-row" style={{ color: 'var(--dim)' }}>
              {t('No instances configured yet.')}
            </div>
          )}

          <div
            style={{
              marginTop: 10,
              paddingTop: 10,
              borderTop: '1px solid var(--line)',
              fontSize: 12.5,
              color: 'var(--dim)',
              display: 'flex',
              justifyContent: 'space-between',
              gap: 10,
            }}
          >
            <span>
              <span className={`live-dot${connected ? ' on' : ''}`} />
              {connected ? t('live') : t('reconnecting…')}
            </span>
            <Link to="/instances" onClick={() => setOpen(false)}>
              {t('Manage nodes')}
            </Link>
          </div>
        </div>
      ) : null}
    </div>
  )
}
