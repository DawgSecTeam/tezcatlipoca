import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, platformOf } from '../api'
import { useComp } from './CompLayout'
import Topology from '../components/Topology'
import OsPicker from '../components/OsPicker'
import { Button, Card, ErrorBanner, Field, Modal, PlatformBadge, inputCls } from '../components/ui'

function NewBoxModal({ open, onClose, comp, onCreated }) {
  const nextOctet = Math.max(1, ...comp.boxes.filter((b) => b.last_octet < 200).map((b) => b.last_octet)) + 1
  const [form, setForm] = useState({ name: '', template: '', cpu: 1, memory_mb: 2048, disk_gb: 15 })
  const [octet, setOctet] = useState('')
  const [picking, setPicking] = useState(false)
  const [err, setErr] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const submit = async (e) => {
    e.preventDefault()
    try {
      const isWin = platformOf(form.template) === 'windows'
      const box = await api.addBox(comp.id, {
        ...form, last_octet: Number(octet || nextOctet),
        ...(isWin ? { disk_iface: 'sata0' } : {}),
      })
      onCreated(box)
    } catch (e) { setErr(e) }
  }
  return (
    <Modal open={open} onClose={onClose} title="Add box">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <div className="grid grid-cols-2 gap-4">
          <Field label="Name"><input className={`${inputCls} font-mono`} value={form.name} onChange={set('name')} placeholder="web01" required autoFocus /></Field>
          <Field label="IP (.last octet)"><input type="number" className={inputCls} value={octet} placeholder={String(nextOctet)} onChange={(e) => setOctet(e.target.value)} /></Field>
        </div>
        <Field label="Operating system">
          <button type="button" onClick={() => setPicking(true)} className={`${inputCls} text-left font-mono ${form.template ? '' : 'text-faint'}`}>{form.template || 'Choose…'}</button>
        </Field>
        <div className="grid grid-cols-3 gap-4">
          <Field label="CPU"><input type="number" className={inputCls} value={form.cpu} onChange={set('cpu')} /></Field>
          <Field label="RAM (MB)"><input type="number" className={inputCls} value={form.memory_mb} onChange={set('memory_mb')} /></Field>
          <Field label="Disk (GB)"><input type="number" className={inputCls} value={form.disk_gb} onChange={set('disk_gb')} /></Field>
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.template}>Add box</Button>
        </div>
      </form>
      <OsPicker open={picking} value={form.template} onClose={() => setPicking(false)} onPick={(t) => {
        // Windows templates need more than the linux defaults (every shipped comp uses 2/4096/60).
        const win = platformOf(t) === 'windows'
        setForm({ ...form, template: t, ...(win ? { cpu: 2, memory_mb: 4096, disk_gb: 60 } : {}) })
        setPicking(false)
      }} />
    </Modal>
  )
}

export default function Boxes() {
  const { comp, reload } = useComp()
  const navigate = useNavigate()
  const [adding, setAdding] = useState(false)
  const count = (m, n) => (m[n] || []).length

  return (
    <div className="pb-6">
      <h1 className="mb-8 text-4xl font-bold tracking-tight">Boxes</h1>
      <Card className="mb-10 p-6">
        <h2 className="mb-3 text-xl font-semibold">Topology</h2>
        <Topology boxes={comp.boxes} onSelect={(b) => navigate(`/c/${comp.id}/boxes/${b.name}`)} />
      </Card>
      <Card className="p-6">
        <div className="mb-5 flex items-center justify-between">
          <h2 className="text-xl font-semibold">Boxes</h2>
          <Button variant="primary" onClick={() => setAdding(true)}>+ Add box</Button>
        </div>
        <div className="space-y-3">
          {comp.boxes.length === 0 && <p className="py-4 text-center text-sm text-faint">No boxes yet.</p>}
          {[...comp.boxes].sort((a, b) => a.last_octet - b.last_octet).map((b) => (
            <div key={b.name} className="tile flex items-center gap-4 px-5 py-3">
              <div className="w-28 font-mono font-semibold">{b.name}</div>
              <div className="w-14 font-mono text-sm text-faint">.{b.last_octet}</div>
              <div className="flex flex-1 items-center gap-2 font-mono text-sm text-muted"><PlatformBadge platform={platformOf(b.template)} /> {b.template}{b.unmanaged && <span className="text-xs text-faint">(unmanaged)</span>}</div>
              <div className="text-sm text-faint">{count(comp.box_services, b.name)} svc · {count(comp.box_vulns, b.name)} misc</div>
              <Link to={`/c/${comp.id}/boxes/${b.name}`} className="btn !py-1">edit</Link>
            </div>
          ))}
        </div>
      </Card>
      <NewBoxModal open={adding} comp={comp} onClose={() => setAdding(false)}
        onCreated={async (b) => { setAdding(false); await reload(); navigate(`/c/${comp.id}/boxes/${b.name}`) }} />
    </div>
  )
}
