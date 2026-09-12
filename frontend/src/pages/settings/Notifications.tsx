/** Where the hub tells you something went wrong (spec §10). */

import { useState } from 'react'
import { api } from '../../api/client'
import type { Notifier, NotifierType } from '../../api/types'
import { Card, Empty } from '../../components/ui'
import { useResource } from '../../hooks/useApi'
import { RunnerBanners } from '../../components/RunnerBanners'
import { useRunner } from '../../hooks/useRunner'
import { useT } from '../../i18n'

// Keyed on the backend's KNOWN_EVENTS. A missing entry falls back to the raw
// event id below, which reads as a bug rather than a label — backend/tests/
// test_i18n_events.py fails the build instead.
const EVENT_LABELS: Record<string, string> = {
  'reconcile.fixed': 'Reconciliation applied a fix',
  'instance.unreachable': 'An instance went unreachable',
  'instance.recovered': 'An instance became reachable again',
  'push.failed': 'A push failed and was queued',
  'reconcile.stalled': 'Reconciliation stopped running',
  'reconcile.resumed': 'Reconciliation started running again',
}

const URL_HINTS: Record<NotifierType, string> = {
  homeassistant: 'http://homeassistant.local:8123/api/webhook/<webhook-id>',
  discord: 'https://discord.com/api/webhooks/<id>/<token>',
  gotify: 'https://gotify.example.com/message',
}

export default function SettingsNotifications() {
  const t = useT()
  const notifiers = useResource<Notifier[]>(() => api.notifiers())
  const meta = useResource(() => api.notifierMeta())
  const runner = useRunner()
  const { busy, run } = runner
  const reload = notifiers.reload
  const events = meta.data?.events ?? []

  const [form, setForm] = useState<{
    name: string
    type: NotifierType
    url: string
    token: string
    events: string[]
  }>({ name: '', type: 'homeassistant', url: '', token: '', events: [] })

  const submit = (submitEvent: React.FormEvent) => {
    submitEvent.preventDefault()
    void run(async () => {
      const created = await api.createNotifier(form)
      setForm({ name: '', type: 'homeassistant', url: '', token: '', events: [] })
      return t('Added notifier {name}.', { name: created.name })
    }, reload)
  }

  const toggleEvent = (name: string) =>
    setForm({
      ...form,
      events: form.events.includes(name)
        ? form.events.filter((item) => item !== name)
        : [...form.events, name],
    })

  return (
    <>
      <RunnerBanners state={runner} />
      <Card
        title={t('Notifications')}
        hint={t('Zero or more targets can be active at once. Leave every event unticked to receive all of them.')}
      >
        <form onSubmit={submit}>
          <div className="row">
            <div className="field">
              <label htmlFor="n-name">{t('Name')}</label>
              <input
                id="n-name"
                value={form.name}
                onChange={(event) => setForm({ ...form, name: event.target.value })}
                required
              />
            </div>
            <div className="field fixed" style={{ width: 170 }}>
              <label htmlFor="n-type">{t('Type')}</label>
              <select
                id="n-type"
                value={form.type}
                onChange={(event) => setForm({ ...form, type: event.target.value as NotifierType })}
              >
                <option value="homeassistant">Home Assistant</option>
                <option value="discord">Discord</option>
                <option value="gotify">Gotify</option>
              </select>
            </div>
            <div className="field" style={{ flex: '2 1 320px' }}>
              <label htmlFor="n-url">{t('Webhook URL')}</label>
              <input
                id="n-url"
                value={form.url}
                placeholder={URL_HINTS[form.type]}
                onChange={(event) => setForm({ ...form, url: event.target.value })}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="n-token">
                {form.type === 'gotify' ? t('Application token') : t('Bearer token (optional)')}
              </label>
              <input
                id="n-token"
                type="password"
                value={form.token}
                autoComplete="new-password"
                onChange={(event) => setForm({ ...form, token: event.target.value })}
              />
            </div>
          </div>

          <div style={{ margin: '4px 0 12px' }}>
            {events.map((name) => (
              <label className="checkbox" key={name}>
                <input
                  type="checkbox"
                  checked={form.events.includes(name)}
                  onChange={() => toggleEvent(name)}
                />
                {t(EVENT_LABELS[name] ?? name)}
              </label>
            ))}
          </div>

          <button className="primary" type="submit" disabled={busy}>
            {t('Add notifier')}
          </button>
        </form>

        <div style={{ marginTop: 18 }}>
          {notifiers.data?.length ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t('Name')}</th>
                    <th>{t('Type')}</th>
                    <th>{t('URL')}</th>
                    <th>{t('Events')}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {notifiers.data.map((target) => (
                    <tr key={target.id} style={{ opacity: target.enabled ? 1 : 0.55 }}>
                      <td>{target.name}</td>
                      <td>{target.type}</td>
                      <td className="mono">{target.url}</td>
                      <td>
                        {target.events.length
                          ? target.events.map((name) => t(EVENT_LABELS[name] ?? name)).join(', ')
                          : t('all events')}
                        {target.last_error ? (
                          <div className="mono" style={{ color: 'var(--danger)', marginTop: 4 }}>
                            {target.last_error}
                          </div>
                        ) : null}
                      </td>
                      <td className="right">
                        <button
                          className="small"
                          disabled={busy}
                          onClick={() =>
                            void run(async () => {
                              const result = await api.testNotifier(target.id)
                              return result.error
                                ? t('Test failed: {error}', { error: result.error })
                                : t('Test notification sent to {name}.', { name: target.name })
                            }, reload)
                          }
                        >
                          {t('Test')}
                        </button>{' '}
                        <button
                          className="small"
                          disabled={busy}
                          onClick={() =>
                            void run(async () => {
                              await api.updateNotifier(target.id, { enabled: !target.enabled })
                              return target.enabled
                                ? t('{name} disabled.', { name: target.name })
                                : t('{name} enabled.', { name: target.name })
                            }, reload)
                          }
                        >
                          {target.enabled ? t('Disable') : t('Enable')}
                        </button>{' '}
                        <button
                          className="small danger"
                          disabled={busy}
                          onClick={() =>
                            void run(async () => {
                              await api.deleteNotifier(target.id)
                              return t('Removed {name}.', { name: target.name })
                            }, reload)
                          }
                        >
                          {t('Remove')}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty>{t('No notification targets configured.')}</Empty>
          )}
        </div>
      </Card>
    </>
  )
}
