import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, asPin, platformOf } from '../api'
import { useComp } from './CompLayout'
import { Button, ErrorBanner, Field, PlatformBadge, SearchInput, Spinner, inputCls } from '../components/ui'

const LABEL = { services: 'Services', misconfigs: 'Misconfigs' }

// ── catalog row details (shared by the preview pane and the selected-pin panel) ────────────

function CatalogInfo({ entry, compact }) {
  const [showScript, setShowScript] = useState(false)
  if (!entry) return null
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <PlatformBadge platform={entry.platform} />
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700">{entry.category}</span>
        <span className="text-xs text-slate-400">{entry.type} · run as {entry.run_as}</span>
      </div>
      {entry.broken && <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-800"><b>Known broken:</b> {entry.broken}</div>}
      <p className="text-slate-700">{entry.description || <span className="italic text-slate-400">No description in the catalog.</span>}</p>
      {entry.depends_on?.length > 0 && (
        <p className="text-xs text-slate-500">Depends on: {entry.depends_on.map((d) => (typeof d === 'string' ? d : d.name || JSON.stringify(d))).join(', ')}</p>
      )}
      {entry.required_vars?.length > 0 && (
        <p className="text-xs text-slate-500">Required vars: {entry.required_vars.map((v) => (
          <code key={v} className="mr-1 rounded bg-amber-50 px-1 text-amber-800">{v}{entry.identity_vars?.includes(v) ? ' (auto)' : ''}</code>
        ))}</p>
      )}
      {!compact && entry.script && (
        <div>
          <button className="text-xs text-indigo-600 hover:underline" onClick={() => setShowScript(!showScript)}>{showScript ? 'Hide' : 'Show'} script</button>
          {showScript && <pre className="mt-1 max-h-72 overflow-auto rounded-md bg-slate-900 p-2 text-[11px] text-slate-100">{entry.script}</pre>}
        </div>
      )}
    </div>
  )
}

// ── right panel when a pin is selected: description + env var editor ──────────────────────

function VarsEditor({ rows, setRows, required, identity }) {
  const update = (i, k, v) => setRows(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)))
  return (
    <div className="space-y-2">
      {rows.map((r, i) => {
        const missing = required.includes(r.key) && !identity.includes(r.key) && !r.value
        return (
          <div key={i} className="flex gap-2">
            <input className={`${inputCls} !w-2/5 shrink-0 font-mono uppercase`} value={r.key} placeholder="VAR" onChange={(e) => update(i, 'key', e.target.value.toUpperCase())} />
            <input className={`${inputCls} min-w-0 !flex-1 font-mono ${missing ? 'border-amber-400 bg-amber-50' : ''}`} value={r.value}
              placeholder={identity.includes(r.key) ? 'auto-filled (box IP)' : 'value'} onChange={(e) => update(i, 'value', e.target.value)} />
            <button onClick={() => setRows(rows.filter((_, j) => j !== i))} className="px-1 text-slate-400 hover:text-red-600">✕</button>
          </div>
        )
      })}
      <button onClick={() => setRows([...rows, { key: '', value: '' }])} className="text-sm text-indigo-600 hover:underline">+ Add variable</button>
    </div>
  )
}

function PinDetail({ kind, pin, onSave, onRemove }) {
  const [entry, setEntry] = useState(null)
  const [entryErr, setEntryErr] = useState(null)
  const [draft, setDraft] = useState(pin)
  const [rows, setRows] = useState([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setDraft(pin)
    setEntry(null); setEntryErr(null)
    api.catalogEntry(pin.name).then(setEntry).catch(setEntryErr)
  }, [pin])
  useEffect(() => {
    const vars = pin.vars || {}
    const req = (entry?.required_vars || []).filter((v) => !(v in vars) && !entry.identity_vars?.includes(v))
    setRows([...Object.entries(vars).map(([key, value]) => ({ key, value })), ...req.map((key) => ({ key, value: '' }))])
  }, [pin, entry])

  const set = (k) => (e) => setDraft({ ...draft, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })
  const save = async () => {
    setSaving(true)
    const vars = Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value]))
    await onSave({ ...draft, vars })
    setSaving(false)
  }

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-200 p-4">
        <div className="text-xs uppercase tracking-wide text-slate-400">{kind === 'services' ? 'Service' : 'Misconfig'}</div>
        <h2 className="break-all font-mono text-lg font-semibold">{pin.name}</h2>
      </div>
      <div className="flex-1 space-y-5 overflow-auto p-4">
        {entry ? <CatalogInfo entry={entry} /> : entryErr
          ? <p className="text-xs text-slate-400">{/^score\//.test(pin.name) ? 'Native service scored without a plant (score/<slice> pin).' : `Catalog: ${entryErr.message}`}</p>
          : <Spinner />}

        {kind === 'services' && (
          <div className="space-y-3 border-t border-slate-200 pt-4">
            <h3 className="text-sm font-semibold">Scoring</h3>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Display" hint="Check name: <box>-<display>"><input className={inputCls} value={draft.display || ''} onChange={set('display')} /></Field>
              <Field label="Port"><input type="number" className={inputCls} value={draft.port || ''} onChange={set('port')} /></Field>
              <Field label="Check" hint="e.g. Tcp; blank = service default"><input className={inputCls} value={draft.check || ''} onChange={set('check')} /></Field>
            </div>
            <div className="flex gap-4 text-sm">
              <label className="flex items-center gap-2"><input type="checkbox" checked={!!draft.score_only} onChange={set('score_only')} /> Score only</label>
              <label className="flex items-center gap-2"><input type="checkbox" checked={!!draft.plant_only} onChange={set('plant_only')} /> Plant only</label>
            </div>
          </div>
        )}

        <div className="space-y-3 border-t border-slate-200 pt-4">
          <h3 className="text-sm font-semibold">Nakon vars</h3>
          <VarsEditor rows={rows} setRows={setRows} required={entry?.required_vars || []} identity={entry?.identity_vars || []} />
        </div>
      </div>
      <div className="flex justify-between border-t border-slate-200 p-3">
        <Button variant="danger" onClick={onRemove}>Remove</Button>
        <Button variant="primary" disabled={saving} onClick={save}>{saving ? 'Saving…' : 'Save'}</Button>
      </div>
    </div>
  )
}

// ── right panel in add mode: catalog search; picking one splits it 50/50 with a preview ────

function CatalogPicker({ kind, platform, existing, onAdd, onClose }) {
  const [q, setQ] = useState('')
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)
  const [picked, setPicked] = useState(null)

  useEffect(() => {
    setRows(null); setErr(null)
    api.catalog({ platform, kind }).then(setRows).catch(setErr)
  }, [platform, kind])

  const shown = useMemo(() => {
    const n = q.toLowerCase()
    return (rows || []).filter((r) => `${r.name} ${r.description || ''}`.toLowerCase().includes(n))
  }, [rows, q])

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 p-3">
        <div>
          <h2 className="font-semibold">Add {kind === 'services' ? 'service' : 'misconfig'}</h2>
          <p className="text-xs text-slate-400">vulndb catalog · <PlatformBadge platform={platform} /></p>
        </div>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-700">✕</button>
      </div>
      <div className={`flex min-h-0 flex-col ${picked ? 'h-1/2 border-b border-slate-200' : 'flex-1'}`}>
        <div className="p-3"><SearchInput value={q} onChange={setQ} autoFocus placeholder="Search the catalog…" /></div>
        <div className="min-h-0 flex-1 overflow-auto px-3 pb-3">
          <ErrorBanner error={err} />
          {!rows && !err && <div className="flex justify-center p-4"><Spinner /></div>}
          {rows && !shown.length && <p className="p-2 text-sm text-slate-400">Nothing matches.</p>}
          <ul className="space-y-1">
            {shown.map((r) => (
              <li key={r.name}>
                <button onClick={() => setPicked(r)} className={`w-full rounded-lg border px-3 py-2 text-left ${picked?.name === r.name ? 'border-indigo-400 bg-indigo-50' : 'border-slate-200 hover:bg-slate-50'}`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-sm">{r.name}</span>
                    {existing.has(r.name) && <span className="shrink-0 text-[11px] text-emerald-600">on box</span>}
                    {r.broken && <span className="shrink-0 text-[11px] text-red-600">broken</span>}
                  </div>
                  {r.description && <p className="line-clamp-1 text-xs text-slate-500">{r.description}</p>}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
      {picked && (
        <div className="flex h-1/2 min-h-0 flex-col">
          <div className="min-h-0 flex-1 overflow-auto p-4">
            <h3 className="mb-2 break-all font-mono font-semibold">{picked.name}</h3>
            <CatalogInfo entry={picked} />
          </div>
          <div className="flex justify-end border-t border-slate-200 p-3">
            <Button variant="primary" onClick={() => onAdd(picked)}>Add config</Button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── page ──────────────────────────────────────────────────────────────────────────────────

export default function PinPage({ kind }) {
  const { comp, reload } = useComp()
  const { box: boxName } = useParams()
  const [params, setParams] = useSearchParams()
  const [q, setQ] = useState('')
  const [err, setErr] = useState(null)

  const box = comp.boxes.find((b) => b.name === boxName)
  const file = kind === 'services' ? comp.box_services : comp.box_vulns
  const raw = useMemo(() => (box ? file[box.name] || [] : []), [file, box])
  const pins = useMemo(() => raw.map(asPin), [raw])
  if (!box) return <div className="p-8">No box <code>{boxName}</code>.</div>

  const sel = params.has('sel') ? Number(params.get('sel')) : null
  const adding = params.get('add') === '1'
  const platform = platformOf(box.template)
  const shown = pins.map((p, i) => [p, i]).filter(([p]) => p.name.toLowerCase().includes(q.toLowerCase()))

  const save = async (next, selectIdx) => {
    try {
      await api.putPins(comp.id, box.name, kind, next)
      await reload()
      setParams(selectIdx == null ? {} : { sel: String(selectIdx) })
    } catch (e) { setErr(e) }
  }
  // Untouched entries go back exactly as they were read (bare names stay bare names).
  const replaceAt = (i, pin) => raw.map((p, j) => (j === i ? pin : p))

  return (
    <div className="flex h-full">
      <section className="flex w-96 shrink-0 flex-col border-r border-slate-200 bg-slate-50">
        <div className="p-4 pb-2">
          <Link to={`/c/${comp.id}/boxes/${box.name}?tab=${kind}`} className="text-lg font-semibold hover:text-indigo-600">‹ {LABEL[kind]}</Link>
          <p className="text-xs text-slate-400">{box.name} · <span className="font-mono">{box.template}</span></p>
        </div>
        <div className="px-4 pb-3"><SearchInput value={q} onChange={setQ} placeholder={`Search ${kind} on ${box.name}…`} /></div>
        <ErrorBanner error={err} onClose={() => setErr(null)} />
        <ul className="min-h-0 flex-1 space-y-2 overflow-auto px-4">
          {shown.length === 0 && <li className="py-6 text-center text-sm text-slate-400">{pins.length ? 'No matches.' : 'None enabled yet.'}</li>}
          {shown.map(([p, i]) => (
            <li key={i}>
              <button onClick={() => setParams({ sel: String(i) })}
                className={`w-full rounded-lg border px-3 py-2 text-left ${sel === i ? 'border-indigo-500 bg-[repeating-linear-gradient(135deg,#e0e7ff_0,#e0e7ff_6px,#eef2ff_6px,#eef2ff_12px)]' : 'border-slate-200 bg-white hover:border-slate-300'}`}>
                <div className="truncate font-mono text-sm">{p.name}</div>
                <div className="text-xs text-slate-400">
                  {[p.display && `${p.display}${p.port ? `:${p.port}` : ''}`, p.vars && `${Object.keys(p.vars).length} vars`].filter(Boolean).join(' · ') || ' '}
                </div>
              </button>
            </li>
          ))}
        </ul>
        <div className="p-4">
          <Button className="w-full" variant={adding ? 'primary' : 'default'} onClick={() => setParams({ add: '1' })}>Add +</Button>
        </div>
      </section>

      <section className="min-w-0 flex-1 bg-white">
        {adding ? (
          <CatalogPicker kind={kind} platform={platform} existing={new Set(pins.map((p) => p.name))}
            onClose={() => setParams({})}
            onAdd={(row) => save([...raw, row.name], raw.length)} />
        ) : sel != null && pins[sel] ? (
          <PinDetail key={`${sel}-${pins[sel].name}`} kind={kind} pin={pins[sel]}
            onSave={(pin) => save(replaceAt(sel, pin), sel)}
            onRemove={() => save(raw.filter((_, j) => j !== sel), null)} />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-400">Select a {kind === 'services' ? 'service' : 'misconfig'} to edit its vars, or Add + to browse the catalog.</div>
        )}
      </section>
    </div>
  )
}
