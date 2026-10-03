import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { Button, ErrorBanner, Field, Modal, SearchInput, ThemeToggle, inputCls } from '../components/ui'

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
      <form onSubmit={submit} className="space-y-4">
        <ErrorBanner error={err} />
        <Field label="Name"><input className={inputCls} value={form.name} onChange={set('name')} required autoFocus /></Field>
        <Field label="ID" hint="Folder name under competitions/ — letters, digits, '-', '_'">
          <input className={`${inputCls} font-mono`} value={form.id} onChange={set('id')} required pattern="[A-Za-z0-9][A-Za-z0-9._\-]*" placeholder="acme-2027" />
        </Field>
        <Field label="Scenario"><textarea className={inputCls} rows={3} value={form.scenario} onChange={set('scenario')} /></Field>
        <Field label="Difficulty (1–10)"><input type="number" min="1" max="10" className={inputCls} value={form.difficulty} onChange={set('difficulty')} /></Field>
        <div className="flex justify-end gap-3 pt-2">
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
    <div className="mx-auto max-w-6xl px-8 py-12">
      <div className="mb-10 flex items-center justify-between">
        <div>
          <div className="text-[11px] font-bold uppercase tracking-[0.2em] text-faint">tezcatlipoca</div>
          <h1 className="text-4xl font-bold tracking-tight">Comps</h1>
        </div>
        <div className="flex items-center gap-3">
          <ThemeToggle />
          <Button variant="primary" onClick={() => setCreating(true)}>+ New competition</Button>
        </div>
      </div>
      <SearchInput value={q} onChange={setQ} placeholder="Search competitions…" className="mx-auto mb-12 max-w-2xl" />
      <ErrorBanner error={err} />
      {!comps && !err && <p className="text-faint">Loading…</p>}
      <div className="grid grid-cols-1 gap-8 md:grid-cols-2">
        {shown.map((c) => (
          <Link key={c.id} to={`/c/${c.id}`} className="panel block p-7 transition duration-200 hover:-translate-y-1">
            <div className="flex items-start justify-between gap-3">
              <h2 className="text-xl font-semibold">{c.name}</h2>
              {c.difficulty && <span className="chip shrink-0 bg-sunken text-muted">diff {c.difficulty}</span>}
            </div>
            <p className="font-mono text-xs text-faint">{c.id}</p>
            <p className="mt-3 text-sm">
              {c.boxes} box{c.boxes === 1 ? '' : 'es'}, {c.injects} inject{c.injects === 1 ? '' : 's'}
              <span className="text-faint"> · {c.services} services · {c.misconfigs} misconfigs</span>
            </p>
            {c.scenario && <p className="mt-2 line-clamp-2 text-sm text-muted">{c.scenario}</p>}
          </Link>
        ))}
      </div>
      {comps && !shown.length && <p className="text-center text-faint">No competitions match.</p>}
      <NewCompModal open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}
