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
          <p className="max-w-2xl text-slate-600">{get('scenario') || <span className="italic text-slate-400">No scenario yet.</span>}</p>
          <Button variant="ghost" onClick={() => { setItems(comp.compfile); setEditing(true) }}>Edit info</Button>
        </div>
        <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
          <div><dt className="inline text-slate-400">Difficulty </dt><dd className="inline">{get('difficulty') || '—'}/10</dd></div>
          <div><dt className="inline text-slate-400">Domain </dt><dd className="inline font-mono">{domain}</dd></div>
          {extra.map(([k, v]) => <div key={k}><dt className="inline text-slate-400">{k} </dt><dd className="inline font-mono">{v}</dd></div>)}
        </dl>
      </div>
    )
  }
  return (
    <Card className="space-y-3 p-4">
      <ErrorBanner error={err} />
      {KNOWN.map(([k, label]) => (
        <Field key={k} label={label}>
          {k === 'scenario'
            ? <textarea rows={3} className={inputCls} value={get(k)} onChange={(e) => set(k, e.target.value)} />
            : <input className={inputCls} value={get(k)} onChange={(e) => set(k, e.target.value)} type={k === 'difficulty' ? 'number' : 'text'} />}
        </Field>
      ))}
      {extra.length > 0 && <p className="text-xs text-slate-400">Other Compfile keys ({extra.map(([k]) => k).join(', ')}) are kept as-is.</p>}
      <div className="flex justify-end gap-2">
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
    <div className="p-8">
      <h1 className="text-3xl font-bold tracking-tight">{comp.name}</h1>
      <p className="mb-4 font-mono text-xs text-slate-400">competitions/{comp.id}/</p>
      <div className="mb-6"><InfoEditor key={comp.compfile.join('|')} comp={comp} onSaved={reload} /></div>
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_320px]">
        <div className="space-y-6">
          <Card className="p-4">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-lg font-semibold">Topology</h2>
              <span className="text-xs text-slate-400">per team · click a box to edit</span>
            </div>
            <Topology boxes={comp.boxes} onSelect={(b) => navigate(`/c/${comp.id}/boxes/${b.name}`)} />
          </Card>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            {[['Boxes', comp.boxes.length, 'boxes'], ['Services', comp.services, 'boxes'], ['Misconfigs', comp.misconfigs, 'boxes'], ['Injects', comp.injects, 'injects']].map(([label, n, to]) => (
              <Card key={label} className="cursor-pointer p-4 hover:border-indigo-300" onClick={() => navigate(`/c/${comp.id}/${to}`)}>
                <div className="text-2xl font-semibold">{n}</div>
                <div className="text-sm text-slate-500">{label}</div>
              </Card>
            ))}
          </div>
        </div>
        <DeployPanel comp={comp} />
      </div>
    </div>
  )
}
