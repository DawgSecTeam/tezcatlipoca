import { useEffect, useState } from 'react'
import { api } from '../api'
import { Modal, PlatformBadge, SearchInput, ErrorBanner } from './ui'

// The "operating system" pill opens this: a searchable list of Proxmox templates.
export default function OsPicker({ open, onClose, value, onPick }) {
  const [templates, setTemplates] = useState(null)
  const [q, setQ] = useState('')
  const [err, setErr] = useState(null)
  const [custom, setCustom] = useState('')

  useEffect(() => {
    if (open && !templates) api.templates().then(setTemplates).catch(setErr)
  }, [open, templates])

  const shown = (templates || []).filter((t) => t.name.toLowerCase().includes(q.toLowerCase()))

  return (
    <Modal open={open} onClose={onClose} title="Operating system">
      <ErrorBanner error={err} />
      <SearchInput value={q} onChange={setQ} autoFocus placeholder="Search templates…" />
      <div className="mt-3 max-h-80 overflow-auto">
        {!templates && !err && <p className="p-2 text-sm text-slate-400">Loading…</p>}
        {shown.map((t) => (
          <button key={t.name} onClick={() => onPick(t.name)}
            className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm hover:bg-slate-100 ${t.name === value ? 'bg-indigo-50 ring-1 ring-indigo-300' : ''}`}>
            <span className="font-mono">{t.name}</span>
            <span className="flex items-center gap-2">
              {t.live && <span className="text-[11px] text-emerald-600">on node</span>}
              <PlatformBadge platform={t.platform} />
            </span>
          </button>
        ))}
      </div>
      <form className="mt-3 flex gap-2 border-t border-slate-200 pt-3" onSubmit={(e) => { e.preventDefault(); custom.trim() && onPick(custom.trim()) }}>
        <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="Other template name…"
          className="flex-1 rounded-md border border-slate-300 px-2.5 py-1.5 font-mono text-sm" />
        <button className="rounded-md border border-slate-300 px-3 text-sm hover:bg-slate-100">Use</button>
      </form>
      <p className="mt-2 text-xs text-slate-400">Deploy preflight checks the template exists on the target node.</p>
    </Modal>
  )
}
