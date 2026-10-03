import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Button, Card, ErrorBanner, Field, Modal, Spinner, inputCls } from './ui'

function JobLog({ job, onDone }) {
  const [text, setText] = useState('')
  const [state, setState] = useState(job)
  const offset = useRef(0)
  const pre = useRef()

  useEffect(() => {
    let stop = false
    offset.current = 0
    setText('')
    const tick = async () => {
      try {
        const r = await api.job(job.id, offset.current)
        offset.current = r.offset
        if (r.text) setText((t) => t + r.text)
        setState(r)
        if (!r.running) { onDone?.(r); return }
      } catch { /* server restarted; keep polling */ }
      if (!stop) setTimeout(tick, 1500)
    }
    tick()
    return () => { stop = true }
  }, [job.id]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [text])

  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-sm">
        {state.running ? <><Spinner /> <span>Running…</span></>
          : <span className={state.returncode === 0 ? 'text-emerald-700' : 'text-red-700'}>Exited {state.returncode}</span>}
      </div>
      <pre ref={pre} className="h-[60vh] overflow-auto rounded-md bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">{text}</pre>
    </div>
  )
}

export default function DeployPanel({ comp }) {
  const [nodes, setNodes] = useState(null)
  const [form, setForm] = useState({ teams: 2, scoring_vmid: '', engine_node: '', team_node: '' })
  const [jobs, setJobs] = useState([])
  const [viewing, setViewing] = useState(null)
  const [confirm, setConfirm] = useState(false)
  const [err, setErr] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const refreshJobs = () => api.jobs(comp.id).then(setJobs).catch(() => {})
  useEffect(() => { api.nodes().then(setNodes).catch(() => {}); refreshJobs() }, [comp.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const running = jobs.find((j) => j.running)
  const start = async (fn) => {
    setErr(null)
    try { const j = await fn(); setViewing(j); refreshJobs() } catch (e) { setErr(e) }
  }
  const body = (plan_only) => ({
    teams: Number(form.teams), plan_only,
    scoring_vmid: form.scoring_vmid ? Number(form.scoring_vmid) : null,
    engine_node: form.engine_node, team_node: form.team_node,
  })

  return (
    <Card className="p-4">
      <h2 className="mb-3 text-lg font-semibold">Deploy</h2>
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <div className="space-y-3">
        <Field label="Teams"><input type="number" min="1" max="50" className={inputCls} value={form.teams} onChange={set('teams')} /></Field>
        <Field label="Nodes" hint={nodes?.multi ? 'Unpinned teams are placed by capacity-fill (nodes.json).' : 'Single node from .env — add nodes.json for multi-node.'}>
          {nodes?.multi ? (
            <div className="space-y-2">
              <select className={inputCls} value={form.engine_node} onChange={set('engine_node')}>
                <option value="">Engine node: auto</option>
                {nodes.nodes.map((n) => <option key={n.name} value={n.name}>Engine on {n.name}</option>)}
              </select>
              <input className={`${inputCls} font-mono`} placeholder="team pins: 101=zfs-193,102=hdd-150" value={form.team_node} onChange={set('team_node')} />
            </div>
          ) : (
            <div className="rounded-md bg-slate-50 px-2.5 py-1.5 font-mono text-xs text-slate-600">{nodes?.nodes?.[0]?.name || '…'}</div>
          )}
        </Field>
        <Field label="Scoring VMID" hint="A free vmid for the engine (preflight refuses collisions)">
          <input type="number" className={inputCls} value={form.scoring_vmid} onChange={set('scoring_vmid')} placeholder="e.g. 1010" />
        </Field>
        <div className="grid grid-cols-2 gap-2 pt-1">
          <Button disabled={!!running} onClick={() => start(() => api.deploy(comp.id, body(true)))}>Plan only</Button>
          <Button variant="primary" disabled={!!running} onClick={() => setConfirm(true)}>Deploy</Button>
          <Button className="col-span-2" disabled={!!running} onClick={() => start(() => api.verify(comp.id))}>Verify</Button>
        </div>
        <p className="text-xs text-slate-400">Practice runs belong in a fresh worktree (AGENTS.md). Teardown: <code>destroy-competition.py</code>.</p>
      </div>

      {jobs.length > 0 && (
        <div className="mt-4 border-t border-slate-200 pt-3">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Runs</h3>
          <ul className="space-y-1">
            {jobs.map((j) => (
              <li key={j.id}>
                <button onClick={() => setViewing(j)} className="flex w-full items-center justify-between rounded px-2 py-1 text-left text-sm hover:bg-slate-100">
                  <span className="capitalize">{j.action}</span>
                  <span className={`text-xs ${j.running ? 'text-indigo-600' : j.returncode === 0 ? 'text-emerald-600' : 'text-red-600'}`}>
                    {j.running ? 'running' : j.returncode === 0 ? 'ok' : `rc ${j.returncode}`}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <Modal open={confirm} onClose={() => setConfirm(false)} title={`Deploy ${comp.name}?`}>
        <p className="text-sm text-slate-700">
          This runs <code className="text-xs">create-competition.py --competition {comp.id} --teams {form.teams} --yes</code>
          {form.scoring_vmid && <> with scoring vmid {form.scoring_vmid}</>} and builds real VMs on Proxmox.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={() => setConfirm(false)}>Cancel</Button>
          <Button variant="primary" onClick={() => { setConfirm(false); start(() => api.deploy(comp.id, body(false))) }}>Deploy</Button>
        </div>
      </Modal>
      <Modal wide open={!!viewing} onClose={() => setViewing(null)} title={viewing ? `${viewing.action} — ${comp.id}` : ''}>
        {viewing && <JobLog job={viewing} onDone={refreshJobs} />}
      </Modal>
    </Card>
  )
}
