import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useMatch, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { ErrorBanner, Spinner, ThemeToggle } from '../components/ui'

const CompCtx = createContext(null)
export const useComp = () => useContext(CompCtx)

function CompSwitcher({ current }) {
  const [comps, setComps] = useState([])
  const navigate = useNavigate()
  useEffect(() => { api.comps().then(setComps).catch(() => {}) }, [])
  return (
    <div className="relative">
      <select value={current} onChange={(e) => navigate(`/c/${e.target.value}`)}
        className="field cursor-pointer appearance-none truncate rounded-full pr-9 font-semibold">
        {comps.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        {!comps.some((c) => c.id === current) && <option value={current}>{current}</option>}
      </select>
      <span className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2 text-xs text-faint">⌃⌄</span>
    </div>
  )
}

const navCls = ({ isActive }) =>
  `block rounded-full px-4 py-2 text-sm transition ${isActive ? 'bg-accent-soft font-semibold text-accent' : 'text-muted hover:bg-sunken hover:text-fg'}`

function Sidebar({ comp }) {
  const inBox = useMatch('/c/:id/boxes/:box/*')
  const boxName = inBox?.params.box
  const [boxesOpen, setBoxesOpen] = useState(true)
  const base = `/c/${comp.id}`

  return (
    <aside className="panel flex h-full w-60 flex-col gap-4 p-4">
      <Link to="/" className="px-2 pt-1 text-[11px] font-bold uppercase tracking-[0.2em] text-faint hover:text-accent">tezcatlipoca</Link>
      <CompSwitcher current={comp.id} />
      {boxName ? (
        <nav className="space-y-1">
          <Link to={`${base}/boxes`} className="block px-4 py-2 text-sm text-muted hover:text-accent">‹ Boxes</Link>
          <div className="label px-4 pt-2">{boxName}</div>
          <NavLink end to={`${base}/boxes/${boxName}`} className={navCls}>Box</NavLink>
          <NavLink to={`${base}/boxes/${boxName}/misconfigs`} className={navCls}>Misconfigs</NavLink>
          <NavLink to={`${base}/boxes/${boxName}/services`} className={navCls}>Services</NavLink>
        </nav>
      ) : (
        <nav className="min-h-0 flex-1 space-y-1 overflow-auto">
          <NavLink end to={base} className={navCls}>Overview</NavLink>
          <NavLink to={`${base}/injects`} className={navCls}>Injects</NavLink>
          <NavLink to={`${base}/packet`} className={navCls}>Packet</NavLink>
          <div className="flex items-center">
            <NavLink end to={`${base}/boxes`} className={(s) => `${navCls(s)} flex-1`}>Boxes</NavLink>
            <button onClick={() => setBoxesOpen(!boxesOpen)} className="px-3 text-faint hover:text-fg">{boxesOpen ? '▾' : '▸'}</button>
          </div>
          {boxesOpen && comp.boxes.map((b) => (
            <NavLink key={b.name} to={`${base}/boxes/${b.name}`} className={({ isActive }) => `ml-4 block rounded-full px-4 py-1.5 font-mono text-[13px] ${isActive ? 'text-accent' : 'text-faint hover:text-fg'}`}>
              → {b.name}
            </NavLink>
          ))}
        </nav>
      )}
      <div className="mt-auto flex justify-end"><ThemeToggle /></div>
    </aside>
  )
}

export default function CompLayout() {
  const { id } = useParams()
  const [comp, setComp] = useState(null)
  const [err, setErr] = useState(null)
  const reload = useCallback(() => api.comp(id).then(setComp).catch(setErr), [id])
  useEffect(() => { setComp(null); setErr(null); reload() }, [reload])

  if (err) return <div className="p-10"><ErrorBanner error={err} /><Link className="text-accent" to="/">← All competitions</Link></div>
  if (!comp) return <div className="flex h-screen items-center justify-center"><Spinner /></div>

  return (
    <CompCtx.Provider value={{ comp, reload, setComp }}>
      <div className="flex h-screen">
        <div className="shrink-0 p-6 pr-0"><Sidebar comp={comp} /></div>
        {/* Padding lives inside the scroller so the floating panels' halos are never clipped. */}
        <main className="min-w-0 flex-1 overflow-auto p-6 pl-8">
          <Outlet />
        </main>
      </div>
    </CompCtx.Provider>
  )
}
