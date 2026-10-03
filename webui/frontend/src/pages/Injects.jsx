import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { useComp } from './CompLayout'
import MarkdownEditor from '../components/MarkdownEditor'
import { Button, ErrorBanner, Field, Modal, inputCls } from '../components/ui'

const fmt = (m) => (m == null ? '—' : m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m` : `${m}m`)

function InjectEditor({ comp, slug, onChanged }) {
  const [data, setData] = useState(null)
  const [meta, setMeta] = useState({})
  const [body, setBody] = useState('')
  const [err, setErr] = useState(null)
  const [saved, setSaved] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    setData(null); setErr(null)
    api.inject(comp.id, slug).then((d) => { setData(d); setMeta(d.meta); setBody(d.body); setSaved(true) }).catch(setErr)
  }, [comp.id, slug])

  const setM = (k) => (e) => { setMeta({ ...meta, [k]: e.target.value }); setSaved(false) }
  const save = async () => {
    try { await api.putInject(comp.id, slug, meta, body); setSaved(true); setErr(null); onChanged() } catch (e) { setErr(e) }
  }
  const remove = async () => {
    if (!confirm(`Delete inject ${slug}?`)) return
    try { await api.deleteInject(comp.id, slug); onChanged(); navigate(`/c/${comp.id}/injects`) } catch (e) { setErr(e) }
  }
  if (!data) return <div className="p-6"><ErrorBanner error={err} /></div>

  return (
    <div className="space-y-4 p-6">
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <div className="flex items-center justify-between gap-4">
        <input className="flex-1 border-0 bg-transparent text-2xl font-bold focus:outline-none" value={meta.title || ''} onChange={setM('title')} placeholder="Inject title" />
        <div className="flex gap-2">
          <Button variant="danger" onClick={remove}>Delete</Button>
          <Button variant="primary" onClick={save} disabled={saved}>{saved ? 'Saved' : 'Save'}</Button>
        </div>
      </div>
      <div className="grid max-w-xl grid-cols-3 gap-3">
        <Field label="Opens (min from T0)"><input type="number" className={inputCls} value={meta.open_offset_min ?? ''} onChange={setM('open_offset_min')} /></Field>
        <Field label="Due (min)"><input type="number" className={inputCls} value={meta.due_offset_min ?? ''} onChange={setM('due_offset_min')} /></Field>
        <Field label="Closes (min)"><input type="number" className={inputCls} value={meta.close_offset_min ?? ''} onChange={setM('close_offset_min')} /></Field>
      </div>
      <MarkdownEditor value={body} onChange={(v) => { setBody(v); setSaved(false) }} />
    </div>
  )
}

export default function Injects() {
  const { comp, reload } = useComp()
  const { slug } = useParams()
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ slug: '', title: '' })
  const [err, setErr] = useState(null)

  const create = async (e) => {
    e.preventDefault()
    const last = comp.injectList.at(-1)
    const open = last ? (last.close_offset_min ?? 0) : 0
    try {
      await api.putInject(comp.id, form.slug, { title: form.title, open_offset_min: open, due_offset_min: open + 30, close_offset_min: open + 60 }, `# ${form.title}\n\n`)
      setCreating(false); await reload(); navigate(`/c/${comp.id}/injects/${form.slug}`)
    } catch (e) { setErr(e) }
  }
  const nextNum = String(comp.injectList.length + 1).padStart(2, '0')

  return (
    <div className="flex h-full">
      <section className="flex w-72 shrink-0 flex-col border-r border-slate-200 bg-slate-50">
        <div className="flex items-center justify-between p-4">
          <h1 className="text-lg font-semibold">Injects</h1>
          <Button onClick={() => { setForm({ slug: `${nextNum}-`, title: '' }); setErr(null); setCreating(true) }}>+ New</Button>
        </div>
        <ul className="flex-1 space-y-1 overflow-auto px-2">
          {comp.injectList.length === 0 && <li className="p-3 text-sm text-slate-400">No injects yet.</li>}
          {comp.injectList.map((i) => (
            <li key={i.slug}>
              <button onClick={() => navigate(`/c/${comp.id}/injects/${i.slug}`)}
                className={`w-full rounded-lg px-3 py-2 text-left ${slug === i.slug ? 'bg-white shadow-sm ring-1 ring-indigo-300' : 'hover:bg-white'}`}>
                <div className="truncate text-sm font-medium">{i.title || i.slug}</div>
                <div className="text-xs text-slate-400">opens {fmt(i.open_offset_min)} · due {fmt(i.due_offset_min)} · closes {fmt(i.close_offset_min)}</div>
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className="min-w-0 flex-1 overflow-auto">
        {slug ? <InjectEditor key={slug} comp={comp} slug={slug} onChanged={reload} />
          : <div className="flex h-full items-center justify-center text-sm text-slate-400">Pick an inject or create one.</div>}
      </section>
      <Modal open={creating} onClose={() => setCreating(false)} title="New inject">
        <form onSubmit={create} className="space-y-3">
          <ErrorBanner error={err} />
          <Field label="Title"><input className={inputCls} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} required autoFocus /></Field>
          <Field label="Folder (slug)" hint="injects/<slug>/ — ordered by name"><input className={`${inputCls} font-mono`} value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} required pattern="[A-Za-z0-9][A-Za-z0-9._\-]*" /></Field>
          <div className="flex justify-end gap-2"><Button type="button" onClick={() => setCreating(false)}>Cancel</Button><Button variant="primary">Create</Button></div>
        </form>
      </Modal>
    </div>
  )
}
