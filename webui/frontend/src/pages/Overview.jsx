import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useComp } from './CompLayout'
import Topology from '../components/Topology'
import DeployPanel from '../components/DeployPanel'
import { Button, Card, ErrorBanner, Field, inputCls } from '../components/ui'

// The Compfile keys an author normally sets; anything else in the file is kept and shown below.
const KNOWN = [
  ['name', 'Name'], ['scenario', 'Scenario'], ['difficulty', 'Difficulty'],
  ['domain_prefix', 'Domain prefix'], ['domain_suffix', 'Domain suffix'],
]

function InfoEditor({ comp, onSaved }) {
  const [items, setItems] = useState(comp.compfile)
  const [editing, setEditing] = useState(false)
  const [err, setErr] = useState(null)
  const get = (k) => items.find(([key]) => key === k)?.[1] ?? ''
  const set = (k, v) => setItems(items.some(([key]) => key === k) ? items.map(([key, val]) => [key, key === k ? v : val]) : [...items, [k, v]])
  const extra = items.filter(([k]) => !KNOWN.some(([kk]) => kk === k))

  const save = async () => {
    try { await api.putCompfile(comp.id, items.filter(([, v]) => v !== '')); setEditing(false); onSaved() } catch (e) { setErr(e) }
  }

  if (!editing) {
    const domain = get('domain_prefix') || get('domain_suffix') ? `${get('domain_prefix')}<team>${get('domain_suffix')}` : 'team<id>.local'
    return (
      <div>
        <div className="flex items-start justify-between gap-4">
          <p className="max-w-3xl text-[15px] leading-relaxed text-muted">{get('scenario') || <span className="italic text-faint">No scenario yet.</span>}</p>
          <Button variant="ghost" onClick={() => { setItems(comp.compfile); setEditing(true) }}>Edit info</Button>
        </div>
        <div className="mt-4 flex flex-wrap gap-2 text-sm">
          <span className="chip bg-sunken px-3 py-1 text-muted">difficulty <b className="ml-1 text-fg">{get('difficulty') || '—'}/10</b></span>
          <span className="chip bg-sunken px-3 py-1 font-mono text-muted">{domain}</span>
          {extra.map(([k, v]) => <span key={k} className="chip bg-sunken px-3 py-1 text-muted">{k} <span className="ml-1 font-mono text-fg">{v}</span></span>)}
        </div>
      </div>
    )
  }
  return (
    <Card className="max-w-3xl space-y-4 p-6">
      <ErrorBanner error={err} />
      {KNOWN.map(([k, label]) => (
        <Field key={k} label={label}>
          {k === 'scenario'
            ? <textarea rows={3} className={inputCls} value={get(k)} onChange={(e) => set(k, e.target.value)} />
            : <input className={inputCls} value={get(k)} onChange={(e) => set(k, e.target.value)} type={k === 'difficulty' ? 'number' : 'text'} />}
        </Field>
      ))}
      {extra.length > 0 && <p className="text-xs text-faint">Other Compfile keys ({extra.map(([k]) => k).join(', ')}) are kept as-is.</p>}
      <div className="flex justify-end gap-3">
        <Button onClick={() => setEditing(false)}>Cancel</Button>
        <Button variant="primary" onClick={save}>Save</Button>
      </div>
    </Card>
  )
}

export default function Overview() {
  const { comp, reload } = useComp()
  const navigate = useNavigate()
  return (
    <div className="pb-6">
      <h1 className="text-4xl font-bold tracking-tight">{comp.name}</h1>
      <p className="mb-5 font-mono text-xs text-faint">competitions/{comp.id}/</p>
      <div className="mb-10"><InfoEditor key={comp.compfile.join('|')} comp={comp} onSaved={reload} /></div>
      <div className="grid grid-cols-1 gap-8 xl:grid-cols-[1fr_340px]">
        <div className="space-y-8">
          <Card className="p-6">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-xl font-semibold">Topology</h2>
              <span className="text-xs text-faint">per team · click a box to edit</span>
            </div>
            <Topology boxes={comp.boxes} onSelect={(b) => navigate(`/c/${comp.id}/boxes/${b.name}`)} />
          </Card>
          <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
            {[['Boxes', comp.boxes.length, 'boxes'], ['Services', comp.services, 'boxes'], ['Misconfigs', comp.misconfigs, 'boxes'], ['Injects', comp.injects, 'injects']].map(([label, n, to]) => (
              <button key={label} className="panel p-5 text-left transition duration-200 hover:-translate-y-1" onClick={() => navigate(`/c/${comp.id}/${to}`)}>
                <div className="text-3xl font-semibold">{n}</div>
                <div className="text-sm text-muted">{label}</div>
              </button>
            ))}
          </div>
        </div>
        <div><DeployPanel comp={comp} /></div>
      </div>
    </div>
  )
}
