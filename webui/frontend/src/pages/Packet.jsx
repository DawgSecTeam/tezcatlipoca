import { useEffect, useState } from 'react'
import { api } from '../api'
import { useComp } from './CompLayout'
import MarkdownEditor from '../components/MarkdownEditor'
import { Button, ErrorBanner } from '../components/ui'
import { Dirty, PageHeader, saveKey, useSaveHotkey, useToast } from '../components/kit'

export default function Packet() {
  const { comp } = useComp()
  const [body, setBody] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [err, setErr] = useState(null)
  const toast = useToast()
  useEffect(() => { api.packet(comp.id).then((d) => setBody(d.body)).catch(setErr) }, [comp.id])

  const save = async () => {
    try { await api.putPacket(comp.id, body); setDirty(false); toast('Packet saved') } catch (e) { setErr(e) }
  }
  useSaveHotkey(save, dirty)

  return (
    <div className="pb-6">
      <PageHeader crumbs={[[comp.name, `/c/${comp.id}`], ['Packet']]} title="Competitor packet"
        sub={<>What teams receive ahead of the event · <span className="font-mono text-xs text-faint">packet.md</span></>}
        actions={<><Dirty dirty={dirty} /><Button variant="primary" onClick={save} disabled={!dirty} title={`Save (${saveKey})`}>Save</Button></>} />
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      {body !== null && (
        <div className="rise rise-1">
          <MarkdownEditor value={body} onChange={(v) => { setBody(v); setDirty(true) }} minHeight={620}
            placeholder="Scenario, format, scored services, rules… generate-packet.py can draft one from the comp's config." />
        </div>
      )}
    </div>
  )
}
