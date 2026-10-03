import { useMemo, useRef, useState } from 'react'
import { marked } from 'marked'

// Markdown for now; the Nextcloud/office integration can replace this component wholesale.

// Wrap the selection (or insert at the caret) and keep the selection on the wrapped text.
function wrap(el, before, after = before, placeholder = '') {
  const { selectionStart: s, selectionEnd: e, value } = el
  const inner = value.slice(s, e) || placeholder
  const next = value.slice(0, s) + before + inner + after + value.slice(e)
  return { next, sel: [s + before.length, s + before.length + inner.length] }
}
// Prefix every selected line (headings, lists, quotes).
function prefixLines(el, prefix) {
  const { selectionStart: s, selectionEnd: e, value } = el
  const start = value.lastIndexOf('\n', s - 1) + 1
  const block = value.slice(start, e).split('\n').map((l) => prefix + l).join('\n')
  return { next: value.slice(0, start) + block + value.slice(e), sel: [start, start + block.length] }
}

const TOOLS = [
  ['B', 'Bold', (el) => wrap(el, '**', '**', 'bold'), 'font-bold', 'b'],
  ['I', 'Italic', (el) => wrap(el, '_', '_', 'italic'), 'italic', 'i'],
  ['H', 'Heading', (el) => prefixLines(el, '## '), 'font-semibold'],
  ['•', 'Bulleted list', (el) => prefixLines(el, '- ')],
  ['1.', 'Numbered list', (el) => prefixLines(el, '1. '), 'text-[11px]'],
  ['❝', 'Quote', (el) => prefixLines(el, '> ')],
  ['</>', 'Code', (el) => wrap(el, '`', '`', 'code'), 'font-mono text-[11px]'],
  ['↗', 'Link', (el) => wrap(el, '[', '](https://)', 'text'), '', 'k'],
]

export default function MarkdownEditor({ value, onChange, minHeight = 420, placeholder = 'Start writing…' }) {
  const [mode, setMode] = useState('split')
  const ta = useRef()
  const html = useMemo(() => marked.parse(value || ''), [value])
  const words = useMemo(() => (value || '').trim().split(/\s+/).filter(Boolean).length, [value])

  const apply = (fn) => {
    const el = ta.current
    if (!el) return
    const { next, sel } = fn(el)
    onChange(next)
    requestAnimationFrame(() => { el.focus(); el.setSelectionRange(...sel) })
  }
  const onKeyDown = (e) => {
    if (!(e.metaKey || e.ctrlKey)) return
    const tool = TOOLS.find((t) => t[4] && t[4] === e.key.toLowerCase())
    if (tool) { e.preventDefault(); apply(tool[2]) }
  }

  const tab = (m, label) => (
    <button onClick={() => setMode(m)} className={`rounded-full px-3 py-1 text-xs font-semibold transition ${mode === m ? 'bg-bg text-fg shadow-tile' : 'text-muted hover:text-fg'}`}>{label}</button>
  )
  return (
    <div className="panel overflow-hidden">
      <div className="flex items-center gap-3 px-4 pt-3 pb-2">
        <div className="flex gap-1 rounded-full bg-sunken p-1 shadow-inset">{tab('edit', 'Write')}{tab('split', 'Split')}{tab('preview', 'Preview')}</div>
        {mode !== 'preview' && (
          <div className="flex items-center gap-0.5" role="toolbar" aria-label="Formatting">
            {TOOLS.map(([glyph, label, fn, cls = '', key]) => (
              <button key={label} onClick={() => apply(fn)} title={key ? `${label} (Ctrl/⌘ ${key.toUpperCase()})` : label} aria-label={label}
                className={`grid h-7 min-w-7 place-items-center rounded-lg px-1.5 text-xs text-muted transition hover:bg-sunken hover:text-fg ${cls}`}>{glyph}</button>
            ))}
          </div>
        )}
        <span className="ml-auto text-[11px] tabular-nums text-faint">{words} word{words === 1 ? '' : 's'}</span>
      </div>
      <div className={`grid gap-3 px-3 pb-3 ${mode === 'split' ? 'grid-cols-2' : 'grid-cols-1'}`} style={{ minHeight }}>
        {mode !== 'preview' && (
          <textarea ref={ta} value={value} onChange={(e) => onChange(e.target.value)} onKeyDown={onKeyDown} spellCheck placeholder={placeholder}
            className="h-full w-full resize-none rounded-2xl bg-sunken p-5 font-mono text-[13px] leading-relaxed text-fg shadow-inset placeholder:text-faint focus:outline-none" style={{ minHeight }} />
        )}
        {mode !== 'edit' && (
          <div className="prose prose-sm max-w-none overflow-auto p-5 dark:prose-invert prose-headings:font-[family-name:var(--font-display)] prose-headings:font-semibold prose-a:text-accent"
            dangerouslySetInnerHTML={{ __html: html || '<p style="opacity:.4">Nothing to preview yet.</p>' }} />
        )}
      </div>
    </div>
  )
}
