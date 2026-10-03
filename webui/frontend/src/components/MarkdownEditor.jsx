import { useMemo, useState } from 'react'
import { marked } from 'marked'

// Markdown for now; the Nextcloud/office integration can replace this component wholesale.
export default function MarkdownEditor({ value, onChange, minHeight = 420 }) {
  const [mode, setMode] = useState('split')
  const html = useMemo(() => marked.parse(value || ''), [value])
  const tab = (m, label) => (
    <button onClick={() => setMode(m)} className={`rounded-full px-3 py-1 text-xs font-semibold transition ${mode === m ? 'bg-accent text-accent-fg' : 'text-muted hover:text-fg'}`}>{label}</button>
  )
  return (
    <div className="panel overflow-hidden">
      <div className="flex gap-1 px-4 pt-3 pb-2">
        {tab('edit', 'Write')}{tab('split', 'Split')}{tab('preview', 'Preview')}
      </div>
      <div className={`grid gap-3 px-3 pb-3 ${mode === 'split' ? 'grid-cols-2' : 'grid-cols-1'}`} style={{ minHeight }}>
        {mode !== 'preview' && (
          <textarea value={value} onChange={(e) => onChange(e.target.value)} spellCheck
            className="h-full w-full resize-none rounded-2xl bg-sunken p-4 font-mono text-sm leading-relaxed text-fg shadow-inset focus:outline-none" style={{ minHeight }} />
        )}
        {mode !== 'edit' && (
          <div className="prose prose-sm max-w-none overflow-auto p-4 dark:prose-invert" dangerouslySetInnerHTML={{ __html: html }} />
        )}
      </div>
    </div>
  )
}
