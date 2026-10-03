import { useEffect, useState } from 'react'
import { api } from '../api'
import { useComp } from './CompLayout'
import MarkdownEditor from '../components/MarkdownEditor'
import { Button, ErrorBanner } from '../components/ui'

export default function Packet() {
  const { comp } = useComp()
  const [body, setBody] = useState(null)
  const [saved, setSaved] = useState(true)
  const [err, setErr] = useState(null)
  useEffect(() => { api.packet(comp.id).then((d) => setBody(d.body)).catch(setErr) }, [comp.id])

  const save = async () => {
    try { await api.putPacket(comp.id, body); setSaved(true) } catch (e) { setErr(e) }
  }
  return (
    <div className="space-y-8 pb-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-4xl font-bold tracking-tight">Packet</h1>
          <p className="font-mono text-xs text-faint">competitions/{comp.id}/packet.md — the competitor-facing packet</p>
        </div>
        <Button variant="primary" onClick={save} disabled={saved}>{saved ? 'Saved' : 'Save'}</Button>
      </div>
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      {body !== null && <MarkdownEditor value={body} onChange={(v) => { setBody(v); setSaved(false) }} minHeight={600} />}
    </div>
  )
}
