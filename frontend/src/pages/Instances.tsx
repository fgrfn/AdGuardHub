import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { Instance } from '../api/types'
import { InstanceForm } from '../components/InstanceForm'
import { BLANK_DRAFT, type InstanceDraft, draftFrom } from '../components/instanceDraft'
import { Badge, Banner, Card, Empty, PageHeader } from '../components/ui'
import { IconDots } from '../components/icons'
import { formatTime } from '../format'
import { errorMessage, useResource } from '../hooks/useApi'
import { NodeUpdate } from '../components/NodeUpdate'
import { SubTabs } from '../components/SubTabs'
import { useT } from '../i18n'
import { INSTANCE_TABS } from '../nav'
import { behindLabel, nodesBehind } from '../nodes'

export default function Instances() {
  const t = useT()
  const instances = useResource<Instance[]>(() => api.instances())
  const [draft, setDraft] = useState<InstanceDraft>({ ...BLANK_DRAFT })
  const [editing, setEditing] = useState<Instance | null>(null)
  const [editDraft, setEditDraft] = useState<InstanceDraft>({ ...BLANK_DRAFT })
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
      await instances.reload()
    }
  }

  const add = () =>
    run(async () => {
      const created = await api.createInstance(draft)
      setDraft({ ...BLANK_DRAFT })
      return t('Added {name}.', { name: created.name })
    })

  const startEdit = (instance: Instance) => {
    setEditing(instance)
    setEditDraft(draftFrom(instance))
    setError('')
    setMessage('')
  }

  const saveEdit = () =>
    run(async () => {
      if (!editing) return ''
      // An untouched password field must not wipe the stored credential.
      const payload: Record<string, unknown> = {
        name: editDraft.name,
        base_url: editDraft.base_url,
        username: editDraft.username,
        verify_tls: editDraft.verify_tls,
      }
      if (editDraft.password) payload.password = editDraft.password
      const updated = await api.updateInstance(editing.id, payload)
      setEditing(null)
      return t('Saved {name}.', { name: updated.name })
    })

  const toggle = (instance: Instance) =>
    run(async () => {
      await api.updateInstance(instance.id, { enabled: !instance.enabled })
      return instance.enabled
        ? t('{name} is now disabled.', { name: instance.name })
        : t('{name} is now enabled.', { name: instance.name })
    })

  const toggleMaintenance = (instance: Instance) =>
    run(async () => {
      await api.updateInstance(instance.id, { maintenance: !instance.maintenance })
      return instance.maintenance
        ? t('{name} is back under the hub. Anything it missed is being applied now.', {
            name: instance.name,
          })
        : t('{name} is in maintenance. It will not be pushed to or corrected until you end it.', {
            name: instance.name,
          })
    })

  const test = (instance: Instance) =>
    run(async () => {
      const result = await api.testInstance(instance.id)
      return t('{name} responded — AdGuard Home {version}.', {
        name: instance.name,
        version: result.version,
      })
    })

  const push = (instance: Instance) =>
    run(async () => {
      let result
      try {
        result = await api.pushInstance(instance.id)
      } catch (caught) {
        // 409 means the hub is empty and this push would erase the node. The
        // backend counted what would go, so the question names the real cost
        // rather than a generality — and answering it repeats the same press
        // with the answer attached.
        if (!(caught instanceof ApiError) || caught.status !== 409) throw caught
        if (!confirm(caught.message)) {
          return t('Nothing was pushed to {name}.', { name: instance.name })
        }
        result = await api.pushInstance(instance.id, true)
      }
      return result.error
        ? t('Push to {name} failed: {error}', { name: instance.name, error: result.error })
        : t('{name} now has the full hub configuration.', { name: instance.name })
    })

  const remove = (instance: Instance) => {
    if (
      !confirm(
        t('Remove {name} from AdGuardHub? Its own configuration is left as-is.', {
          name: instance.name,
        }),
      )
    )
      return
    void run(async () => {
      await api.deleteInstance(instance.id)
      if (editing?.id === instance.id) setEditing(null)
      return t('Removed {name}.', { name: instance.name })
    })
  }

  const importFrom = (instance: Instance) => {
    if (
      !confirm(
        t("Import {name}'s configuration as the hub's state?", { name: instance.name }) +
          '\n\n' +
          t(
            'This replaces every rule, subscription and instance setting in AdGuardHub, then pushes the result to all other instances — overwriting whatever they currently have.',
          ) +
          '\n\n' +
          t(
            'Encryption is the exception: it is adopted but left switched off, because enabling it on a node without a valid certificate would make that node unreachable.',
          ),
      )
    )
      return
    void run(async () => {
      const result = await api.importInstance(instance.id, { replace: true })
      const review = result.sections_needing_review.length
        ? ' ' +
          t(
            '{names} was adopted but left switched off — enabling it can make a node unreachable, so review it under Instance settings first.',
            { names: result.sections_needing_review.join(', ') },
          )
        : ''
      return (
        t(
          'Imported {rules} rule(s), {lists} subscription(s) and {sections} settings area(s) from {name}; pushing to the other instances now.',
          {
            rules: result.rules_imported,
            lists: result.filter_lists_imported,
            sections: result.sections_imported.length,
            name: result.instance,
          },
        ) + review
      )
    })
  }

  return (
    <>
      <PageHeader
        title={t('Instances')}
        description={t('Add each AdGuard Home instance once. Credentials are encrypted at rest with the key from ADGUARDHUB_SECRET_KEY and are never sent back to the browser.')}
      />
      <SubTabs tabs={INSTANCE_TABS} />

      {error ? <Banner kind="error">{error}</Banner> : null}
      {message ? <Banner kind="ok">{message}</Banner> : null}
      {instances.error ? <Banner kind="error">{instances.error}</Banner> : null}

      {editing ? (
        <Card title={t('Edit {name}', { name: editing.name })} hint={t('Test the connection before saving.')}>
          <InstanceForm
            draft={editDraft}
            onChange={setEditDraft}
            onSubmit={saveEdit}
            onCancel={() => setEditing(null)}
            submitLabel={t('Save changes')}
            busy={busy}
            existingId={editing.id}
          />
        </Card>
      ) : (
        <Card
          title={t('Add an instance')}
          hint={t('Test the connection first — it verifies the URL and credentials without saving anything.')}
        >
          <InstanceForm
            draft={draft}
            onChange={setDraft}
            onSubmit={add}
            submitLabel={t('Add instance')}
            busy={busy}
          />
        </Card>
      )}

      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 16,
          margin: '22px 0 12px',
          flexWrap: 'wrap',
        }}
      >
        <h2 style={{ margin: 0, fontSize: 16, fontWeight: 620 }}>{t('Connected instances')}</h2>
        <p style={{ margin: 0, color: 'var(--dim)', fontSize: 13 }}>
          {t(
            'Importing one instance as the master replaces the hub state and overwrites every other node on the next push.',
          )}
        </p>
      </div>

      {nodesBehind(instances.data).length ? (
        <Banner kind="warn">
          {/* Two sentences rather than one with "(s)": "1 Ihrer Nodes haben" is
              wrong in German and "1 of your nodes have" is wrong in English. */}
          {nodesBehind(instances.data).length === 1
            ? t('One node has a newer AdGuard Home available: {names}.', {
                names: behindLabel(nodesBehind(instances.data)),
              })
            : t('{count} nodes have a newer AdGuard Home available: {names}.', {
                count: nodesBehind(instances.data).length,
                names: behindLabel(nodesBehind(instances.data)),
              })}
        </Banner>
      ) : null}

      {instances.data && instances.data.length ? (
        <div className="cards-3">
          {instances.data.map((instance) => (
            <NodeCard
              key={instance.id}
              instance={instance}
              busy={busy}
              onEdit={() => startEdit(instance)}
              onTest={() => test(instance)}
              onPush={() => push(instance)}
              onImport={() => importFrom(instance)}
              onToggle={() => toggle(instance)}
              onToggleMaintenance={() => toggleMaintenance(instance)}
              onRemove={() => remove(instance)}
            />
          ))}
        </div>
      ) : (
        <Card>
          <Empty>{t('No instances configured yet.')}</Empty>
        </Card>
      )}
    </>
  )
}

interface NodeCardProps {
  instance: Instance
  busy: boolean
  onEdit: () => void
  onTest: () => void
  onPush: () => void
  onImport: () => void
  onToggle: () => void
  onToggleMaintenance: () => void
  onRemove: () => void
}

/**
 * One node per card. Only the everyday actions get a button; importing as master,
 * disabling and removing sit in the overflow — putting "Remove" next to "Push now"
 * at the same weight is how the wrong one gets clicked.
 */
function NodeCard({
  instance,
  busy,
  onEdit,
  onTest,
  onPush,
  onImport,
  onToggle,
  onToggleMaintenance,
  onRemove,
}: NodeCardProps) {
  const t = useT()
  const [menu, setMenu] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menu) return
    const close = (event: MouseEvent) => {
      if (!wrap.current?.contains(event.target as Node)) setMenu(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [menu])

  const down = instance.status === 'unreachable'
  const run = (action: () => void) => () => {
    setMenu(false)
    action()
  }

  return (
    <div className={`node-card${down ? ' down' : ''}`}>
      <div className="node-head">
        <span
          className={`node-dot${down ? ' down' : instance.enabled ? '' : ' off'}`}
        />
        <span className="name">{instance.name}</span>
        <Badge tone={instance.status}>{t(instance.status)}</Badge>
      </div>
      <div className="mono" style={{ color: 'var(--dim)', marginBottom: 16 }}>
        {instance.base_url}
      </div>

      {instance.maintenance ? (
        // Said on the card rather than only in the badge: "nothing is reaching
        // this node" is alarming until you know you asked for it.
        <p className="hint" style={{ margin: '-8px 0 16px' }}>
          {t(
            'Held back on purpose: no pushes, no corrections. Changes you make meanwhile are queued and applied when you end maintenance.',
          )}
        </p>
      ) : null}

      <div className="node-facts">
        <div>
          <div className="dl">{t('Version')}</div>
          <div style={{ fontSize: 13 }}>
            {instance.version || <span style={{ color: 'var(--faint)' }}>{t('unknown')}</span>}{' '}
            <NodeUpdate instance={instance} />
          </div>
        </div>
        <div>
          <div className="dl">{t('Last synced')}</div>
          <div style={{ fontSize: 13 }}>{formatTime(instance.last_synced_at)}</div>
        </div>
        <div>
          <div className="dl">{t('Last seen')}</div>
          <div style={{ fontSize: 13 }}>{formatTime(instance.last_seen_at)}</div>
        </div>
      </div>

      {instance.last_error ? (
        <div
          className="mono"
          style={{
            color: 'var(--danger-ink)',
            background: 'var(--danger-soft)',
            borderRadius: 6,
            padding: '7px 10px',
            marginBottom: 14,
          }}
        >
          {instance.last_error}
        </div>
      ) : null}

      <div className="node-actions">
        <button className="small" onClick={onEdit} disabled={busy}>
          {t('Edit')}
        </button>
        <button className="small" onClick={onTest} disabled={busy}>
          {t('Test')}
        </button>
        <button className="small" onClick={onPush} disabled={busy}>
          {t('Push now')}
        </button>
        <div className="menu" ref={wrap}>
          <button
            className="small"
            onClick={() => setMenu(!menu)}
            disabled={busy}
            aria-label={t('More actions for {name}', { name: instance.name })}
            aria-expanded={menu}
          >
            <IconDots />
          </button>
          {menu ? (
            <div className="menu-list">
              <button onClick={run(onImport)}>{t('Import as master')}</button>
              <button onClick={run(onToggle)}>{instance.enabled ? t('Disable') : t('Enable')}</button>
              <button onClick={run(onToggleMaintenance)}>
                {instance.maintenance ? t('End maintenance') : t('Start maintenance')}
              </button>
              <button className="danger" onClick={run(onRemove)}>
                {t('Remove')}
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
