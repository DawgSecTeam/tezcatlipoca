import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useMatch, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { ErrorBanner, Spinner } from '../components/ui'

const CompCtx = createContext(null)
export const useComp = () => useContext(CompCtx)

function CompSwitcher({ current }) {
  const [comps, setComps] = useState([])
  const navigate = useNavigate()
  useEffect(() => { api.comps().then(setComps).catch(() => {}) }, [])
  return (
    <select value={current} onChange={(e) => navigate(`/c/${e.target.value}`)}
      className="w-full truncate rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm font-medium">
      {comps.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      {!comps.some((c) => c.id === current) && <option value={current}>{current}</option>}
    </select>
  )
}

const navCls = ({ isActive }) =>
  `block rounded-md px-3 py-1.5 text-sm ${isActive ? 'bg-indigo-50 font-semibold text-indigo-700' : 'text-slate-700 hover:bg-slate-100'}`

function Sidebar({ comp }) {
  const inBox = useMatch('/c/:id/boxes/:box/*')
  const boxName = inBox?.params.box
  const [boxesOpen, setBoxesOpen] = useState(true)
  const base = `/c/${comp.id}`

  return (
    <aside className="flex w-56 shrink-0 flex-col gap-3 border-r border-slate-200 bg-white p-3">
      <Link to="/" className="px-1 text-xs font-semibold uppercase tracking-widest text-slate-400 hover:text-indigo-600">tezcatlipoca</Link>
      <CompSwitcher current={comp.id} />
      {boxName ? (
        <nav className="space-y-1">
          <Link to={`${base}/boxes`} className="block px-3 py-1.5 text-sm text-slate-500 hover:text-indigo-600">‹ Boxes</Link>
          <div className="px-3 pt-2 text-xs font-semibold uppercase tracking-wide text-slate-400">{boxName}</div>
          <NavLink end to={`${base}/boxes/${boxName}`} className={navCls}>Box</NavLink>
          <NavLink to={`${base}/boxes/${boxName}/misconfigs`} className={navCls}>Misconfigs</NavLink>
          <NavLink to={`${base}/boxes/${boxName}/services`} className={navCls}>Services</NavLink>
        </nav>
      ) : (
        <nav className="space-y-1">
          <NavLink end to={base} className={navCls}>Overview</NavLink>
          <NavLink to={`${base}/injects`} className={navCls}>Injects</NavLink>
          <NavLink to={`${base}/packet`} className={navCls}>Packet</NavLink>
          <div className="flex items-center">
            <NavLink end to={`${base}/boxes`} className={(s) => `${navCls(s)} flex-1`}>Boxes</NavLink>
            <button onClick={() => setBoxesOpen(!boxesOpen)} className="px-2 text-slate-400 hover:text-slate-700">{boxesOpen ? '▾' : '▸'}</button>
          </div>
          {boxesOpen && comp.boxes.map((b) => (
            <NavLink key={b.name} to={`${base}/boxes/${b.name}`} className={({ isActive }) => `ml-3 block rounded px-3 py-1 text-sm ${isActive ? 'text-indigo-700' : 'text-slate-500 hover:text-slate-800'}`}>
              → {b.name}
            </NavLink>
          ))}
        </nav>
      )}
    </aside>
  )
}

export default function CompLayout() {
  const { id } = useParams()
  const [comp, setComp] = useState(null)
  const [err, setErr] = useState(null)
  const reload = useCallback(() => api.comp(id).then(setComp).catch(setErr), [id])
  useEffect(() => { setComp(null); setErr(null); reload() }, [reload])

  if (err) return <div className="p-8"><ErrorBanner error={err} /><Link className="text-indigo-600" to="/">← All competitions</Link></div>
  if (!comp) return <div className="flex h-screen items-center justify-center"><Spinner /></div>

  return (
    <CompCtx.Provider value={{ comp, reload, setComp }}>
      <div className="flex h-screen">
        <Sidebar comp={comp} />
        <main className="min-w-0 flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </CompCtx.Provider>
  )
}
