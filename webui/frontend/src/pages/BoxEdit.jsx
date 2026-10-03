import { useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, asPin, platformOf } from '../api'
import { useComp } from './CompLayout'
import OsPicker from '../components/OsPicker'
import { Button, Card, ErrorBanner, Field, Modal, PlatformBadge, SearchInput, inputCls } from '../components/ui'
import { EmptyState, Icon, PageHeader, fmtMem, useConfirm, useSlashFocus, useToast } from '../components/kit'

function SettingsModal({ comp, box, onClose, onSaved }) {
  const [form, setForm] = useState(box)
  const [err, setErr] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })
  const save = async (e) => {
    e.preventDefault()
    try {
      onSaved(await api.updateBox(comp.id, box.name, { ...form, disk_gb: form.disk_gb === '' ? null : form.disk_gb }))
    } catch (e) { setErr(e) }
  }
  return (
    <Modal open onClose={onClose} title={`${box.name} settings`}>
      <form onSubmit={save} className="space-y-4">
        <ErrorBanner error={err} />
        <div className="grid grid-cols-2 gap-4">
          <Field label="Hostname" hint="Renaming keeps its services and misconfigs"><input className={`${inputCls} font-mono`} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })} /></Field>
          <Field label="Address" hint={<span className="font-mono">192.168.&lt;team&gt;.{form.last_octet}</span>}><input type="number" min="2" max="254" className={`${inputCls} font-mono`} value={form.last_octet} onChange={set('last_octet')} /></Field>
          <Field label="vCPU"><input type="number" min="1" className={inputCls} value={form.cpu ?? ''} onChange={set('cpu')} /></Field>
          <Field label="RAM (MB)"><input type="number" min="256" step="256" className={inputCls} value={form.memory_mb ?? ''} onChange={set('memory_mb')} /></Field>
          <Field label="Disk (GB)" hint="Blank keeps the template's size"><input type="number" className={inputCls} value={form.disk_gb ?? ''} onChange={set('disk_gb')} placeholder="template" /></Field>
          <Field label="Disk interface" hint="Windows templates boot from sata0"><input className={`${inputCls} font-mono`} value={form.disk_iface ?? ''} onChange={set('disk_iface')} placeholder="scsi0" /></Field>
        </div>
        <label className="flex items-start gap-3 rounded-2xl bg-sunken p-3 text-sm shadow-inset">
          <input type="checkbox" checked={!!form.unmanaged} onChange={set('unmanaged')} className="mt-0.5 accent-[var(--accent)]" />
          <span><b>Unmanaged</b><span className="block text-xs text-muted">No SSH and no nakon, e.g. a pfSense firewall. It can't carry services or misconfigs.</span></span>
        </label>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary">Save</Button>
        </div>
      </form>
    </Modal>
  )
}

const TABS = {
  misconfigs: { label: 'Misconfigs', icon: 'shield', file: 'box_vulns', blurb: 'Weaknesses planted for blue teams to find and fix.' },
  services: { label: 'Services', icon: 'service', file: 'box_services', blurb: 'What runs on the box. Scored services are what teams must keep up.' },
}

export default function BoxEdit() {
  const { comp, reload } = useComp()
  const { box: boxName } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'services' ? 'services' : 'misconfigs'
  const navigate = useNavigate()
  const ask = useConfirm()
  const toast = useToast()
  const [q, setQ] = useState('')
  const [picking, setPicking] = useState(false)
  const [settings, setSettings] = useState(false)
  const [err, setErr] = useState(null)
  const searchRef = useRef()
  useSlashFocus(searchRef)

  const box = comp.boxes.find((b) => b.name === boxName)
  if (!box) return <EmptyState icon="box" title={`No box called ${boxName}`} action={<Link className="btn" to={`/c/${comp.id}/boxes`}>All boxes</Link>} />

  const pins = (comp[TABS[tab].file][box.name] || []).map(asPin)
  const shown = pins.map((p, i) => [p, i]).filter(([p]) => `${p.name} ${p.display || ''}`.toLowerCase().includes(q.toLowerCase()))
  const base = `/c/${comp.id}/boxes/${box.name}/${tab}`

  const setTemplate = async (template) => {
    setPicking(false)
    if (template === box.template) return
    try {
      const update = { template }
      if (platformOf(template) === 'windows' && !box.disk_iface) update.disk_iface = 'sata0'
      await api.updateBox(comp.id, box.name, update)
      toast(`${box.name} now uses ${template}`)
      if (platformOf(template) !== platformOf(box.template) && pins.length) toast('Platform changed: check its pins still apply', 'error')
      reload()
    } catch (e) { setErr(e) }
  }
  const remove = async () => {
    const n = (comp.box_services[box.name] || []).length + (comp.box_vulns[box.name] || []).length
    const ok = await ask({ title: `Delete ${box.name}?`, danger: true, confirm: 'Delete box',
      body: <>Removes it from <span className="font-mono">boxes.json</span>{n ? <> along with its {n} service/misconfig pin{n === 1 ? '' : 's'}</> : null}. Nothing deployed is touched.</> })
    if (!ok) return
    try { await api.deleteBox(comp.id, box.name); toast(`Deleted ${box.name}`); await reload(); navigate(`/c/${comp.id}/boxes`) } catch (e) { setErr(e) }
  }

  return (
    <div className="max-w-5xl pb-6">
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <PageHeader mono crumbs={[[comp.name, `/c/${comp.id}`], ['Boxes', `/c/${comp.id}/boxes`], [box.name]]} title={box.name}
        sub={<span className="flex flex-wrap items-center gap-4 font-mono text-xs">
          <span className="flex items-center gap-1.5"><Icon name="net" className="h-3.5 w-3.5 text-faint" />192.168.&lt;team&gt;.{box.last_octet}</span>
          <span className="flex items-center gap-1.5"><Icon name="cpu" className="h-3.5 w-3.5 text-faint" />{box.cpu} vCPU</span>
          <span className="flex items-center gap-1.5"><Icon name="ram" className="h-3.5 w-3.5 text-faint" />{fmtMem(box.memory_mb)}</span>
          <span className="flex items-center gap-1.5"><Icon name="disk" className="h-3.5 w-3.5 text-faint" />{box.disk_gb ? `${box.disk_gb} GB` : 'template disk'}</span>
        </span>}
        actions={<>
          <Button onClick={() => setSettings(true)}><Icon name="gear" />Settings</Button>
          <Button variant="danger" onClick={remove}><Icon name="trash" />Delete</Button>
        </>} />

      <button onClick={() => setPicking(true)} className="btn rise rise-1 gap-3 !px-5 !py-2.5" title="Change operating system">
        <PlatformBadge platform={platformOf(box.template)} />
        <span className="font-mono">{box.template}</span>
        <Icon name="edit" className="h-3.5 w-3.5 text-faint" />
      </button>

      {box.unmanaged ? (
        <Card className="rise rise-2 mt-10 p-6"><EmptyState icon="shield" title="Unmanaged box">nakon doesn't SSH into this box, so it can't carry services or misconfigs.</EmptyState></Card>
      ) : (
        <Card className="rise rise-2 mt-10 p-6">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-4">
            <div className="flex gap-1 rounded-full bg-sunken p-1 shadow-inset" role="tablist">
              {Object.entries(TABS).map(([t, def]) => (
                <button key={t} role="tab" aria-selected={tab === t} onClick={() => { setParams({ tab: t }); setQ('') }}
                  className={`flex items-center gap-2 rounded-full px-4 py-1.5 text-sm font-semibold transition ${tab === t ? 'bg-bg text-fg shadow-tile' : 'text-muted hover:text-fg'}`}>
                  <Icon name={def.icon} />{def.label}
                  <span className={`text-xs tabular-nums ${tab === t ? 'text-accent' : 'opacity-60'}`}>{(comp[def.file][box.name] || []).length}</span>
                </button>
              ))}
            </div>
            <SearchInput inputRef={searchRef} value={q} onChange={setQ} className="w-72" placeholder={`Filter ${TABS[tab].label.toLowerCase()}`} hotkey="/" />
          </div>
          <p className="mb-5 px-1 text-xs text-faint">{TABS[tab].blurb}</p>

          {pins.length === 0 ? (
            <EmptyState icon={TABS[tab].icon} title={`No ${TABS[tab].label.toLowerCase()} yet`}
              action={<Link to={`${base}?add=1`} className="btn btn-primary"><Icon name="plus" />Browse the catalog</Link>}>
              Pick from the vulndb catalog, filtered to {platformOf(box.template)}.
            </EmptyState>
          ) : (
            <>
              <ul className="space-y-3">
                {shown.length === 0 && <li className="py-4 text-center text-sm text-faint">Nothing matches “{q}”.</li>}
                {shown.map(([p, i]) => (
                  <li key={i}>
                    <Link to={`${base}?sel=${i}`} className="tile tile-hover group flex items-center gap-3 px-5 py-3">
                      <span className="min-w-0 flex-1 truncate font-mono text-sm">{p.name}</span>
                      {p.display && <span className="chip bg-sunken font-mono text-muted">{p.display}{p.port ? `:${p.port}` : ''}</span>}
                      {p.score_only && <span className="chip bg-sunken text-eng" title="Scored but not planted by nakon">score only</span>}
                      {p.plant_only && <span className="chip bg-sunken text-lin" title="Planted by nakon but not scored">plant only</span>}
                      {p.vars && <span className="text-xs text-faint">{Object.keys(p.vars).length} var{Object.keys(p.vars).length === 1 ? '' : 's'}</span>}
                      <span className="flex items-center gap-1 text-sm text-faint transition group-hover:text-accent">Edit<Icon name="chevron" className="h-3.5 w-3.5" /></span>
                    </Link>
                  </li>
                ))}
              </ul>
              <div className="mt-6 flex justify-center">
                <Link to={`${base}?add=1`} className="btn btn-primary"><Icon name="plus" />Add another</Link>
              </div>
            </>
          )}
        </Card>
      )}
      <OsPicker open={picking} value={box.template} onClose={() => setPicking(false)} onPick={setTemplate} />
      {settings && <SettingsModal comp={comp} box={box} onClose={() => setSettings(false)}
        onSaved={async (b) => { setSettings(false); toast('Box settings saved'); await reload(); if (b.name !== box.name) navigate(`/c/${comp.id}/boxes/${b.name}`, { replace: true }) }} />}
    </div>
  )
}
