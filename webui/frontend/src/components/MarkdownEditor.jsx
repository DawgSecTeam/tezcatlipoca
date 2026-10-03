import { useMemo, useState } from 'react'
import { marked } from 'marked'

// Markdown for now; the Nextcloud/office integration can replace this component wholesale.
export default function MarkdownEditor({ value, onChange, minHeight = 420 }) {
  const [mode, setMode] = useState('split')
  const html = useMemo(() => marked.parse(value || ''), [value])
  const tab = (m, label) => (
    <button onClick={() => setMode(m)} className={`rounded px-2 py-1 text-xs font-medium ${mode === m ? 'bg-slate-800 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>{label}</button>
  )
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <div className="flex gap-1 border-b border-slate-200 bg-slate-50 px-2 py-1.5">
        {tab('edit', 'Write')}{tab('split', 'Split')}{tab('preview', 'Preview')}
      </div>
      <div className={`grid ${mode === 'split' ? 'grid-cols-2 divide-x divide-slate-200' : 'grid-cols-1'}`} style={{ minHeight }}>
        {mode !== 'preview' && (
          <textarea value={value} onChange={(e) => onChange(e.target.value)} spellCheck
            className="h-full w-full resize-none p-4 font-mono text-sm leading-relaxed focus:outline-none" style={{ minHeight }} />
        )}
        {mode !== 'edit' && (
          <div className="prose prose-sm prose-slate max-w-none overflow-auto p-4" dangerouslySetInnerHTML={{ __html: html }} />
        )}
      </div>
    </div>
  )
}
