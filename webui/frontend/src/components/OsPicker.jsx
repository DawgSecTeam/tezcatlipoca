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
      <div className="-mx-2 mt-4 max-h-80 space-y-2 overflow-auto px-2 py-1">
        {!templates && !err && <p className="p-2 text-sm text-faint">Loading…</p>}
        {shown.map((t) => (
          <button key={t.name} onClick={() => onPick(t.name)}
            className={`tile tile-hover flex w-full items-center justify-between px-4 py-2.5 text-left text-sm ${t.name === value ? '!border-accent' : ''}`}>
            <span className="font-mono">{t.name}</span>
            <span className="flex items-center gap-2">
              {t.live && <span className="text-[11px] text-accent">on node</span>}
              <PlatformBadge platform={t.platform} />
            </span>
          </button>
        ))}
      </div>
      <form className="mt-4 flex gap-2" onSubmit={(e) => { e.preventDefault(); custom.trim() && onPick(custom.trim()) }}>
        <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="Other template name…" className="field font-mono" />
        <button className="btn">Use</button>
      </form>
      <p className="mt-2 text-xs text-faint">Deploy preflight checks the template exists on the target node.</p>
    </Modal>
  )
}
