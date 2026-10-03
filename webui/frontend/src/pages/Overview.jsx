import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useComp } from './CompLayout'
import Topology, { TopologyLegend } from '../components/Topology'
import DeployPanel from '../components/DeployPanel'
import { Button, Card, ErrorBanner, Field, inputCls } from '../components/ui'
import { DifficultyMeter, Icon, PageHeader, useSaveHotkey, useToast } from '../components/kit'

// The Compfile keys an author normally sets; anything else in the file is kept and shown below.
const KNOWN = [
  ['name', 'Name'], ['scenario', 'Scenario'], ['difficulty', 'Difficulty'],
  ['domain_prefix', 'Domain prefix'], ['domain_suffix', 'Domain suffix'],
]

function InfoEditor({ comp, onSaved, onCancel }) {
  const [items, setItems] = useState(comp.compfile)
  const [err, setErr] = useState(null)
  const toast = useToast()
  const get = (k) => items.find(([key]) => key === k)?.[1] ?? ''
  const set = (k, v) => setItems(items.some(([key]) => key === k) ? items.map(([key, val]) => [key, key === k ? v : val]) : [...items, [k, v]])
  const extra = items.filter(([k]) => !KNOWN.some(([kk]) => kk === k))
  const save = async () => {
    try { await api.putCompfile(comp.id, items.filter(([, v]) => v !== '')); toast('Competition info saved'); onSaved() } catch (e) { setErr(e) }
  }
  useSaveHotkey(save, true)

  return (
    <Card className="rise max-w-3xl space-y-4 p-6">
      <ErrorBanner error={err} />
      <Field label="Name"><input className={inputCls} value={get('name')} onChange={(e) => set('name', e.target.value)} autoFocus /></Field>
      <Field label="Scenario" hint="The story competitors are told.">
        <textarea rows={4} className={inputCls} value={get('scenario')} onChange={(e) => set('scenario', e.target.value)} />
      </Field>
      <Field label={`Difficulty · ${get('difficulty') || '—'}/10`}>
        <input type="range" min="1" max="10" value={get('difficulty') || 5} onChange={(e) => set('difficulty', e.target.value)} className="w-full accent-[var(--accent)]" />
      </Field>
      <Field label="Team domain" hint={<>Each team gets <span className="font-mono">{get('domain_prefix')}&lt;team&gt;{get('domain_suffix')}</span>. Leave both blank for <span className="font-mono">team&lt;id&gt;.local</span>.</>}>
        <div className="flex items-center gap-2 font-mono">
          <input className={inputCls} value={get('domain_prefix')} onChange={(e) => set('domain_prefix', e.target.value)} placeholder="prefix-" />
          <span className="shrink-0 text-xs text-faint">&lt;team&gt;</span>
          <input className={inputCls} value={get('domain_suffix')} onChange={(e) => set('domain_suffix', e.target.value)} placeholder=".corp.local" />
        </div>
      </Field>
      {extra.length > 0 && <p className="text-xs text-faint">Other Compfile keys ({extra.map(([k]) => k).join(', ')}) are kept as they are.</p>}
      <div className="flex justify-end gap-3">
        <Button onClick={onCancel}>Cancel</Button>
        <Button variant="primary" onClick={save}>Save</Button>
      </div>
    </Card>
  )
}

function StatCard({ icon, n, label, hint, to, delay }) {
  return (
    <Link to={to} className={`panel rise ${delay} group block p-5 transition duration-200 hover:-translate-y-1`}>
      <div className="flex items-center justify-between text-faint">
        <Icon name={icon} className="h-5 w-5" />
        <Icon name="chevron" className="h-3.5 w-3.5 opacity-0 transition group-hover:translate-x-0.5 group-hover:opacity-100" />
      </div>
      <div className="mt-3 text-3xl font-semibold tabular-nums">{n}</div>
      <div className="text-sm text-muted">{label}</div>
      {hint && <div className="mt-0.5 text-[11px] text-faint">{hint}</div>}
    </Link>
  )
}

export default function Overview() {
  const { comp, reload } = useComp()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const cf = Object.fromEntries(comp.compfile)
  const domain = cf.domain_prefix || cf.domain_suffix ? `${cf.domain_prefix || ''}<team>${cf.domain_suffix || ''}` : 'team<id>.local'
  const base = `/c/${comp.id}`
  const bareBoxes = comp.boxes.filter((b) => !b.unmanaged && !(comp.box_vulns[b.name] || []).length).length

  return (
    <div className="pb-6">
      <PageHeader title={comp.name}
        sub={<span className="flex flex-wrap items-center gap-3"><DifficultyMeter value={cf.difficulty} /><span className="chip bg-sunken px-2.5 py-1 font-mono text-muted">{domain}</span>
          {cf.packet_source && <span className="chip bg-sunken px-2.5 py-1 text-muted" title="This comp was compiled from a packet profile">from <span className="ml-1 font-mono">{cf.packet_source}</span></span>}</span>}
        actions={!editing && <Button variant="ghost" onClick={() => setEditing(true)}><Icon name="edit" />Edit info</Button>} />

      {editing
        ? <div className="mb-10"><InfoEditor comp={comp} onCancel={() => setEditing(false)} onSaved={() => { setEditing(false); reload() }} /></div>
        : <p className="rise rise-1 mb-10 max-w-3xl text-[15px] leading-relaxed text-muted">
            {cf.scenario || <button onClick={() => setEditing(true)} className="italic text-faint hover:text-accent">No scenario yet. Write one →</button>}
          </p>}

      <div className="grid grid-cols-1 gap-8 xl:grid-cols-[1fr_360px]">
        <div className="space-y-8">
          <Card className="rise rise-1 p-6">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="display text-2xl">Topology</h2>
              <TopologyLegend boxes={comp.boxes} />
            </div>
            <p className="mb-2 text-xs text-faint">Every team gets an identical copy of this network. Click a box to edit it.</p>
            <Topology boxes={comp.boxes} onSelect={(b) => navigate(`${base}/boxes/${b.name}`)} />
          </Card>
          <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
            <StatCard icon="boxes" n={comp.boxes.length} label="Boxes" to={`${base}/boxes`} delay="rise-1" />
            <StatCard icon="service" n={comp.services} label="Services" hint="scored + planted" to={`${base}/boxes`} delay="rise-2" />
            <StatCard icon="shield" n={comp.misconfigs} label="Misconfigs" hint={bareBoxes ? `${bareBoxes} box${bareBoxes === 1 ? ' has' : 'es have'} none` : null} to={`${base}/boxes`} delay="rise-3" />
            <StatCard icon="injects" n={comp.injects} label="Injects" to={`${base}/injects`} delay="rise-4" />
          </div>
        </div>
        <div><DeployPanel comp={comp} /></div>
      </div>
    </div>
  )
}
