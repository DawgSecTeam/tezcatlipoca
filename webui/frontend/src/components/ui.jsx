import { useEffect, useRef } from 'react'

export function Button({ variant = 'default', className = '', ...props }) {
  const styles = {
    default: 'bg-white border border-slate-300 hover:bg-slate-100 text-slate-800',
    primary: 'bg-indigo-600 hover:bg-indigo-700 text-white border border-indigo-600',
    danger: 'bg-white border border-red-300 text-red-700 hover:bg-red-50',
    ghost: 'text-slate-600 hover:bg-slate-100',
  }
  return (
    <button
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition disabled:opacity-50 disabled:cursor-not-allowed ${styles[variant]} ${className}`}
      {...props}
    />
  )
}

export function SearchInput({ value, onChange, placeholder = 'Search…', autoFocus, className = '' }) {
  return (
    <div className={`relative ${className}`}>
      <input
        autoFocus={autoFocus}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-md border border-slate-300 bg-white py-1.5 pl-3 pr-8 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
      />
      <svg className="pointer-events-none absolute right-2.5 top-2 h-4 w-4 text-slate-400" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2">
        <circle cx="8.5" cy="8.5" r="5.5" />
        <path d="M13 13l4 4" strokeLinecap="round" />
      </svg>
    </div>
  )
}

export function Field({ label, children, hint }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-400">{hint}</span>}
    </label>
  )
}

export const inputCls =
  'w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500'

export function Card({ className = '', ...props }) {
  return <div className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`} {...props} />
}

export function ErrorBanner({ error, onClose }) {
  if (!error) return null
  return (
    <div className="mb-4 flex items-start justify-between gap-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      <span className="whitespace-pre-wrap">{String(error.message || error)}</span>
      {onClose && <button onClick={onClose} className="text-red-500 hover:text-red-700">✕</button>}
    </div>
  )
}

export function Modal({ open, onClose, title, children, wide }) {
  const ref = useRef()
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-slate-900/40 p-4 pt-[10vh]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className={`w-full ${wide ? 'max-w-3xl' : 'max-w-md'} rounded-xl bg-white shadow-xl`}>
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
          <h2 className="font-semibold">{title}</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  )
}

export function PlatformBadge({ platform }) {
  const cls = platform === 'windows' ? 'bg-sky-100 text-sky-800' : platform === 'linux' ? 'bg-amber-100 text-amber-800' : 'bg-slate-100 text-slate-700'
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>{platform}</span>
}

export function Spinner() {
  return <div className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-indigo-600" />
}
