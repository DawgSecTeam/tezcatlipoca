import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, asPin, platformOf } from '../api'
import { useComp } from './CompLayout'
import { Button, ErrorBanner, Field, PlatformBadge, SearchInput, Spinner, inputCls } from '../components/ui'
import { Dirty, EmptyState, Icon, saveKey, useConfirm, useSaveHotkey, useSlashFocus, useToast } from '../components/kit'

const META = {
  services: { label: 'Services', one: 'service', icon: 'service' },
  misconfigs: { label: 'Misconfigs', one: 'misconfig', icon: 'shield' },
}

// Vars a pin still has to supply: required by the script, not auto-filled, not set.
const missingVars = (pin, row) => (row?.required_vars || [])
  .filter((v) => !(row.identity_vars || []).includes(v) && !(pin.vars || {})[v])

// ── catalog row details (preview pane + selected-pin panel) ───────────────────────────────

function CatalogInfo({ entry }) {
  const [showScript, setShowScript] = useState(false)
  if (!entry) return null
  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <PlatformBadge platform={entry.platform} />
        <span className="chip bg-sunken text-muted">{entry.category}</span>
        <span className="text-xs text-faint">{entry.type} · runs as {entry.run_as}</span>
      </div>
      {entry.broken && <div className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-xs text-danger"><Icon name="warn" className="h-4 w-4 shrink-0" /><span><b>Known broken.</b> {entry.broken}</span></div>}
      <p className="leading-relaxed">{entry.description || <span className="italic text-faint">The catalog has no description for this one. Check the script.</span>}</p>
      {entry.depends_on?.length > 0 && (
        <p className="text-xs text-muted">Pulls in {entry.depends_on.map((d, i) => <code key={i} className="mx-0.5 font-mono">{typeof d === 'string' ? d : d.name || JSON.stringify(d)}</code>)}</p>
      )}
      {entry.required_vars?.length > 0 && (
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted">Needs
          {entry.required_vars.map((v) => (
            <code key={v} className="chip bg-accent-soft font-mono text-accent" title={entry.identity_vars?.includes(v) ? 'Filled automatically at deploy' : 'You set this'}>
              {v}{entry.identity_vars?.includes(v) ? ' · auto' : ''}
            </code>
          ))}
        </p>
      )}
      {entry.script && (
        <div>
          <button className="flex items-center gap-1 text-xs font-semibold text-accent hover:underline" onClick={() => setShowScript(!showScript)}>
            <Icon name="chevron" className={`h-3 w-3 transition ${showScript ? 'rotate-90' : ''}`} />{showScript ? 'Hide' : 'Show'} script
          </button>
          {showScript && <pre className="mt-2 max-h-72 overflow-auto rounded-2xl bg-sunken p-3 font-mono text-[11px] leading-relaxed text-fg shadow-inset">{entry.script}</pre>}
        </div>
      )}
    </div>
  )
}

// ── env var editor ────────────────────────────────────────────────────────────────────────

function VarsEditor({ rows, setRows, required, identity }) {
  const update = (i, k, v) => setRows(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)))
  return (
    <div className="space-y-2">
      {identity.map((v) => (
        <div key={v} className="flex items-center gap-2 text-xs text-muted" title="tezcatlipoca fills this per team at deploy time">
          <span className="field !w-2/5 shrink-0 font-mono opacity-70">{v}</span>
          <span className="flex items-center gap-1.5"><Icon name="sparkle" className="h-3.5 w-3.5 text-accent" />filled with the box's address at deploy</span>
        </div>
      ))}
      {rows.map((r, i) => {
        const missing = required.includes(r.key) && !r.value
        return (
          <div key={i} className="flex items-center gap-2">
            <input className={`${inputCls} !w-2/5 shrink-0 font-mono`} value={r.key} placeholder="NAME" aria-label="Variable name" onChange={(e) => update(i, 'key', e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_'))} />
            <input className={`${inputCls} min-w-0 !flex-1 font-mono ${missing ? '!border-lin' : ''}`} value={r.value} aria-label={`${r.key || 'Variable'} value`}
              placeholder={missing ? 'required' : 'value'} onChange={(e) => update(i, 'value', e.target.value)} />
            <button onClick={() => setRows(rows.filter((_, j) => j !== i))} className="rounded-full p-1.5 text-faint hover:bg-sunken hover:text-danger" aria-label="Remove variable"><Icon name="trash" className="h-3.5 w-3.5" /></button>
          </div>
        )
      })}
      <button onClick={() => setRows([...rows, { key: '', value: '' }])} className="flex items-center gap-1 pt-1 text-sm font-semibold text-accent hover:underline"><Icon name="plus" className="h-3.5 w-3.5" />Add variable</button>
    </div>
  )
}

// ── selected pin: description, scoring, vars ──────────────────────────────────────────────

const MODES = [['both', 'Plant & score'], ['score_only', 'Score only'], ['plant_only', 'Plant only']]

function PinDetail({ kind, pin, boxName, onSave, onRemove }) {
  const [entry, setEntry] = useState(null)
  const [entryErr, setEntryErr] = useState(null)
  const [draft, setDraft] = useState(pin)
  const [rows, setRows] = useState(() => Object.entries(pin.vars || {}).map(([key, value]) => ({ key, value })))
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api.catalogEntry(pin.name).then((e) => {
      setEntry(e)
      // Pre-add empty rows for vars the script needs, so the author sees what to fill.
      const have = new Set(Object.keys(pin.vars || {}))
      const add = (e.required_vars || []).filter((v) => !have.has(v) && !(e.identity_vars || []).includes(v))
      if (add.length) setRows((r) => [...r, ...add.map((key) => ({ key, value: '' }))])
    }).catch(setEntryErr)
  }, [pin])

  const vars = Object.fromEntries(rows.filter((r) => r.key.trim() && r.value !== '').map((r) => [r.key.trim(), r.value]))
  const norm = (p, v) => JSON.stringify([p.display || '', String(p.port || ''), p.check || '', !!p.score_only, !!p.plant_only, v])
  const dirty = norm(draft, vars) !== norm(pin, pin.vars || {})

  const set = (k) => (e) => setDraft({ ...draft, [k]: e.target.value })
  const mode = draft.score_only ? 'score_only' : draft.plant_only ? 'plant_only' : 'both'
  const save = async () => {
    setSaving(true)
    const { vars: _ignored, ...rest } = draft
    await onSave({ ...rest, ...(Object.keys(vars).length ? { vars } : {}) })
    setSaving(false)
  }
  useSaveHotkey(save, dirty && !saving)
  const identity = (entry?.identity_vars || []).filter((v) => !(v in vars))

  return (
    <div className="flex h-full flex-col">
      <div className="px-7 pt-6 pb-4">
        <div className="label">{META[kind].one} on <span className="font-mono normal-case tracking-normal">{boxName}</span></div>
        <h2 className="break-all font-mono text-2xl font-semibold">{pin.name}</h2>
      </div>
      <div className="flex-1 space-y-7 overflow-auto px-7 pb-6">
        {entry ? <CatalogInfo entry={entry} /> : entryErr
          ? <p className="rounded-2xl bg-sunken p-3 text-xs text-muted shadow-inset">{/^score\//.test(pin.name) ? 'A native service scored without a plant (score/<slice> pin): nothing is installed, the engine just checks the port.' : `Couldn't load catalog details: ${entryErr.message}`}</p>
          : <Spinner />}

        {kind === 'services' && (
          <section className="space-y-4 border-t-2 border-line pt-6">
            <div>
              <h3 className="display text-lg">Scoring</h3>
              <p className="text-xs text-faint">How the engine checks this service each round.</p>
            </div>
            <div className="flex w-fit gap-1 rounded-full bg-sunken p-1 shadow-inset" role="radiogroup" aria-label="Plant and score">
              {MODES.map(([m, label]) => (
                <button key={m} role="radio" aria-checked={mode === m} onClick={() => setDraft({ ...draft, score_only: m === 'score_only', plant_only: m === 'plant_only' })}
                  className={`rounded-full px-3.5 py-1.5 text-xs font-semibold transition ${mode === m ? 'bg-bg text-fg shadow-tile' : 'text-muted hover:text-fg'}`}>{label}</button>
              ))}
            </div>
            {mode !== 'plant_only' && (
              <div className="grid grid-cols-[1fr_8rem_1fr] gap-4">
                <Field label="Check name" hint={<>Scoreboard shows <span className="font-mono">{boxName}-{draft.display || '…'}</span></>}><input className={inputCls} value={draft.display || ''} onChange={set('display')} placeholder="http" /></Field>
                <Field label="Port"><input inputMode="numeric" className={`${inputCls} font-mono`} value={draft.port || ''} onChange={(e) => setDraft({ ...draft, port: e.target.value.replace(/\D/g, '') })} placeholder="80" /></Field>
                <Field label="Check type" hint="Blank uses the service's own check"><input className={inputCls} value={draft.check || ''} onChange={set('check')} placeholder="default" /></Field>
              </div>
            )}
          </section>
        )}

        <section className="space-y-4 border-t-2 border-line pt-6">
          <div>
            <h3 className="display text-lg">Environment variables</h3>
            <p className="text-xs text-faint">Passed to the nakon script when it runs on the box.</p>
          </div>
          <VarsEditor rows={rows} setRows={setRows} required={entry?.required_vars || []} identity={identity} />
        </section>
      </div>
      <div className="flex items-center gap-3 px-7 pb-6 pt-3">
        <Button variant="danger" onClick={onRemove}><Icon name="trash" />Remove</Button>
        <span className="ml-auto"><Dirty dirty={dirty} /></span>
        <Button variant="primary" disabled={saving || !dirty} onClick={save} title={`Save (${saveKey})`}>{saving ? 'Saving…' : 'Save'}</Button>
      </div>
    </div>
  )
}

// ── add mode: catalog search; picking one splits the pane 50/50 with a preview ─────────────

function CatalogPicker({ kind, platform, boxName, existing, onAdd, onClose }) {
  const [q, setQ] = useState('')
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)
  const [picked, setPicked] = useState(null)
  const [cat, setCat] = useState('all')
  const listRef = useRef()

  useEffect(() => {
    setRows(null); setErr(null)
    api.catalog({ platform, kind }).then(setRows).catch(setErr)
  }, [platform, kind])

  const cats = useMemo(() => [...new Set((rows || []).map((r) => r.category))].sort(), [rows])
  const shown = useMemo(() => {
    const n = q.toLowerCase()
    return (rows || []).filter((r) => (cat === 'all' || r.category === cat) && `${r.name} ${r.description || ''}`.toLowerCase().includes(n))
  }, [rows, q, cat])

  // ↑/↓ moves the highlight, Enter adds it — the picker works without the mouse.
  const onKeyDown = (e) => {
    if (!shown.length) return
    const i = picked ? shown.findIndex((r) => r.name === picked.name) : -1
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const next = shown[Math.max(0, Math.min(shown.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))]
      setPicked(next)
      listRef.current?.querySelector(`[data-name="${CSS.escape(next.name)}"]`)?.scrollIntoView({ block: 'nearest' })
    } else if (e.key === 'Enter' && picked) {
      e.preventDefault(); onAdd(picked)
    }
  }

  return (
    <div className="flex h-full flex-col" onKeyDown={onKeyDown}>
      <div className="flex items-start justify-between px-7 pt-6 pb-1">
        <div>
          <h2 className="display text-2xl">Add a {META[kind].one}</h2>
          <p className="mt-1 flex items-center gap-2 text-xs text-faint">from the vulndb catalog · <PlatformBadge platform={platform} />{rows && <span>· {shown.length} of {rows.length}</span>}</p>
        </div>
        <button onClick={onClose} className="btn btn-ghost h-8 w-8 !p-0 text-faint" aria-label="Close catalog">✕</button>
      </div>
      <div className={`flex min-h-0 flex-col ${picked ? 'h-1/2 border-b-2 border-line' : 'flex-1'}`}>
        <div className="space-y-3 px-7 py-4">
          <SearchInput value={q} onChange={setQ} autoFocus placeholder="Search names and descriptions" />
          {cats.length > 1 && (
            <div className="flex flex-wrap gap-1.5">
              {['all', ...cats].map((c) => (
                <button key={c} onClick={() => setCat(c)} className={`chip px-3 py-1 transition ${cat === c ? 'bg-accent text-accent-fg' : 'bg-sunken text-muted hover:text-fg'}`}>{c}</button>
              ))}
            </div>
          )}
        </div>
        <div ref={listRef} className="min-h-0 flex-1 overflow-auto px-7 pb-5 pt-1">
          <ErrorBanner error={err} />
          {!rows && !err && <div className="flex justify-center p-6"><Spinner /></div>}
          {rows && !shown.length && <p className="p-2 text-center text-sm text-faint">Nothing matches. Try fewer words.</p>}
          <ul className="space-y-3">
            {shown.map((r) => (
              <li key={r.name} data-name={r.name}>
                <button onClick={() => setPicked(r)} onDoubleClick={() => onAdd(r)} className={`tile tile-hover w-full px-4 py-2.5 text-left ${picked?.name === r.name ? 'hatched !border-accent' : ''}`}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-sm">{r.name}</span>
                    <span className="flex shrink-0 items-center gap-2">
                      {missingVars({}, r).length > 0 && <span className="text-[11px] text-faint" title={`Needs ${missingVars({}, r).join(', ')}`}>needs vars</span>}
                      {existing.has(r.name) && <span className="text-[11px] font-semibold text-accent">on box</span>}
                      {r.broken && <span className="text-[11px] font-semibold text-danger">broken</span>}
                    </span>
                  </div>
                  {r.description && <p className="line-clamp-1 text-xs text-muted">{r.description}</p>}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
      {picked && (
        <div className="flex h-1/2 min-h-0 flex-col">
          <div className="min-h-0 flex-1 overflow-auto px-7 pt-5">
            <h3 className="mb-3 break-all font-mono text-lg font-semibold">{picked.name}</h3>
            <CatalogInfo entry={picked} />
          </div>
          <div className="flex items-center justify-between gap-3 px-7 pb-6 pt-3">
            <span className="text-[11px] text-faint"><kbd>↑</kbd> <kbd>↓</kbd> browse · <kbd>Enter</kbd> add</span>
            <Button variant="primary" onClick={() => onAdd(picked)}><Icon name="plus" />{existing.has(picked.name) ? 'Add again' : `Add to ${boxName}`}</Button>
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
  const [catalog, setCatalog] = useState({})
  const toast = useToast()
  const ask = useConfirm()
  const searchRef = useRef()
  useSlashFocus(searchRef)

  const box = comp.boxes.find((b) => b.name === boxName)
  const file = kind === 'services' ? comp.box_services : comp.box_vulns
  const raw = useMemo(() => (box ? file[box.name] || [] : []), [file, box])
  const pins = useMemo(() => raw.map(asPin), [raw])
  const platform = box ? platformOf(box.template) : 'linux'

  // One catalog read per page, used to flag pins that are missing required vars.
  useEffect(() => {
    api.catalog({ platform, kind }).then((rows) => setCatalog(Object.fromEntries(rows.map((r) => [r.name, r])))).catch(() => {})
  }, [platform, kind])

  if (!box) return <EmptyState icon="box" title={`No box called ${boxName}`} />

  const sel = params.has('sel') ? Number(params.get('sel')) : null
  const adding = params.get('add') === '1'
  const shown = pins.map((p, i) => [p, i]).filter(([p]) => p.name.toLowerCase().includes(q.toLowerCase()))

  const save = async (next, selectIdx, message) => {
    try {
      await api.putPins(comp.id, box.name, kind, next)
      await reload()
      setParams(selectIdx == null ? {} : { sel: String(selectIdx) })
      if (message) toast(message)
    } catch (e) { setErr(e); toast(e.message, 'error') }
  }
  // Untouched entries go back exactly as they were read (bare names stay bare names).
  const replaceAt = (i, pin) => raw.map((p, j) => (j === i ? pin : p))
  const remove = async (i) => {
    const ok = await ask({ title: `Remove ${pins[i].name}?`, confirm: 'Remove', danger: true,
      body: <>It comes off <span className="font-mono">{box.name}</span>'s {META[kind].label.toLowerCase()} list. You can add it back from the catalog.</> })
    if (ok) save(raw.filter((_, j) => j !== i), null, `Removed ${pins[i].name}`)
  }
  const added = (row) => {
    const miss = missingVars({}, row)
    return miss.length ? `Added ${row.name}. Now set ${miss.join(', ')}` : `Added ${row.name}`
  }

  return (
    <div className="flex h-full gap-8">
      <section className="panel rise flex w-96 shrink-0 flex-col">
        <div className="px-6 pt-6 pb-4">
          <Link to={`/c/${comp.id}/boxes/${box.name}?tab=${kind}`} className="flex items-center gap-1 font-mono text-xs text-faint transition hover:text-accent">
            <Icon name="back" className="h-3 w-3" />{box.name}
          </Link>
          <div className="mt-1 flex items-baseline justify-between">
            <h1 className="display text-3xl">{META[kind].label}</h1>
            <span className="text-sm tabular-nums text-faint">{pins.length}</span>
          </div>
        </div>
        <div className="px-6 pb-4"><SearchInput inputRef={searchRef} value={q} onChange={setQ} placeholder={`Filter ${META[kind].label.toLowerCase()}`} hotkey="/" /></div>
        <div className="px-6"><ErrorBanner error={err} onClose={() => setErr(null)} /></div>
        <ul className="min-h-0 flex-1 space-y-3 overflow-auto px-6 py-1">
          {pins.length === 0 && <li><EmptyState icon={META[kind].icon} title={`No ${META[kind].label.toLowerCase()} yet`}>Use <b>Add</b> below to browse the catalog.</EmptyState></li>}
          {pins.length > 0 && shown.length === 0 && <li className="py-6 text-center text-sm text-faint">Nothing matches “{q}”.</li>}
          {shown.map(([p, i]) => {
            const miss = missingVars(p, catalog[p.name])
            return (
              <li key={i}>
                <button onClick={() => setParams({ sel: String(i) })}
                  className={`tile tile-hover w-full px-4 py-2.5 text-left ${sel === i && !adding ? 'hatched !border-accent' : ''}`}>
                  <div className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate font-mono text-sm">{p.name}</span>
                    {miss.length > 0 && <span className="flex items-center gap-1 text-[11px] font-semibold text-lin" title={`Missing ${miss.join(', ')}`}><Icon name="warn" className="h-3.5 w-3.5" />needs vars</span>}
                  </div>
                  <div className="text-xs text-faint">
                    {[p.display && `${p.display}${p.port ? `:${p.port}` : ''}`, p.score_only && 'score only', p.plant_only && 'plant only', p.vars && `${Object.keys(p.vars).length} var${Object.keys(p.vars).length === 1 ? '' : 's'}`].filter(Boolean).join(' · ') || ' '}
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
        <div className="p-6">
          <Button className="w-full !py-2.5" variant={adding ? 'default' : 'primary'} onClick={() => setParams({ add: '1' })} disabled={adding}>
            <Icon name="plus" />{adding ? 'Pick from the catalog →' : `Add ${META[kind].one}`}
          </Button>
        </div>
      </section>

      <section className="panel rise rise-1 min-w-0 flex-1 overflow-hidden">
        {adding ? (
          <CatalogPicker kind={kind} platform={platform} boxName={box.name} existing={new Set(pins.map((p) => p.name))}
            onClose={() => setParams({})}
            onAdd={(row) => save([...raw, row.name], raw.length, added(row))} />
        ) : sel != null && pins[sel] ? (
          <PinDetail key={`${sel}-${JSON.stringify(raw[sel])}`} kind={kind} pin={pins[sel]} boxName={box.name}
            onSave={(pin) => save(replaceAt(sel, pin), sel, 'Saved')}
            onRemove={() => remove(sel)} />
        ) : (
          <div className="flex h-full items-center justify-center">
            <EmptyState icon={META[kind].icon} title={`Select a ${META[kind].one}`}
              action={<Button onClick={() => setParams({ add: '1' })}><Icon name="plus" />Browse the catalog</Button>}>
              Its description and environment variables open here.
            </EmptyState>
          </div>
        )}
      </section>
    </div>
  )
}
