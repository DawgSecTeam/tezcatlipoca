import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { Button, Card, ErrorBanner, Field, Modal, SearchInput, inputCls } from '../components/ui'

function NewCompModal({ open, onClose }) {
  const [form, setForm] = useState({ id: '', name: '', scenario: '', difficulty: 5 })
  const [err, setErr] = useState(null)
  const navigate = useNavigate()
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const submit = async (e) => {
    e.preventDefault()
    try {
      const c = await api.createComp({ ...form, difficulty: Number(form.difficulty) })
      navigate(`/c/${c.id}`)
    } catch (e) { setErr(e) }
  }
  return (
    <Modal open={open} onClose={onClose} title="New competition">
      <form onSubmit={submit} className="space-y-3">
        <ErrorBanner error={err} />
        <Field label="Name"><input className={inputCls} value={form.name} onChange={set('name')} required autoFocus /></Field>
        <Field label="ID" hint="Folder name under competitions/ — letters, digits, '-', '_'">
          <input className={`${inputCls} font-mono`} value={form.id} onChange={set('id')} required pattern="[A-Za-z0-9][A-Za-z0-9._\-]*" placeholder="acme-2027" />
        </Field>
        <Field label="Scenario"><textarea className={inputCls} rows={3} value={form.scenario} onChange={set('scenario')} /></Field>
        <Field label="Difficulty (1–10)"><input type="number" min="1" max="10" className={inputCls} value={form.difficulty} onChange={set('difficulty')} /></Field>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary">Create</Button>
        </div>
      </form>
    </Modal>
  )
}

export default function Comps() {
  const [comps, setComps] = useState(null)
  const [err, setErr] = useState(null)
  const [q, setQ] = useState('')
  const [creating, setCreating] = useState(false)
  useEffect(() => { api.comps().then(setComps).catch(setErr) }, [])

  const needle = q.toLowerCase()
  const shown = (comps || [])
    .filter((c) => `${c.name} ${c.id} ${c.scenario}`.toLowerCase().includes(needle))
    .sort((a, b) => b.modified - a.modified)

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-3xl font-bold tracking-tight">Comps</h1>
        <Button variant="primary" onClick={() => setCreating(true)}>+ New competition</Button>
      </div>
      <SearchInput value={q} onChange={setQ} placeholder="Search competitions…" className="mb-6" />
      <ErrorBanner error={err} />
      {!comps && !err && <p className="text-slate-400">Loading…</p>}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {shown.map((c) => (
          <Link key={c.id} to={`/c/${c.id}`}>
            <Card className="h-full p-5 transition hover:border-indigo-300 hover:shadow-md">
              <div className="flex items-start justify-between gap-2">
                <h2 className="text-lg font-semibold">{c.name}</h2>
                {c.difficulty && <span className="shrink-0 rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">diff {c.difficulty}</span>}
              </div>
              <p className="font-mono text-xs text-slate-400">{c.id}</p>
              <p className="mt-2 text-sm text-slate-700">
                {c.boxes} box{c.boxes === 1 ? '' : 'es'}, {c.injects} inject{c.injects === 1 ? '' : 's'}
                <span className="text-slate-400"> · {c.services} services · {c.misconfigs} misconfigs</span>
              </p>
              {c.scenario && <p className="mt-2 line-clamp-2 text-sm text-slate-500">{c.scenario}</p>}
            </Card>
          </Link>
        ))}
      </div>
      {comps && !shown.length && <p className="text-slate-400">No competitions match.</p>}
      <NewCompModal open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}
