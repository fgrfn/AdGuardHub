import { useState } from 'react'
import { api } from '../api/client'
import type { Rule, RuleKind, RuleOrigin } from '../api/types'
import { Badge, Banner, Card, Empty, PageHeader } from '../components/ui'
import { formatTime } from '../format'
import { errorMessage, useResource } from '../hooks/useApi'
import { SubTabs } from '../components/SubTabs'
import { useT } from '../i18n'
import { FILTER_TABS } from '../nav'


type Tab = 'all' | 'block' | 'allow' | 'comment'

/**
 * Two of the three AdGuard entry points live here: free-form custom rules and the
 * Allowlist tab. The third (the query log's whitelist action) writes the same model
 * from the Query log page.
 */
export default function Rules() {
  const t = useT()
  const [tab, setTab] = useState<Tab>('all')
  const [search, setSearch] = useState('')
  const kind: RuleKind | undefined = tab === 'all' ? undefined : tab
  const rules = useResource<Rule[]>(() => api.rules({ kind, search }), [tab, search])

  const [text, setText] = useState('')
  const [domain, setDomain] = useState('')
  const [bulk, setBulk] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  async function run(action: () => Promise<string>) {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      setMessage(await action())
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
      await rules.reload()
    }
  }

  const addRule = (event: React.FormEvent) => {
    event.preventDefault()
    void run(async () => {
      const created = await api.createRule({ text, origin: 'custom' })
      setText('')
      return t('Added {rule} — pushing to all instances.', { rule: created.text })
    })
  }

  const addAllow = (event: React.FormEvent) => {
    event.preventDefault()
    void run(async () => {
      const created = await api.allowDomain(domain, 'allowlist')
      setDomain('')
      return t('Allowlisted {rule} — pushing to all instances.', { rule: created.text })
    })
  }

  const addBulk = (event: React.FormEvent) => {
    event.preventDefault()
    void run(async () => {
      const created = await api.bulkRules(bulk, 'custom')
      setBulk('')
      return created.length
        ? t('Imported {count} new rule(s).', { count: created.length })
        : t('Nothing new — every line was a duplicate, blank or a comment.')
    })
  }

  const toggle = (rule: Rule) =>
    run(async () => {
      await api.updateRule(rule.id, { enabled: !rule.enabled })
      return rule.enabled
        ? t('{rule} is now disabled.', { rule: rule.text })
        : t('{rule} is now enabled.', { rule: rule.text })
    })

  /** Drop the countdown: the rule turned out to be one worth keeping. */
  const keep = (rule: Rule) =>
    run(async () => {
      await api.updateRule(rule.id, { expires_in_minutes: 0 })
      return t('{rule} will now stay until you remove it.', { rule: rule.text })
    })

  const remove = (rule: Rule) =>
    run(async () => {
      await api.deleteRule(rule.id)
      return t('Removed {rule}.', { rule: rule.text })
    })

  return (
    <>
      <PageHeader
        title={t('Filtering rules')}
        description={t(
          'The central rule set, in native AdGuard syntax. Every change is pushed to all instances straight away.',
        )}
      />
      <SubTabs tabs={FILTER_TABS} />

      {error ? <Banner kind="error">{error}</Banner> : null}
      {message ? <Banner kind="ok">{message}</Banner> : null}
      {rules.error ? <Banner kind="error">{rules.error}</Banner> : null}

      <div className="row" style={{ alignItems: 'stretch' }}>
        <Card
          title={t('Custom rule')}
          hint={t('Any AdGuard rule, e.g. ||ads.example.com^ or @@||shop.example.com^')}
        >
          <form onSubmit={addRule} className="row">
            <input
              value={text}
              placeholder="||ads.example.com^"
              onChange={(event) => setText(event.target.value)}
              required
            />
            <button className="primary fixed" type="submit" disabled={busy}>
              {t('Add')}
            </button>
          </form>
        </Card>

        <Card
          title={t('Allowlist')}
          hint={t('A domain to never block; stored as an @@ exception rule.')}
        >
          <form onSubmit={addAllow} className="row">
            <input
              value={domain}
              placeholder="shop.example.com"
              onChange={(event) => setDomain(event.target.value)}
              required
            />
            <button className="primary fixed" type="submit" disabled={busy}>
              {t('Allow')}
            </button>
          </form>
        </Card>
      </div>

      <Card
        title={t('Bulk import')}
        hint={t('Paste AdGuard syntax, one rule per line. Blank lines are skipped; ! and # comments are kept, so a note stays with the rule it explains.')}
      >
        <form onSubmit={addBulk}>
          <textarea
            value={bulk}
            onChange={(event) => setBulk(event.target.value)}
            placeholder={'||ads.example.com^\n@@||shop.example.com^'}
          />
          <button type="submit" disabled={busy || !bulk.trim()} style={{ marginTop: 10 }}>
            {t('Import rules')}
          </button>
        </form>
      </Card>

      <Card>
        <div className="tabs">
          {(['all', 'block', 'allow', 'comment'] as Tab[]).map((item) => (
            <button
              key={item}
              className={`tab${tab === item ? ' active' : ''}`}
              onClick={() => setTab(item)}
            >
              {item === 'all'
                ? t('All')
                : item === 'block'
                  ? t('Block')
                  : item === 'allow'
                    ? t('Allow')
                    : t('Notes')}
            </button>
          ))}
          <div style={{ marginLeft: 'auto', paddingBottom: 6, width: 220 }}>
            <input
              value={search}
              placeholder={t('Search rules…')}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
        </div>

        {rules.data && rules.data.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t('Rule')}</th>
                  <th>{t('Kind')}</th>
                  <th>{t('Added via')}</th>
                  <th>{t('Updated')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rules.data.map((rule) => (
                  <tr key={rule.id} style={{ opacity: rule.enabled ? 1 : 0.55 }}>
                    <td className="mono">{rule.text}</td>
                    <td>
                      <Badge tone={rule.kind}>{t(rule.kind)}</Badge>
                    </td>
                    <td>
                      {originLabel(rule.origin, t)}
                      {/* Beside where it came from, because the two answer one
                          question: a temporary allow from the query log is the
                          shape of rule this exists for, and it should be
                          obvious at a glance which of them will clean
                          themselves up. */}
                      {rule.expires_at ? (
                        <div className="hint" title={formatTime(rule.expires_at)}>
                          {t('expires {when}', { when: formatTime(rule.expires_at) })}
                        </div>
                      ) : null}
                    </td>
                    <td>{formatTime(rule.updated_at)}</td>
                    <td className="right">
                      {rule.expires_at ? (
                        <>
                          <button className="small" onClick={() => keep(rule)} disabled={busy}>
                            {t('Keep')}
                          </button>{' '}
                        </>
                      ) : null}
                      <button className="small" onClick={() => toggle(rule)} disabled={busy}>
                        {rule.enabled ? t('Disable') : t('Enable')}
                      </button>{' '}
                      <button className="small danger" onClick={() => remove(rule)} disabled={busy}>
                        {t('Delete')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>{rules.loading ? t('Loading…') : t('No rules match.')}</Empty>
        )}
      </Card>
    </>
  )
}

function originLabel(origin: RuleOrigin, t: (text: string) => string): string {
  if (origin === 'querylog') return t('Query log')
  if (origin === 'allowlist') return t('Allowlist')
  return t('Custom rule')
}
