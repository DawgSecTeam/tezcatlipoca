import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { Button, ErrorBanner, Field, Modal, SearchInput, ThemeToggle, inputCls } from '../components/ui'
import { DifficultyMeter, EmptyState, Icon, Logo, timeAgo, useSlashFocus } from '../components/kit'

const slugify = (s) => s.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64)

function NewCompModal({ open, onClose }) {
  const [form, setForm] = useState({ id: '', name: '', scenario: '', difficulty: 5 })
  const [idTouched, setIdTouched] = useState(false)
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
        <Field label="Name">
          <input className={inputCls} value={form.name} required autoFocus placeholder="ACME Qualifier 2027"
            onChange={(e) => setForm({ ...form, name: e.target.value, id: idTouched ? form.id : slugify(e.target.value) })} />
        </Field>
        <Field label="Folder" hint={<>Created as <span className="font-mono">competitions/{form.id || '…'}/</span></>}>
          <input className={`${inputCls} font-mono`} value={form.id} required pattern="[A-Za-z0-9][A-Za-z0-9._\-]*" placeholder="acme-quals-2027"
            onChange={(e) => { setIdTouched(true); setForm({ ...form, id: e.target.value }) }} />
        </Field>
        <Field label="Scenario" hint="The story competitors are told. Shown on the overview and in the packet.">
          <textarea className={inputCls} rows={3} value={form.scenario} onChange={set('scenario')} placeholder="Defend ACME's e-commerce stack while…" />
        </Field>
        <Field label={`Difficulty · ${form.difficulty}/10`}>
          <input type="range" min="1" max="10" value={form.difficulty} onChange={set('difficulty')} className="w-full accent-[var(--accent)]" />
        </Field>
        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button variant="primary"><Icon name="plus" />Create competition</Button>
        </div>
      </form>
    </Modal>
  )
}

function Stat({ icon, n, label }) {
  return (
    <span className="flex items-center gap-1.5" title={`${n} ${label}`}>
      <Icon name={icon} className="h-3.5 w-3.5 text-faint" />
      <span className="font-semibold tabular-nums">{n}</span>
      <span className="text-faint">{label}</span>
    </span>
  )
}

export default function Comps() {
  const [comps, setComps] = useState(null)
  const [err, setErr] = useState(null)
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('recent')
  const [creating, setCreating] = useState(false)
  const searchRef = useRef()
  useSlashFocus(searchRef)
  useEffect(() => { api.comps().then(setComps).catch(setErr) }, [])

  const needle = q.toLowerCase()
  const shown = (comps || [])
    .filter((c) => `${c.name} ${c.id} ${c.scenario}`.toLowerCase().includes(needle))
    .sort((a, b) => (sort === 'recent' ? b.modified - a.modified : a.name.localeCompare(b.name)))

  return (
    <div className="mx-auto max-w-6xl px-8 py-10">
      <div className="rise mb-12 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <Logo className="h-7 w-7" />
          <span className="text-sm font-semibold tracking-tight">tezcatlipoca</span>
        </div>
        <div className="flex items-center gap-3">
          <ThemeToggle />
          <Button variant="primary" onClick={() => setCreating(true)}><Icon name="plus" />New competition</Button>
        </div>
      </div>

      <div className="rise rise-1 mb-10 text-center">
        <h1 className="display text-6xl">Competitions</h1>
        <p className="mt-3 text-muted">
          {comps ? `${comps.length} ranges` : '…'} · author boxes, services and misconfigs, then deploy to Proxmox
        </p>
      </div>

      <div className="rise rise-2 mx-auto mb-12 flex max-w-2xl items-center gap-3">
        <SearchInput inputRef={searchRef} value={q} onChange={setQ} placeholder="Search by name, folder or scenario" hotkey="/" className="flex-1" />
        <div className="flex shrink-0 rounded-full bg-sunken p-1 shadow-inset">
          {[['recent', 'Recent'], ['name', 'A–Z']].map(([k, l]) => (
            <button key={k} onClick={() => setSort(k)} className={`rounded-full px-3 py-1 text-xs font-semibold transition ${sort === k ? 'bg-bg text-fg shadow-tile' : 'text-muted hover:text-fg'}`}>{l}</button>
          ))}
        </div>
      </div>

      <ErrorBanner error={err} />
      {!comps && !err && <div className="grid grid-cols-1 gap-8 md:grid-cols-2">{[0, 1, 2, 3].map((i) => <div key={i} className="panel h-48 animate-pulse opacity-60" />)}</div>}

      <div className="grid grid-cols-1 gap-8 md:grid-cols-2">
        {shown.map((c, i) => (
          <Link key={c.id} to={`/c/${c.id}`} className={`panel rise group flex flex-col p-7 transition duration-200 hover:-translate-y-1 ${i < 4 ? `rise-${i + 1}` : ''}`}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="display truncate text-2xl">{c.name}</h2>
                <p className="mt-0.5 truncate text-xs text-faint"><span className="font-mono">{c.id}</span> · edited {timeAgo(c.modified)}</p>
              </div>
              <DifficultyMeter value={c.difficulty} />
            </div>
            {c.scenario
              ? <p className="mt-4 line-clamp-2 text-sm leading-relaxed text-muted">{c.scenario}</p>
              : <p className="mt-4 text-sm italic text-faint">No scenario written yet.</p>}
            <div className="mt-auto flex flex-wrap items-center gap-x-5 gap-y-2 pt-6 text-xs">
              <Stat icon="box" n={c.boxes} label={c.boxes === 1 ? 'box' : 'boxes'} />
              <Stat icon="service" n={c.services} label="services" />
              <Stat icon="shield" n={c.misconfigs} label="misconfigs" />
              <Stat icon="injects" n={c.injects} label="injects" />
              <Icon name="chevron" className="ml-auto h-4 w-4 text-faint transition group-hover:translate-x-0.5 group-hover:text-accent" />
            </div>
          </Link>
        ))}
      </div>
      {comps && !shown.length && (
        <EmptyState icon="search" title={q ? `Nothing matches “${q}”` : 'No competitions yet'}
          action={q ? <Button onClick={() => setQ('')}>Clear search</Button> : <Button variant="primary" onClick={() => setCreating(true)}><Icon name="plus" />New competition</Button>}>
          {q ? 'Try a folder name or a word from the scenario.' : 'Create one, or compile a packet with compile-packet.py.'}
        </EmptyState>
      )}
      <NewCompModal open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}
