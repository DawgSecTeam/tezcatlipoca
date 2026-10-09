import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, consolePath, kindOf } from '../api'
import { Button, ErrorBanner, Field, PlatformBadge, Spinner, ThemeToggle, inputCls } from '../components/ui'
import { EmptyState, Icon, Logo, useConfirm, useToast } from '../components/kit'

// One page, three states: signed out (Quotient team/admin login), a team's boxes, or the white
// team's view of every team plus the access gate. /api/me decides which.

function TopBar({ me, onLogout }) {
  return (
    <div className="rise mb-12 flex items-center justify-between gap-4">
      <div className="flex min-w-0 items-center gap-2.5">
        <Logo className="h-7 w-7" />
        <span className="text-sm font-semibold tracking-tight">tezcatlipoca</span>
        {me?.event_name && <span className="truncate text-sm text-faint">· {me.event_name}</span>}
      </div>
      <div className="flex shrink-0 items-center gap-3">
        {me && (
          <span className="chip gap-1.5 bg-sunken text-muted" title={me.display ? `signed in as ${me.user} (${me.display})` : `signed in as ${me.user}`}>
            <span className="h-1.5 w-1.5 rounded-full bg-accent" />{me.user}{me.display && <span className="text-faint">· {me.display}</span>}
          </span>
        )}
        {me?.scoreboard_url && (
          <a className="btn" href={me.scoreboard_url} target="_blank" rel="noopener noreferrer"><Icon name="overview" />Scoreboard</a>
        )}
        <ThemeToggle />
        {me && <Button variant="ghost" onClick={onLogout}>Sign out</Button>}
      </div>
    </div>
  )
}

function Hero({ title, sub }) {
  return (
    <div className="rise rise-1 mb-10 text-center">
      <h1 className="display text-6xl">{title}</h1>
      {sub && <p className="mt-3 text-muted">{sub}</p>}
    </div>
  )
}

function Login({ onDone }) {
  const [form, setForm] = useState({ username: '', password: '', display: '' })
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const [event, setEvent] = useState(null)
  useEffect(() => { api.health().then((h) => setEvent(h.event_name || null)).catch(() => {}) }, [])
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setErr(null)
    try {
      await api.login(form.username.trim(), form.password, form.display.trim())
      onDone()
    } catch (e) { setErr(e) } finally { setBusy(false) }
  }
  return (
    <>
      <Hero title={event || 'Range portal'} sub="Sign in with your team's scoreboard login to reach your boxes" />
      <form onSubmit={submit} className="panel rise rise-2 mx-auto max-w-md space-y-4 p-7">
        <ErrorBanner error={err} />
        <Field label="Team">
          <input className={`${inputCls} font-mono`} value={form.username} onChange={set('username')} required autoFocus autoComplete="username" placeholder="team1" />
        </Field>
        <Field label="Password">
          <input className={inputCls} type="password" value={form.password} onChange={set('password')} required autoComplete="current-password" />
        </Field>
        <Field label="Your name" hint="Optional. Your team shares one login; this is how the access log tells you apart.">
          <input className={inputCls} value={form.display} onChange={set('display')} maxLength={40} placeholder="Ada" />
        </Field>
        <div className="flex justify-end pt-2">
          <Button variant="primary" disabled={busy}>{busy ? <Spinner /> : <Icon name="chevron" />}Sign in</Button>
        </div>
      </form>
    </>
  )
}

function BoxCard({ team, box, enabled, i = 0 }) {
  const kind = kindOf(box)
  return (
    <div className={`panel rise flex flex-col p-6 ${i < 4 ? `rise-${i + 1}` : ''}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate font-mono text-xl font-semibold tracking-tight">{box.name}</h3>
          <p className="mt-0.5 font-mono text-xs text-faint">{box.ip}</p>
        </div>
        <PlatformBadge platform={kind} />
      </div>
      <div className="mt-auto flex items-center justify-between gap-3 pt-6">
        <span className="text-xs text-faint">{kind === 'windows' ? 'Ctrl-Alt-Del from the toolbar' : kind === 'firewall' ? 'the appliance console menu' : 'log in with your box credentials'}</span>
        {enabled
          ? <Link to={consolePath(team, box.name)} target="_blank" rel="noopener" className="btn btn-primary"><Icon name="terminal" />Console</Link>
          : <span className="btn pointer-events-none opacity-45"><Icon name="terminal" />Console</span>}
      </div>
    </div>
  )
}

function TeamView({ me }) {
  const open = me.access.open
  const usable = open && me.console
  return (
    <>
      <Hero title={me.team} sub={`${me.boxes.length} ${me.boxes.length === 1 ? 'box' : 'boxes'} · ${open ? 'access open' : 'access opens at the start'}`} />
      {!open && (
        <div className="rise rise-2 mx-auto mb-10 flex max-w-2xl items-center gap-3 rounded-2xl bg-accent-soft px-5 py-3 text-sm text-accent">
          <Icon name="clock" className="h-5 w-5 shrink-0" />
          Box access hasn't opened yet. Your consoles unlock when the competition starts. This page updates on its own.
        </div>
      )}
      {open && !me.console && (
        <div className="mx-auto mb-10 max-w-2xl"><ErrorBanner error="Consoles aren't configured for this competition. Tell the organizers." /></div>
      )}
      <div className="grid grid-cols-1 gap-8 md:grid-cols-2">
        {me.boxes.map((b, i) => <BoxCard key={b.name} team={me.team} box={b} enabled={usable} i={i} />)}
      </div>
      {!me.boxes.length && <EmptyState icon="boxes" title="No boxes">This team has no boxes in this competition.</EmptyState>}
    </>
  )
}

function AccessLog() {
  const [entries, setEntries] = useState(null)
  const load = useCallback(() => api.log().then((d) => setEntries(d.entries)).catch(() => setEntries([])), [])
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [load])
  const rows = (entries || []).slice(-80).reverse()
  return (
    <section className="panel rise mt-12 p-6">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="display text-2xl">Access log</h2>
        <Button variant="ghost" onClick={load}>Refresh</Button>
      </div>
      {!entries ? <Spinner /> : !rows.length ? <p className="text-sm text-faint">Nothing yet.</p> : (
        <div className="max-h-96 overflow-auto rounded-2xl bg-sunken p-4 font-mono text-[12px] leading-relaxed text-muted shadow-inset">
          {rows.map((e, i) => {
            const { at, event, ...rest } = e
            return (
              <div key={i} className="whitespace-pre-wrap break-all">
                <span className="text-faint">{at}</span>{'  '}
                <span className={/failed|refused|error|blocked/.test(event) ? 'text-danger' : 'text-fg'}>{event}</span>{'  '}
                {Object.entries(rest).map(([k, v]) => `${k}=${v}`).join(' ')}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

function AdminView({ me, reload }) {
  const confirm = useConfirm()
  const toast = useToast()
  const open = me.access.open
  const flip = async () => {
    const ok = await confirm({
      title: open ? 'Close box access?' : 'Open box access?',
      body: open
        ? 'Every team loses the Console button immediately. Consoles already open stay connected.'
        : 'Every team can open consoles on its boxes from now on.',
      confirm: open ? 'Close access' : 'Open access', danger: open,
    })
    if (!ok) return
    try { await api.setAccess(!open); toast(open ? 'Access closed' : 'Access open'); reload() } catch (e) { toast(e.message, 'error') }
  }
  const teams = Object.entries(me.teams)
  return (
    <>
      <Hero title="White team" sub={`${teams.length} teams · ${teams.reduce((n, [, b]) => n + b.length, 0)} boxes`} />
      <div className="panel rise rise-2 mx-auto mb-12 flex max-w-2xl flex-wrap items-center justify-between gap-4 p-6">
        <div>
          <div className="label !mb-1">Team box access</div>
          <div className="flex items-center gap-2 text-sm">
            <span className={`h-2 w-2 rounded-full ${open ? 'bg-accent' : 'bg-danger'}`} />
            <span className="font-semibold">{open ? 'Open' : 'Closed'}</span>
            {me.access.opened_by && <span className="text-faint">· last changed by {me.access.opened_by}{me.access.opened_at ? ` at ${me.access.opened_at}` : ''}</span>}
          </div>
          <p className="mt-1 text-xs text-faint">White team consoles always work, open or closed.</p>
        </div>
        <Button variant={open ? 'danger' : 'primary'} onClick={flip}>{open ? 'Close access' : 'Open access'}</Button>
      </div>
      {!me.console && <ErrorBanner error="No console token on any node: logins work, consoles don't. See docs/portal.md → Operating it." />}
      {teams.map(([team, boxes]) => (
        <section key={team} className="mb-12">
          <h2 className="display mb-5 text-3xl">{team}</h2>
          <div className="grid grid-cols-1 gap-8 md:grid-cols-2">
            {boxes.map((b, i) => <BoxCard key={b.name} team={team} box={b} enabled={me.console} i={i} />)}
          </div>
        </section>
      ))}
      <AccessLog />
    </>
  )
}

export default function Home() {
  const [me, setMe] = useState(undefined)   // undefined = loading, null = signed out
  const [err, setErr] = useState(null)
  const reload = useCallback(() => api.me().then(setMe).catch((e) => (e.status === 401 ? setMe(null) : setErr(e))), [])
  useEffect(() => { reload() }, [reload])
  // A team waiting at the gate sees it open without reloading.
  useEffect(() => {
    if (me?.role !== 'team' || me.access.open) return
    const t = setInterval(reload, 15000)
    return () => clearInterval(t)
  }, [me, reload])
  useEffect(() => { document.title = me?.event_name ? `${me.event_name} · portal` : 'Range portal' }, [me])
  const logout = async () => { await api.logout().catch(() => {}); setMe(null) }

  return (
    <div className="mx-auto max-w-6xl px-8 py-10">
      <TopBar me={me} onLogout={logout} />
      <ErrorBanner error={err} onClose={() => setErr(null)} />
      {me === undefined && <div className="flex justify-center py-20"><Spinner /></div>}
      {me === null && <Login onDone={reload} />}
      {me?.role === 'team' && <TeamView me={me} />}
      {me?.role === 'admin' && <AdminView me={me} reload={reload} />}
    </div>
  )
}
