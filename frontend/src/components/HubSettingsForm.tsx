import { useState } from 'react'
import { api } from '../api/client'
import type { HubSettings } from '../api/types'
import { useT } from '../i18n'
import { Banner, Card, Empty } from './ui'
import { errorMessage, useResource } from '../hooks/useApi'

interface NumberFieldSpec {
  key: keyof Omit<HubSettings, 'limits'>
  label: string
  hint: string
  unit: string
}

const TIMERS: NumberFieldSpec[] = [
  {
    key: 'reconcile_interval',
    label: 'Reconciliation interval',
    hint: 'How often every instance is compared against the hub and corrected. Your own changes do not wait for this — they are pushed the moment you make them.',
    unit: 'seconds',
  },
  {
    key: 'retry_interval',
    label: 'Retry interval',
    hint: 'How often a queued push to an unreachable instance is retried.',
    unit: 'seconds',
  },
  {
    key: 'querylog_poll_interval',
    label: 'Query log poll interval',
    hint: 'How often each instance is polled for new log entries. Lower means more requests.',
    unit: 'seconds',
  },
  {
    key: 'querylog_buffer_size',
    label: 'Query log buffer',
    hint: 'How many aggregated entries are kept in memory. Nothing is written to disk.',
    unit: 'entries',
  },
  {
    key: 'http_timeout',
    label: 'Instance request timeout',
    hint: 'How long to wait on an instance before treating it as unreachable.',
    unit: 'seconds',
  },
]

/** Timers and limits, editable at runtime — no container restart, no compose edit. */
export function HubSettingsForm() {
  const t = useT()
  const settings = useResource<HubSettings>(() => api.hubSettings())
  const [draft, setDraft] = useState<HubSettings | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  const value = draft ?? settings.data
  if (!value) {
    return (
      <Card title={t('Sync & timers')}>
        <Empty>{settings.error || t('Loading…')}</Empty>
      </Card>
    )
  }

  const update = (patch: Partial<HubSettings>) => setDraft({ ...value, ...patch })

  const save = (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    setMessage('')
    void (async () => {
      try {
        // `limits` is server-owned and not part of the write payload.
        const saved = await api.saveHubSettings({
          reconcile_enabled: value.reconcile_enabled,
          reconcile_interval: value.reconcile_interval,
          retry_interval: value.retry_interval,
          querylog_enabled: value.querylog_enabled,
          querylog_poll_interval: value.querylog_poll_interval,
          querylog_buffer_size: value.querylog_buffer_size,
          http_timeout: value.http_timeout,
          external_api_enabled: value.external_api_enabled,
        })
        setDraft(null)
        settings.setData(saved)
        setMessage(t('Saved — the workers pick this up on their next cycle.'))
      } catch (caught) {
        setError(errorMessage(caught))
      } finally {
        setBusy(false)
      }
    })()
  }

  return (
    <Card
      title={t('Sync & timers')}
      hint={t('Applied on the next worker cycle. Out-of-range values are clamped rather than rejected.')}
    >
      {error ? <Banner kind="error">{error}</Banner> : null}
      {message ? <Banner kind="ok">{message}</Banner> : null}

      <form onSubmit={save}>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={value.reconcile_enabled}
            onChange={(event) => update({ reconcile_enabled: event.target.checked })}
          />
          {t('Run reconciliation on a timer')}
        </label>
        {!value.reconcile_enabled ? (
          <Banner kind="warn">
            {t(
              'Drift will not be detected or corrected automatically while this is off. Changes you make here are still pushed immediately; only the safety net is paused.',
            )}
          </Banner>
        ) : null}

        <label className="checkbox">
          <input
            type="checkbox"
            checked={value.querylog_enabled}
            onChange={(event) => update({ querylog_enabled: event.target.checked })}
          />
          {t('Poll the instances for query log entries')}
        </label>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={value.external_api_enabled}
            onChange={(event) => update({ external_api_enabled: event.target.checked })}
          />
          {t('Serve the AdGuard-compatible API at')} <code>/control</code>
        </label>
        <p className="hint" style={{ margin: '0 0 8px 26px' }}>
          {t(
            'Lets apps and integrations built for AdGuard Home point at the hub instead of at one node — they sign in with this admin account, and what they change is pushed everywhere.',
          )}
        </p>

        {/* A grid, not a `.row`: that flex row bottom-aligns its children, so a
            field whose help text wrapped to two lines pushed its own label and
            input a line higher than its neighbours' — five timers sitting at two
            different heights. `aligned` then keeps the inputs on one line too,
            where a label wraps and the plain grid would step the input down. */}
        <div className="fields-grid aligned" style={{ marginTop: 10 }}>
          {TIMERS.map((field) => {
            const [min, max] = value.limits[field.key] ?? [1, 100000]
            return (
              <div className="field" key={field.key}>
                <label htmlFor={field.key}>
                  {t(field.label)} ({t(field.unit)})
                </label>
                <input
                  id={field.key}
                  type="number"
                  min={min}
                  max={max}
                  value={value[field.key] as number}
                  onChange={(event) =>
                    update({ [field.key]: Number(event.target.value) } as Partial<HubSettings>)
                  }
                />
                <div className="hint" style={{ margin: '4px 0 0' }}>
                  {t(field.hint)} {t('Allowed {min}–{max}.', { min, max })}
                </div>
              </div>
            )
          })}
        </div>

        <div className="actions" style={{ marginTop: 12 }}>
          <button className="primary" type="submit" disabled={busy}>
            {t('Save settings')}
          </button>
          {draft ? (
            <button type="button" onClick={() => setDraft(null)} disabled={busy}>
              {t('Discard changes')}
            </button>
          ) : null}
        </div>
      </form>
    </Card>
  )
}
