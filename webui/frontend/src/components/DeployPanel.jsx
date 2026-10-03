import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Button, Card, ErrorBanner, Field, Modal, Spinner, inputCls } from './ui'
import { Icon, Stepper, fmtMem, timeAgo, useToast } from './kit'

const ACTION_LABEL = { plan: 'Dry run', deploy: 'Deploy', verify: 'Verify' }

function JobLog({ job, onDone }) {
  const [text, setText] = useState('')
  const [state, setState] = useState(job)
  const [follow, setFollow] = useState(true)
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

  useEffect(() => { if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [text, follow])

  return (
    <div>
      <div className="mb-3 flex items-center gap-3 text-sm">
        {state.running ? <><Spinner /><span className="text-muted">Running…</span></>
          : <span className={`chip px-3 py-1 ${state.returncode === 0 ? 'bg-accent-soft text-accent' : 'bg-danger-soft text-danger'}`}>
              {state.returncode === 0 ? 'Finished' : `Failed · exit ${state.returncode}`}
            </span>}
        <code className="truncate text-xs text-faint">{state.cmd}</code>
        <label className="ml-auto flex shrink-0 items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-[var(--accent)]" /> Follow
        </label>
      </div>
      <pre ref={pre} className="h-[60vh] overflow-auto rounded-2xl bg-sunken p-4 font-mono text-xs leading-relaxed text-fg shadow-inset">{text || ' '}</pre>
    </div>
  )
}

function Footprint({ comp, teams }) {
  const n = Number(teams) || 0
  const managed = comp.boxes
  const sum = (k) => managed.reduce((a, b) => a + (Number(b[k]) || 0), 0)
  const rows = [
    ['VMs', `${n * managed.length}`, `${n} × ${managed.length} boxes, + engine`],
    ['RAM', fmtMem(n * sum('memory_mb')), 'team boxes only'],
    ['vCPU', `${n * sum('cpu')}`, ''],
  ]
  return (
    <div className="grid grid-cols-3 gap-2 rounded-2xl bg-sunken p-3 shadow-inset">
      {rows.map(([k, v, hint]) => (
        <div key={k} title={hint} className="text-center">
          <div className="text-lg font-semibold tabular-nums">{v}</div>
          <div className="text-[10px] font-semibold uppercase tracking-wider text-faint">{k}</div>
        </div>
      ))}
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
  const toast = useToast()
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const refreshJobs = () => api.jobs(comp.id).then(setJobs).catch(() => {})
  useEffect(() => { api.nodes().then(setNodes).catch(() => {}); refreshJobs() }, [comp.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const running = jobs.find((j) => j.running)
  const noBoxes = comp.boxes.length === 0
  const start = async (fn) => {
    setErr(null)
    try { const j = await fn(); setViewing(j); refreshJobs() } catch (e) { setErr(e) }
  }
  const body = (plan_only) => ({
    teams: Number(form.teams), plan_only,
    scoring_vmid: form.scoring_vmid ? Number(form.scoring_vmid) : null,
    engine_node: form.engine_node, team_node: form.team_node,
  })
  const onDone = (r) => {
    refreshJobs()
    toast(`${ACTION_LABEL[r.action]} ${r.returncode === 0 ? 'finished' : `failed (exit ${r.returncode})`}`, r.returncode === 0 ? 'ok' : 'error')
  }

  return (
    <Card className="rise rise-2 p-6">
      <div className="mb-5 flex items-center justify-between">
        <h2 className="display text-2xl">Deploy</h2>
        {comp.deployed && <span className="chip bg-accent-soft px-2.5 py-1 text-accent" title=".deploy_state.json exists for this competition"><span className="mr-1.5 h-1.5 w-1.5 rounded-full bg-accent" />has a deploy</span>}
      </div>
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      <div className="space-y-5">
        <Field label="Teams"><Stepper value={form.teams} onChange={(v) => setForm({ ...form, teams: v })} /></Field>
        <Footprint comp={comp} teams={form.teams} />
        <Field label="Proxmox nodes" hint={nodes?.multi ? 'Unpinned teams are placed by capacity-fill (nodes.json).' : 'Single node from .env. Add nodes.json to spread teams across hosts.'}>
          {nodes?.multi ? (
            <div className="space-y-2">
              <select className={inputCls} value={form.engine_node} onChange={set('engine_node')}>
                <option value="">Engine: auto (node with most teams)</option>
                {nodes.nodes.map((n) => <option key={n.name} value={n.name}>Engine on {n.name}</option>)}
              </select>
              <input className={`${inputCls} font-mono`} placeholder="Team pins, e.g. 101=zfs-193,102=hdd-150" value={form.team_node} onChange={set('team_node')} />
            </div>
          ) : (
            <div className="field flex items-center gap-2 font-mono text-xs text-muted"><Icon name="net" className="h-3.5 w-3.5" />{nodes?.nodes?.[0]?.name || '…'}</div>
          )}
        </Field>
        <Field label="Engine VM ID" hint="A free Proxmox VMID for the scoring engine. Preflight refuses collisions.">
          <input inputMode="numeric" className={`${inputCls} font-mono`} value={form.scoring_vmid} onChange={(e) => setForm({ ...form, scoring_vmid: e.target.value.replace(/\D/g, '') })} placeholder="auto" />
        </Field>
        <div className="space-y-3 pt-1">
          <Button variant="primary" className="w-full !py-2.5" disabled={!!running || noBoxes} onClick={() => setConfirm(true)}>
            <Icon name="rocket" />Deploy {form.teams || 0} team{Number(form.teams) === 1 ? '' : 's'}
          </Button>
          <div className="grid grid-cols-2 gap-3">
            <Button disabled={!!running || noBoxes} onClick={() => start(() => api.deploy(comp.id, body(true)))} title="create-competition.py --plan-only: prints the plan, touches nothing">
              <Icon name="play" />Dry run
            </Button>
            <Button disabled={!!running} onClick={() => start(() => api.verify(comp.id))} title="verify-competition.py against the running range">
              <Icon name="check" />Verify
            </Button>
          </div>
          {noBoxes && <p className="text-center text-xs text-faint">Add a box before deploying.</p>}
        </div>
      </div>

      <div className="mt-6">
        <h3 className="label">Recent runs</h3>
        {jobs.length === 0
          ? <p className="text-xs text-faint">No runs since the server started.</p>
          : (
            <ul className="space-y-2">
              {jobs.slice(0, 6).map((j) => (
                <li key={j.id}>
                  <button onClick={() => setViewing(j)} className="tile tile-hover flex w-full items-center gap-3 px-4 py-2 text-left text-sm">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${j.running ? 'animate-pulse bg-accent' : j.returncode === 0 ? 'bg-accent' : 'bg-danger'}`} />
                    <span className="font-medium">{ACTION_LABEL[j.action] || j.action}</span>
                    <span className="ml-auto text-xs text-faint">{j.running ? 'running' : timeAgo(j.started)}</span>
                    <Icon name="terminal" className="h-3.5 w-3.5 text-faint" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        <p className="mt-4 text-[11px] leading-relaxed text-faint">
          Practice runs belong in a fresh worktree (AGENTS.md). Tear down with <code className="font-mono">destroy-competition.py</code>.
        </p>
      </div>

      <Modal open={confirm} onClose={() => setConfirm(false)} title={`Deploy ${comp.name}?`}>
        <p className="text-sm leading-relaxed text-muted">
          This builds real VMs on Proxmox: <b className="text-fg">{form.teams} team{Number(form.teams) === 1 ? '' : 's'}</b> × {comp.boxes.length} boxes, plus the scoring engine
          {form.scoring_vmid && <> (VMID <span className="font-mono">{form.scoring_vmid}</span>)</>}.
        </p>
        <div className="mt-4"><Footprint comp={comp} teams={form.teams} /></div>
        <code className="mt-4 block rounded-xl bg-sunken p-3 font-mono text-[11px] text-muted shadow-inset">
          create-competition.py --competition {comp.id} --teams {form.teams} --yes{form.scoring_vmid && ` --scoring-vmid ${form.scoring_vmid}`}
        </code>
        <div className="mt-6 flex justify-end gap-3">
          <Button onClick={() => setConfirm(false)}>Cancel</Button>
          <Button variant="primary" autoFocus onClick={() => { setConfirm(false); start(() => api.deploy(comp.id, body(false))) }}><Icon name="rocket" />Deploy</Button>
        </div>
      </Modal>
      <Modal wide open={!!viewing} onClose={() => setViewing(null)} title={viewing ? `${ACTION_LABEL[viewing.action] || viewing.action} · ${comp.name}` : ''}>
        {viewing && <JobLog job={viewing} onDone={onDone} />}
      </Modal>
    </Card>
  )
}
