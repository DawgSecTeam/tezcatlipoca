import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, kindOf, platformOf } from '../api'
import { useComp } from './CompLayout'
import Topology, { TopologyLegend } from '../components/Topology'
import OsPicker from '../components/OsPicker'
import { Button, Card, ErrorBanner, Field, Modal, PlatformBadge, inputCls } from '../components/ui'
import { EmptyState, Icon, PageHeader, fmtMem, useToast } from '../components/kit'

function NewBoxModal({ open, onClose, comp, onCreated }) {
  const used = new Set(comp.boxes.map((b) => b.last_octet))
  let nextOctet = 2
  while (used.has(nextOctet)) nextOctet++
  const [form, setForm] = useState({ name: '', template: '', cpu: 1, memory_mb: 2048, disk_gb: 15 })
  const [octet, setOctet] = useState('')
  const [picking, setPicking] = useState(false)
  const [err, setErr] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const taken = comp.boxes.some((b) => b.name === form.name)
  const ip = Number(octet || nextOctet)
  const ipTaken = used.has(ip)
  const submit = async (e) => {
    e.preventDefault()
    try {
      const isWin = platformOf(form.template) === 'windows'
      const box = await api.addBox(comp.id, { ...form, last_octet: ip, ...(isWin ? { disk_iface: 'sata0' } : {}) })
      onCreated(box)
    } catch (e) { setErr(e) }
  }
  return (
    <Modal open={open} onClose={onClose} title="Add a box">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <div className="grid grid-cols-[1fr_9rem] gap-4">
          <Field label="Hostname" hint={taken ? <span className="text-danger">Already used in this comp</span> : 'Lowercase, digits, dashes'}>
            <input className={`${inputCls} font-mono`} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })} placeholder="web01" required autoFocus pattern="[a-z][a-z0-9\-]*" />
          </Field>
          <Field label="Address" hint={ipTaken ? <span className="text-danger">.{ip} is taken</span> : <span className="font-mono">192.168.&lt;team&gt;.{ip}</span>}>
            <div className="relative">
              <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 font-mono text-sm text-faint">.</span>
              <input inputMode="numeric" className={`${inputCls} pl-5 font-mono`} value={octet} placeholder={String(nextOctet)} onChange={(e) => setOctet(e.target.value.replace(/\D/g, ''))} />
            </div>
          </Field>
        </div>
        <Field label="Operating system">
          <button type="button" onClick={() => setPicking(true)} className={`${inputCls} flex items-center justify-between text-left font-mono ${form.template ? '' : 'text-faint'}`}>
            <span>{form.template || 'Choose a template…'}</span>
            {form.template ? <PlatformBadge platform={platformOf(form.template)} /> : <Icon name="chevron" className="h-3.5 w-3.5" />}
          </button>
        </Field>
        <div className="grid grid-cols-3 gap-4">
          <Field label="vCPU"><input type="number" min="1" className={inputCls} value={form.cpu} onChange={set('cpu')} /></Field>
          <Field label="RAM (MB)"><input type="number" min="256" step="256" className={inputCls} value={form.memory_mb} onChange={set('memory_mb')} /></Field>
          <Field label="Disk (GB)"><input type="number" min="1" className={inputCls} value={form.disk_gb} onChange={set('disk_gb')} /></Field>
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.template || taken || ipTaken}><Icon name="plus" />Add box</Button>
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

function NewFirewallModal({ open, onClose, comp, onCreated }) {
  const [form, setForm] = useState({ name: 'fw01', template: '', cpu: 1, memory_mb: 512, disk_gb: 12 })
  const [picking, setPicking] = useState(false)
  const [err, setErr] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const taken = comp.boxes.some((b) => b.name === form.name)
  const exists = comp.boxes.some((b) => b.in_path)
  // Default the template to whatever firewall-shaped template the node knows (pfSense
  // first, VyOS as the second kind); the OsPicker stays available for a
  // differently-named appliance template.
  useEffect(() => {
    if (open && !form.template) {
      api.templates().then((ts) => {
        const fw = ts.find((t) => /pfsense|opnsense|vyos/i.test(t.name))
        if (fw) setForm((f) => ({ ...f, template: fw.name }))
      }).catch(() => {})
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps
  const submit = async (e) => {
    e.preventDefault()
    try {
      const box = await api.addBox(comp.id, { ...form, last_octet: 1, unmanaged: true, in_path: true })
      onCreated(box)
    } catch (e) { setErr(e) }
  }
  return (
    <Modal open={open} onClose={onClose} title="Add the in-path firewall">
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <p className="text-sm leading-relaxed text-muted">
          One firewall per competition: every team gets its own clone, wired
          engine&nbsp;→&nbsp;transit&nbsp;→&nbsp;firewall&nbsp;→&nbsp;team switch. It owns the
          team gateway address <span className="font-mono">.1</span>, so it carries no
          services or misconfigs — it is infrastructure, not a target.
        </p>
        <div className="grid grid-cols-[1fr_9rem] gap-4">
          <Field label="Hostname" hint={taken ? <span className="text-danger">Already used in this comp</span> : 'Lowercase, digits, dashes'}>
            <input className={`${inputCls} font-mono`} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })} placeholder="fw01" required pattern="[a-z][a-z0-9\-]*" />
          </Field>
          <Field label="LAN address" hint={<span className="font-mono">the team gateway</span>}>
            <div className="relative">
              <input disabled className={`${inputCls} pl-5 font-mono text-faint`} value=".1" />
            </div>
          </Field>
        </div>
        <Field label="Operating system" hint="A pfSense- or VyOS-class appliance template on the node">
          <button type="button" onClick={() => setPicking(true)} className={`${inputCls} flex items-center justify-between text-left font-mono ${form.template ? '' : 'text-faint'}`}>
            <span>{form.template || 'Choose a template…'}</span>
            {form.template ? <PlatformBadge platform="firewall" /> : <Icon name="chevron" className="h-3.5 w-3.5" />}
          </button>
        </Field>
        <div className="grid grid-cols-3 gap-4">
          <Field label="vCPU"><input type="number" min="1" className={inputCls} value={form.cpu} onChange={set('cpu')} /></Field>
          <Field label="RAM (MB)"><input type="number" min="256" step="256" className={inputCls} value={form.memory_mb} onChange={set('memory_mb')} /></Field>
          <Field label="Disk (GB)"><input type="number" min="1" className={inputCls} value={form.disk_gb} onChange={set('disk_gb')} /></Field>
        </div>
        <div className="rounded-2xl bg-sunken p-3 font-mono text-xs text-muted shadow-inset">
          <div>WAN&nbsp;&nbsp;172.31.&lt;team&gt;.2/30 · transit vmbrW&lt;team&gt;</div>
          <div>LAN&nbsp;&nbsp;192.168.&lt;team&gt;.1/24 · the boxes' gateway</div>
        </div>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.template || taken || exists}>
            <Icon name="plus" />{exists ? 'Firewall exists' : 'Add firewall'}
          </Button>
        </div>
      </form>
      <OsPicker open={picking} value={form.template} onClose={() => setPicking(false)} onPick={(t) => { setForm({ ...form, template: t }); setPicking(false) }} />
    </Modal>
  )
}

function Spec({ icon, children }) {
  return <span className="flex items-center gap-1.5 text-xs text-muted"><Icon name={icon} className="h-3.5 w-3.5 text-faint" />{children}</span>
}

export default function Boxes() {
  const { comp, reload } = useComp()
  const navigate = useNavigate()
  const toast = useToast()
  const [adding, setAdding] = useState(false)
  const [addingFw, setAddingFw] = useState(false)
  const count = (m, n) => (m[n] || []).length
  const sorted = [...comp.boxes].sort((a, b) => a.last_octet - b.last_octet)
  const total = (k) => comp.boxes.reduce((a, b) => a + (Number(b[k]) || 0), 0)

  return (
    <div className="pb-6">
      <PageHeader crumbs={[[comp.name, `/c/${comp.id}`], ['Boxes']]} title="Boxes"
        sub={comp.boxes.length ? `${comp.boxes.length} per team · ${total('cpu')} vCPU · ${fmtMem(total('memory_mb'))} RAM per team` : 'The machines every team gets a copy of'}
        actions={<div className="flex gap-3">
          {!comp.boxes.some((b) => b.in_path) &&
            <Button onClick={() => setAddingFw(true)}><Icon name="net" />Add firewall</Button>}
          <Button variant="primary" onClick={() => setAdding(true)}><Icon name="plus" />Add box</Button>
        </div>} />

      <Card className="rise rise-1 mb-10 p-6">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="display text-2xl">Topology</h2>
          <TopologyLegend boxes={comp.boxes} />
        </div>
        <Topology boxes={comp.boxes} onSelect={(b) => navigate(`/c/${comp.id}/boxes/${b.name}`)} />
      </Card>

      <Card className="rise rise-2 p-6">
        <h2 className="display mb-5 text-2xl">All boxes</h2>
        {comp.boxes.length === 0 ? (
          <EmptyState icon="box" title="No boxes yet" action={<Button variant="primary" onClick={() => setAdding(true)}><Icon name="plus" />Add the first box</Button>}>
            Pick an operating system and an address; services and misconfigs come next.
          </EmptyState>
        ) : (
          <div className="space-y-3">
            {sorted.map((b) => (
              <Link key={b.name} to={`/c/${comp.id}/boxes/${b.name}`} className="tile tile-hover group flex items-center gap-5 px-5 py-3">
                <div className="w-32 min-w-0">
                  <div className="truncate font-mono font-semibold">{b.name}</div>
                  <div className="font-mono text-xs text-faint">.{b.last_octet}</div>
                </div>
                <div className="flex min-w-0 flex-1 items-center gap-2">
                  <PlatformBadge platform={kindOf(b)} />
                  <span className="truncate font-mono text-sm text-muted">{b.template}</span>
                  {b.in_path && <span className="chip bg-sunken text-fw">firewall · in-path</span>}
                  {b.unmanaged && !b.in_path && <span className="chip bg-sunken text-faint">unmanaged</span>}
                </div>
                <div className="hidden items-center gap-4 lg:flex">
                  <Spec icon="cpu">{b.cpu}</Spec>
                  <Spec icon="ram">{fmtMem(b.memory_mb)}</Spec>
                  <Spec icon="disk">{b.disk_gb ? `${b.disk_gb} GB` : 'template'}</Spec>
                </div>
                <div className="flex w-36 items-center justify-end gap-4">
                  <Spec icon="service">{count(comp.box_services, b.name)}</Spec>
                  <Spec icon="shield">{count(comp.box_vulns, b.name)}</Spec>
                </div>
                <span className="flex items-center gap-1 text-sm text-faint transition group-hover:text-accent">Edit<Icon name="chevron" className="h-3.5 w-3.5" /></span>
              </Link>
            ))}
          </div>
        )}
      </Card>
      <NewBoxModal key={adding} open={adding} comp={comp} onClose={() => setAdding(false)}
        onCreated={async (b) => { setAdding(false); toast(`Added ${b.name}`); await reload(); navigate(`/c/${comp.id}/boxes/${b.name}`) }} />
      <NewFirewallModal key={addingFw} open={addingFw} comp={comp} onClose={() => setAddingFw(false)}
        onCreated={async (b) => { setAddingFw(false); toast(`Added firewall ${b.name}`); await reload(); navigate(`/c/${comp.id}/boxes/${b.name}`) }} />
    </div>
  )
}
