import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { useComp } from './CompLayout'
import MarkdownEditor from '../components/MarkdownEditor'
import { Button, Card, ErrorBanner, Field, Modal, inputCls } from '../components/ui'
import { Dirty, EmptyState, Icon, fmtOffset, saveKey, useConfirm, useSaveHotkey, useToast } from '../components/kit'

const slugify = (s) => s.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48)
const hours = (m) => `${+(m / 60).toFixed(1)}h`

// open→due is the working window (solid), due→close the late window (soft).
function Bar({ inj, span, tall }) {
  const o = inj.open_offset_min ?? 0, d = inj.due_offset_min ?? o, c = inj.close_offset_min ?? d
  const pct = (m) => `${(m / span) * 100}%`
  return (
    <div className={`relative w-full rounded-full bg-sunken shadow-inset ${tall ? 'h-3' : 'h-1.5'}`}>
      <div className="absolute inset-y-0 rounded-full bg-accent" style={{ left: pct(o), width: `max(${pct(d - o)}, 4px)` }} />
      <div className="absolute inset-y-0 rounded-r-full bg-accent opacity-30" style={{ left: pct(d), width: pct(c - d) }} />
    </div>
  )
}

function Schedule({ list, span, onPick }) {
  // 15/30/60-minute gridlines depending on how long the event runs.
  const step = span <= 90 ? 15 : span <= 240 ? 30 : 60
  const end = Math.ceil(span / step) * step
  const ticks = Array.from({ length: end / step + 1 }, (_, i) => i * step)
  return (
    <Card className="rise p-7">
      <h2 className="display text-3xl">Schedule</h2>
      <p className="mb-7 text-xs text-faint">Time from the event start (T0). Solid runs from open to due; faded is the late window up to close.</p>
      <div className="grid grid-cols-[34%_1fr]">
        <span />
        <div className="relative mb-2 h-4 font-mono text-[10px] text-faint">
          {ticks.map((t) => (
            <span key={t} className="absolute -translate-x-1/2 first:translate-x-0 last:-translate-x-full" style={{ left: `${(t / end) * 100}%` }}>{fmtOffset(t).replace('T+', '')}</span>
          ))}
        </div>
      </div>
      <div className="relative space-y-3.5">
        <div className="pointer-events-none absolute inset-y-0 left-[34%] right-0">
          {ticks.map((t) => <span key={t} className="absolute inset-y-0 w-px bg-line" style={{ left: `${(t / end) * 100}%` }} />)}
        </div>
        {list.map((i) => (
          <button key={i.slug} onClick={() => onPick(i.slug)} className="group relative grid w-full grid-cols-[34%_1fr] items-center text-left">
            <span className="truncate pr-4 text-sm transition group-hover:text-accent">{i.title || i.slug}</span>
            <Bar inj={i} span={end} tall />
          </button>
        ))}
      </div>
    </Card>
  )
}

function InjectEditor({ comp, slug, onChanged }) {
  const [data, setData] = useState(null)
  const [meta, setMeta] = useState({})
  const [body, setBody] = useState('')
  const [err, setErr] = useState(null)
  const [dirty, setDirty] = useState(false)
  const navigate = useNavigate()
  const ask = useConfirm()
  const toast = useToast()

  useEffect(() => {
    setData(null); setErr(null)
    api.inject(comp.id, slug).then((d) => { setData(d); setMeta(d.meta); setBody(d.body); setDirty(false) }).catch(setErr)
  }, [comp.id, slug])

  const o = Number(meta.open_offset_min), d = Number(meta.due_offset_min), c = Number(meta.close_offset_min)
  const bad = !(o <= d && d <= c)
  const save = async () => {
    try { await api.putInject(comp.id, slug, meta, body); setDirty(false); setErr(null); toast('Inject saved'); onChanged() } catch (e) { setErr(e) }
  }
  useSaveHotkey(save, dirty && !bad)
  const remove = async () => {
    const ok = await ask({ title: `Delete “${meta.title || slug}”?`, danger: true, confirm: 'Delete inject',
      body: <>Removes <span className="font-mono">injects/{slug}/</span> and its briefing.</> })
    if (!ok) return
    try { await api.deleteInject(comp.id, slug); toast('Inject deleted'); onChanged(); navigate(`/c/${comp.id}/injects`) } catch (e) { setErr(e) }
  }
  if (!data) return err ? <ErrorBanner error={err} /> : null

  return (
    <div className="space-y-7">
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <div className="rise flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="mb-1 font-mono text-xs text-faint">injects/{slug}/</div>
          <textarea rows={1} className="display block w-full resize-none border-0 bg-transparent text-4xl leading-tight text-fg [field-sizing:content] placeholder:text-faint focus:outline-none" value={meta.title || ''}
            onChange={(e) => { setMeta({ ...meta, title: e.target.value.replace(/\n/g, ' ') }); setDirty(true) }} placeholder="Untitled inject" aria-label="Title" />
        </div>
        <div className="flex shrink-0 items-center gap-3 pt-6">
          <Dirty dirty={dirty} />
          <Button variant="danger" onClick={remove} title="Delete inject" aria-label="Delete inject"><Icon name="trash" /></Button>
          <Button variant="primary" onClick={save} disabled={!dirty || bad} title={`Save (${saveKey})`}>Save</Button>
        </div>
      </div>
      <Card className="rise rise-1 p-5">
        <div className="grid grid-cols-3 gap-4">
          {[['open_offset_min', 'Opens'], ['due_offset_min', 'Due'], ['close_offset_min', 'Closes']].map(([k, label]) => (
            <Field key={k} label={label} hint={<span className="font-mono">{fmtOffset(meta[k])}</span>}>
              <div className="relative">
                <input inputMode="numeric" className={`${inputCls} pr-12 font-mono`} value={meta[k] ?? ''} onChange={(e) => { setMeta({ ...meta, [k]: e.target.value.replace(/\D/g, '') }); setDirty(true) }} />
                <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-faint">min</span>
              </div>
            </Field>
          ))}
        </div>
        <div className="mt-4"><Bar inj={{ open_offset_min: o || 0, due_offset_min: d || 0, close_offset_min: c || 0 }} span={Math.max(c || 1, 1)} tall /></div>
        {bad && <p className="mt-3 text-xs text-danger">Times must run open ≤ due ≤ close.</p>}
      </Card>
      <div className="rise rise-2">
        <MarkdownEditor value={body} onChange={(v) => { setBody(v); setDirty(true) }} placeholder="Write the briefing competitors receive when this inject opens…" />
      </div>
    </div>
  )
}

export default function Injects() {
  const { comp, reload } = useComp()
  const { slug } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ slug: '', title: '' })
  const [slugTouched, setSlugTouched] = useState(false)
  const [err, setErr] = useState(null)

  const list = comp.injectList
  const span = Math.max(60, ...list.map((i) => i.close_offset_min ?? i.due_offset_min ?? 0))
  const nextNum = String(list.length + 1).padStart(2, '0')

  const create = async (e) => {
    e.preventDefault()
    const last = list.at(-1)
    const open = last ? (last.due_offset_min ?? 0) : 0
    try {
      await api.putInject(comp.id, form.slug, { title: form.title, open_offset_min: open, due_offset_min: open + 30, close_offset_min: open + 60 }, `# ${form.title}\n\n`)
      setCreating(false); toast('Inject created'); await reload(); navigate(`/c/${comp.id}/injects/${form.slug}`)
    } catch (e) { setErr(e) }
  }
  const openNew = () => { setForm({ slug: `${nextNum}-`, title: '' }); setSlugTouched(false); setErr(null); setCreating(true) }

  return (
    <div className="flex h-full gap-8">
      <section className="panel rise flex w-80 shrink-0 flex-col">
        <div className="flex items-center justify-between px-6 pt-6 pb-4">
          <button onClick={() => navigate(`/c/${comp.id}/injects`)} className="text-left" title="Show the schedule">
            <h1 className="display text-3xl">Injects</h1>
            <span className="text-xs text-faint">{list.length ? `${list.length} across ${hours(span)}` : 'Timed taskings'}</span>
          </button>
          <Button variant="primary" className="h-9 w-9 !p-0" onClick={openNew} title="New inject" aria-label="New inject"><Icon name="plus" /></Button>
        </div>
        <ul className="flex-1 space-y-3 overflow-auto px-6 pb-6 pt-1">
          {list.length === 0 && <li><EmptyState icon="injects" title="No injects yet" action={<Button onClick={openNew}><Icon name="plus" />New inject</Button>}>Timed written tasks released during the event.</EmptyState></li>}
          {list.map((i) => (
            <li key={i.slug}>
              <button onClick={() => navigate(`/c/${comp.id}/injects/${i.slug}`)}
                className={`tile tile-hover w-full px-4 py-3 text-left ${slug === i.slug ? 'hatched !border-accent' : ''}`}>
                <div className="truncate text-sm font-semibold">{i.title || i.slug}</div>
                <div className="mb-2 mt-0.5 font-mono text-[11px] text-faint">{fmtOffset(i.open_offset_min)} → due {fmtOffset(i.due_offset_min)}</div>
                <Bar inj={i} span={span} />
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className="-my-6 -mr-6 min-w-0 flex-1 overflow-auto py-6 pl-1 pr-6">
        {slug ? <InjectEditor key={slug} comp={comp} slug={slug} onChanged={reload} />
          : list.length ? <Schedule list={list} span={span} onPick={(s) => navigate(`/c/${comp.id}/injects/${s}`)} />
          : <Card className="flex h-full items-center justify-center"><EmptyState icon="clock" title="Your schedule appears here">Create injects and they line up on a timeline from T0.</EmptyState></Card>}
      </section>
      <Modal open={creating} onClose={() => setCreating(false)} title="New inject">
        <form onSubmit={create} className="space-y-4">
          <ErrorBanner error={err} />
          <Field label="Title"><input className={inputCls} value={form.title} required autoFocus placeholder="Incident report: suspicious logins"
            onChange={(e) => setForm({ title: e.target.value, slug: slugTouched ? form.slug : `${nextNum}-${slugify(e.target.value)}` })} /></Field>
          <Field label="Folder" hint="Injects are ordered by folder name; the number keeps them in sequence.">
            <input className={`${inputCls} font-mono`} value={form.slug} required pattern="[A-Za-z0-9][A-Za-z0-9._\-]*"
              onChange={(e) => { setSlugTouched(true); setForm({ ...form, slug: e.target.value }) }} />
          </Field>
          <p className="text-xs text-faint">It opens when the previous inject is due, with a 30-minute window. Adjust after creating.</p>
          <div className="flex justify-end gap-3"><Button type="button" onClick={() => setCreating(false)}>Cancel</Button><Button variant="primary"><Icon name="plus" />Create</Button></div>
        </form>
      </Modal>
    </div>
  )
}
