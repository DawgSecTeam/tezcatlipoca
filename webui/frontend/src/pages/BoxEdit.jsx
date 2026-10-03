import { useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, asPin, platformOf } from '../api'
import { useComp } from './CompLayout'
import OsPicker from '../components/OsPicker'
import { Button, Card, ErrorBanner, Field, Modal, PlatformBadge, SearchInput, inputCls } from '../components/ui'

function HardwareModal({ open, onClose, comp, box, onSaved }) {
  const [form, setForm] = useState(box)
  const [err, setErr] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })
  const save = async (e) => {
    e.preventDefault()
    try {
      const saved = await api.updateBox(comp.id, box.name, {
        ...form, disk_gb: form.disk_gb === '' ? null : form.disk_gb,
      })
      onSaved(saved)
    } catch (e) { setErr(e) }
  }
  return (
    <Modal open={open} onClose={onClose} title={`${box.name} — settings`}>
      <form onSubmit={save} className="space-y-3">
        <ErrorBanner error={err} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name"><input className={`${inputCls} font-mono`} value={form.name} onChange={set('name')} /></Field>
          <Field label="IP (.last octet)"><input type="number" className={inputCls} value={form.last_octet} onChange={set('last_octet')} /></Field>
          <Field label="CPU"><input type="number" className={inputCls} value={form.cpu ?? ''} onChange={set('cpu')} /></Field>
          <Field label="RAM (MB)"><input type="number" className={inputCls} value={form.memory_mb ?? ''} onChange={set('memory_mb')} /></Field>
          <Field label="Disk (GB)" hint="Blank = template size"><input type="number" className={inputCls} value={form.disk_gb ?? ''} onChange={set('disk_gb')} /></Field>
          <Field label="Disk interface" hint="Windows templates use sata0"><input className={`${inputCls} font-mono`} value={form.disk_iface ?? ''} onChange={set('disk_iface')} placeholder="scsi0 (default)" /></Field>
        </div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.unmanaged} onChange={set('unmanaged')} /> Unmanaged (no SSH/nakon — e.g. pfSense)</label>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary">Save</Button>
        </div>
      </form>
    </Modal>
  )
}

export default function BoxEdit() {
  const { comp, reload } = useComp()
  const { box: boxName } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'services' ? 'services' : 'misconfigs'
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [picking, setPicking] = useState(false)
  const [settings, setSettings] = useState(false)
  const [err, setErr] = useState(null)

  const box = comp.boxes.find((b) => b.name === boxName)
  if (!box) return <div className="p-8">No box <code>{boxName}</code>. <Link className="text-indigo-600" to={`/c/${comp.id}/boxes`}>Back to boxes</Link></div>

  const pins = ((tab === 'services' ? comp.box_services : comp.box_vulns)[box.name] || []).map(asPin)
  const shown = pins.map((p, i) => [p, i]).filter(([p]) => `${p.name} ${p.display || ''}`.toLowerCase().includes(q.toLowerCase()))
  const base = `/c/${comp.id}/boxes/${box.name}/${tab}`

  const setTemplate = async (template) => {
    setPicking(false)
    try {
      const update = { template }
      if (platformOf(template) === 'windows' && !box.disk_iface) update.disk_iface = 'sata0'
      await api.updateBox(comp.id, box.name, update); reload()
    } catch (e) { setErr(e) }
  }
  const remove = async () => {
    if (!confirm(`Delete box ${box.name} and its services/misconfigs?`)) return
    try { await api.deleteBox(comp.id, box.name); await reload(); navigate(`/c/${comp.id}/boxes`) } catch (e) { setErr(e) }
  }

  const tabBtn = (t, label) => (
    <button onClick={() => { setParams({ tab: t }); setQ('') }}
      className={`rounded-t-lg border border-b-0 px-4 py-2 text-sm font-medium ${tab === t ? 'border-slate-200 bg-white text-slate-900' : 'border-transparent text-slate-500 hover:text-slate-800'}`}>
      {label} <span className="text-slate-400">{((t === 'services' ? comp.box_services : comp.box_vulns)[box.name] || []).length}</span>
    </button>
  )

  return (
    <div className="max-w-4xl p-8">
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-4xl font-bold tracking-tight">{box.name}</h1>
          <p className="mt-1 font-mono text-sm text-slate-400">192.168.&lt;team&gt;.{box.last_octet} · {box.cpu} cpu · {box.memory_mb} MB · {box.disk_gb ?? 'template'} GB</p>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => setSettings(true)}>Settings</Button>
          <Button variant="danger" onClick={remove}>Delete</Button>
        </div>
      </div>

      <button onClick={() => setPicking(true)} className="mt-4 inline-flex items-center gap-3 rounded-full border border-slate-300 bg-white px-4 py-2 text-sm shadow-sm hover:border-indigo-400">
        <PlatformBadge platform={platformOf(box.template)} />
        <span className="font-mono">{box.template}</span>
        <span className="text-slate-400">✎</span>
      </button>

      {box.unmanaged ? (
        <Card className="mt-6 p-4 text-sm text-slate-500">Unmanaged box — nakon does not SSH in, so it carries no services or misconfigs.</Card>
      ) : (
        <div className="mt-6">
          <div className="flex items-end justify-between gap-4">
            <div className="flex gap-1">{tabBtn('misconfigs', 'Misconfigs')}{tabBtn('services', 'Services')}</div>
            <SearchInput value={q} onChange={setQ} className="mb-1 w-64" placeholder={`Search ${tab}…`} />
          </div>
          <Card className="rounded-tl-none p-4">
            <ul className="space-y-2">
              {shown.length === 0 && <li className="py-4 text-center text-sm text-slate-400">{pins.length ? 'No matches.' : `No ${tab} on this box yet.`}</li>}
              {shown.map(([p, i]) => (
                <li key={i} className="flex items-center gap-3 rounded-lg border border-slate-200 px-3 py-2">
                  <span className="flex-1 font-mono text-sm">{p.name}</span>
                  {p.display && <span className="rounded bg-slate-100 px-1.5 text-xs text-slate-600">{p.display}{p.port ? `:${p.port}` : ''}</span>}
                  {p.score_only && <span className="text-xs text-violet-600">score only</span>}
                  {p.plant_only && <span className="text-xs text-amber-600">plant only</span>}
                  {p.vars && <span className="text-xs text-slate-400">{Object.keys(p.vars).length} var{Object.keys(p.vars).length === 1 ? '' : 's'}</span>}
                  <Link to={`${base}?sel=${i}`} className="rounded-md border border-slate-300 px-3 py-0.5 text-sm hover:bg-slate-100">edit</Link>
                </li>
              ))}
            </ul>
            <div className="mt-4 flex justify-center">
              <Link to={`${base}?add=1`} className="rounded-md border border-slate-300 px-4 py-1.5 text-sm font-medium hover:bg-slate-100">+ Add another</Link>
            </div>
          </Card>
        </div>
      )}
      <OsPicker open={picking} value={box.template} onClose={() => setPicking(false)} onPick={setTemplate} />
      {settings && <HardwareModal open comp={comp} box={box} onClose={() => setSettings(false)}
        onSaved={async (b) => { setSettings(false); await reload(); if (b.name !== box.name) navigate(`/c/${comp.id}/boxes/${b.name}`, { replace: true }) }} />}
    </div>
  )
}
