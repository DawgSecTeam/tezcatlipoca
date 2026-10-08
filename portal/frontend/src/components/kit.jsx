// App-wide UX plumbing: icons, toasts, themed confirm, breadcrumbs, small inputs and formatters.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button, Modal } from './ui'

// ── icons (1.6px strokes on a 20px grid, currentColor) ────────────────────────────────────

const PATHS = {
  overview: 'M3 4.5h6v5H3zM11 4.5h6v3h-6zM11 9.5h6v6h-6zM3 11.5h6v4H3z',
  injects: 'M4 3.5h9l3 3v10H4zM13 3.5v3h3M7 9.5h6M7 12.5h4',
  packet: 'M5 3.5h7l3 3v10H5zM8 8h5M8 11h5M8 14h3',
  boxes: 'M3 5.5l7-3 7 3-7 3zM3 5.5v8l7 3 7-3v-8M10 8.5v8',
  box: 'M3.5 4.5h13v8h-13zM7 15.5h6M10 12.5v3',
  shield: 'M10 2.5l6 2.5v4.5c0 4-2.6 6.6-6 8-3.4-1.4-6-4-6-8V5z',
  service: 'M4 5.5h12v4H4zM4 11.5h12v4H4zM6.5 7.5h.01M6.5 13.5h.01',
  plus: 'M10 4v12M4 10h12',
  back: 'M12 4l-6 6 6 6',
  chevron: 'M7 4l6 6-6 6',
  edit: 'M4 16l1-4 8-8 3 3-8 8zM11.5 5.5l3 3',
  trash: 'M4 6h12M8 6V4h4v2M6 6l1 10h6l1-10',
  rocket: 'M10 16c-1-1.5-1.5-3-1.5-5 0-3 1.5-6 1.5-8.5 0 2.5 1.5 5.5 1.5 8.5 0 2-.5 3.5-1.5 5zM8.5 12L6 14.5l.5-3M11.5 12l2.5 2.5-.5-3',
  play: 'M7 4.5v11l8-5.5z',
  check: 'M4.5 10.5l3.5 3.5 7.5-8',
  search: 'M8.5 3a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM13 13l4 4',
  cpu: 'M6 6h8v8H6zM8 2.5v3M12 2.5v3M8 14.5v3M12 14.5v3M2.5 8h3M2.5 12h3M14.5 8h3M14.5 12h3',
  ram: 'M3 6.5h14v7H3zM6 13.5v2M10 13.5v2M14 13.5v2M6 9h2M11 9h2',
  disk: 'M10 3c3.9 0 7 1.1 7 2.5v9c0 1.4-3.1 2.5-7 2.5s-7-1.1-7-2.5v-9C3 4.1 6.1 3 10 3zM3 5.5c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5',
  net: 'M10 3v4M10 13v4M3 10h4M13 10h4M7 7h6v6H7z',
  clock: 'M10 3a7 7 0 100 14 7 7 0 000-14zM10 6v4l2.5 2',
  terminal: 'M3.5 4.5h13v11h-13zM6.5 8l2.5 2-2.5 2M10.5 12.5h3',
  warn: 'M10 3l7.5 13h-15zM10 8v3.5M10 14h.01',
  gear: 'M10 7.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5zM10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4',
  sparkle: 'M10 3l1.6 4.4L16 9l-4.4 1.6L10 15l-1.6-4.4L4 9l4.4-1.6z',
}

export function Icon({ name, className = 'h-4 w-4' }) {
  return (
    <svg viewBox="0 0 20 20" className={className} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  )
}

// The mark: an obsidian "smoking mirror" — a dark disc, a jade reflection, a rim of light.
export function Logo({ className = 'h-6 w-6' }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <circle cx="12" cy="12" r="10" fill="var(--fg)" />
      <circle cx="12" cy="12" r="10" fill="none" stroke="var(--edge)" strokeWidth="1.2" opacity=".6" />
      <path d="M6.2 13.2A6 6 0 0 1 13.2 6.2" fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinecap="round" />
      <circle cx="15.6" cy="15.6" r="1.3" fill="var(--accent)" opacity=".55" />
    </svg>
  )
}

// ── toasts ────────────────────────────────────────────────────────────────────────────────

const ToastCtx = createContext(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const show = useCallback((message, tone = 'ok') => {
    const id = Math.random().toString(36).slice(2)
    setToasts((t) => [...t, { id, message, tone }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === 'error' ? 6000 : 2400)
  }, [])
  return (
    <ToastCtx.Provider value={show}>
      {children}
      <div className="pointer-events-none fixed bottom-6 left-1/2 z-[60] flex -translate-x-1/2 flex-col items-center gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast tile flex items-center gap-2 !rounded-full px-4 py-2 text-sm" style={{ animation: 'toast-in .25s cubic-bezier(.2,.7,.2,1)' }}>
            <span className={t.tone === 'error' ? 'text-danger' : 'text-accent'}><Icon name={t.tone === 'error' ? 'warn' : 'check'} /></span>
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

// ── themed confirm (replaces window.confirm) ──────────────────────────────────────────────

const ConfirmCtx = createContext(async () => false)
export const useConfirm = () => useContext(ConfirmCtx)

export function ConfirmProvider({ children }) {
  const [req, setReq] = useState(null)
  const ask = useCallback((opts) => new Promise((resolve) => setReq({ ...opts, resolve })), [])
  const close = (v) => { req?.resolve(v); setReq(null) }
  return (
    <ConfirmCtx.Provider value={ask}>
      {children}
      <Modal open={!!req} onClose={() => close(false)} title={req?.title || 'Are you sure?'}>
        {req?.body && <div className="text-sm leading-relaxed text-muted">{req.body}</div>}
        <div className="mt-6 flex justify-end gap-3">
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button variant={req?.danger ? 'danger' : 'primary'} className={req?.danger ? '!border-danger/40' : ''} autoFocus onClick={() => close(true)}>
            {req?.confirm || 'Confirm'}
          </Button>
        </div>
      </Modal>
    </ConfirmCtx.Provider>
  )
}

// ── navigation + layout bits ──────────────────────────────────────────────────────────────

export function Crumbs({ items }) {
  return (
    <nav className="mb-3 flex flex-wrap items-center gap-1.5 text-xs text-faint" aria-label="Breadcrumb">
      {items.map(([label, to], i) => (
        <span key={i} className="flex items-center gap-1.5">
          {i > 0 && <Icon name="chevron" className="h-3 w-3 opacity-60" />}
          {to ? <Link to={to} className="transition hover:text-accent">{label}</Link> : <span className="text-muted">{label}</span>}
        </span>
      ))}
    </nav>
  )
}

export function PageHeader({ crumbs, title, sub, actions, mono }) {
  return (
    <header className="rise mb-8 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {crumbs && <Crumbs items={crumbs} />}
        <h1 className={`display truncate text-5xl ${mono ? '!font-mono !text-4xl !tracking-tight' : ''}`}>{title}</h1>
        {sub && <div className="mt-2 text-sm text-muted">{sub}</div>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-3">{actions}</div>}
    </header>
  )
}

export function EmptyState({ icon = 'sparkle', title, children, action }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-10 text-center">
      <div className="mb-3 grid h-12 w-12 place-items-center rounded-full bg-sunken text-faint shadow-inset"><Icon name={icon} className="h-5 w-5" /></div>
      <div className="font-semibold">{title}</div>
      {children && <div className="mt-1 max-w-sm text-sm text-muted">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function Stepper({ value, onChange, min = 1, max = 50 }) {
  const n = Number(value) || min
  const clamp = (v) => Math.max(min, Math.min(max, v))
  return (
    <div className="flex items-center gap-2">
      <button type="button" className="btn h-10 w-10 !p-0 text-lg" onClick={() => onChange(clamp(n - 1))} disabled={n <= min} aria-label="Fewer">−</button>
      <input className="field h-10 w-20 text-center text-base font-semibold tabular-nums" value={value} inputMode="numeric"
        onChange={(e) => onChange(e.target.value.replace(/\D/g, ''))} onBlur={() => onChange(clamp(n))} />
      <button type="button" className="btn h-10 w-10 !p-0 text-lg" onClick={() => onChange(clamp(n + 1))} disabled={n >= max} aria-label="More">+</button>
    </div>
  )
}

export function DifficultyMeter({ value }) {
  const n = Number(value) || 0
  return (
    <span className="flex items-center gap-1.5" title={`Difficulty ${n}/10`}>
      <span className="flex gap-[3px]">
        {Array.from({ length: 10 }, (_, i) => (
          <span key={i} className={`h-2.5 w-1 rounded-full ${i < n ? (n >= 8 ? 'bg-danger' : 'bg-accent') : 'bg-line'}`} />
        ))}
      </span>
      <span className="text-[11px] font-semibold tabular-nums text-muted">{n || '—'}</span>
    </span>
  )
}

export function Dirty({ dirty }) {
  return dirty
    ? <span className="flex items-center gap-1.5 text-xs text-muted"><span className="h-2 w-2 rounded-full bg-lin" />Unsaved</span>
    : <span className="flex items-center gap-1.5 text-xs text-faint"><Icon name="check" className="h-3.5 w-3.5" />Saved</span>
}

// ⌘S / Ctrl+S → save, but only while there is something to save.
export function useSaveHotkey(save, enabled) {
  const ref = useRef(save)
  ref.current = save
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (enabled) ref.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [enabled])
}

// "/" focuses the page's main search, like most dev tools.
export function useSlashFocus(ref) {
  useEffect(() => {
    const onKey = (e) => {
      const tag = document.activeElement?.tagName
      if (e.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT') {
        e.preventDefault()
        ref.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ref])
}

export const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
export const saveKey = isMac ? '⌘S' : 'Ctrl S'

// ── formatters ────────────────────────────────────────────────────────────────────────────

export const fmtMem = (mb) => (mb == null ? '—' : mb >= 1024 ? `${+(mb / 1024).toFixed(1)} GB` : `${mb} MB`)

export const fmtOffset = (m) => {
  if (m == null || m === '') return '—'
  const h = Math.floor(m / 60), mm = String(m % 60).padStart(2, '0')
  return `T+${h}:${mm}`
}

export function timeAgo(epochSeconds) {
  if (!epochSeconds) return ''
  const s = Date.now() / 1000 - epochSeconds
  if (s < 60) return 'just now'
  const units = [[60, 'min'], [3600, 'h'], [86400, 'd'], [604800, 'w'], [2592000, 'mo'], [31536000, 'y']]
  let label = 'min', div = 60
  for (const [d, l] of units) if (s >= d) { div = d; label = l }
  return `${Math.floor(s / div)}${label === 'min' ? ' min' : label} ago`
}
