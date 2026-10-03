import { platformOf } from '../api'

// Auto-drawn from boxes.json: every team gets the same /24 (192.168.<team>.0/24, gateway .1,
// terraform/main.tf), so one generic team is drawn. An unmanaged pfSense box sits in-path
// between the engine and the team switch (docs/pfsense-inpath-2026-09-28.md).
const isFirewall = (b) => /pfsense|opnsense|firewall/i.test(b.template) || (b.unmanaged && /fw/i.test(b.name))

const COLORS = {
  windows: { fill: '#e0f2fe', stroke: '#0284c7' },
  linux: { fill: '#fef3c7', stroke: '#d97706' },
  firewall: { fill: '#fee2e2', stroke: '#dc2626' },
}

function Node({ x, y, w, h, label, sub, color, onClick, selected }) {
  return (
    <g onClick={onClick} className={onClick ? 'cursor-pointer' : ''} transform={`translate(${x - w / 2},${y - h / 2})`}>
      <rect width={w} height={h} rx="8" fill={color.fill} stroke={color.stroke} strokeWidth={selected ? 3 : 1.5} className={onClick ? 'transition hover:opacity-80' : ''} />
      <text x={w / 2} y={sub ? h / 2 - 3 : h / 2 + 4} textAnchor="middle" fontSize="13" fontWeight="600" fill="#0f172a">{label}</text>
      {sub && <text x={w / 2} y={h / 2 + 13} textAnchor="middle" fontSize="11" fill="#475569">{sub}</text>}
    </g>
  )
}

export default function Topology({ boxes, onSelect, selected, height = 280 }) {
  const firewalls = boxes.filter(isFirewall)
  const hosts = boxes.filter((b) => !isFirewall(b)).sort((a, b) => a.last_octet - b.last_octet)

  const perRow = Math.max(1, Math.min(hosts.length, 6))
  const rows = Math.ceil(hosts.length / perRow) || 1
  const W = Math.max(560, perRow * 130 + 40)
  const engineY = 36
  const fwY = 100
  const switchY = firewalls.length ? 164 : 110
  const hostY0 = switchY + 74
  const H = Math.max(height, hostY0 + rows * 70)
  const cx = W / 2

  const hostPos = hosts.map((b, i) => {
    const row = Math.floor(i / perRow)
    const inRow = Math.min(perRow, hosts.length - row * perRow)
    const col = i % perRow
    return { b, x: cx + (col - (inRow - 1) / 2) * 130, y: hostY0 + row * 70 }
  })

  if (!boxes.length) {
    return <div className="flex h-40 items-center justify-center text-sm text-slate-400">No boxes yet — add one to see the topology.</div>
  }

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: H }}>
      {/* links */}
      <line x1={cx} y1={engineY} x2={cx} y2={switchY} stroke="#94a3b8" strokeWidth="2" />
      {hostPos.map(({ b, x, y }) => (
        <path key={b.name} d={`M${cx},${switchY} L${cx},${switchY + 30} L${x},${switchY + 30} L${x},${y}`} fill="none" stroke="#94a3b8" strokeWidth="1.5" />
      ))}

      <Node x={cx} y={engineY} w={170} h={40} label="Scoring engine" sub="Quotient" color={{ fill: '#ede9fe', stroke: '#7c3aed' }} />
      {firewalls.map((b, i) => (
        <Node key={b.name} x={cx + (i - (firewalls.length - 1) / 2) * 150} y={fwY} w={130} h={44}
          label={b.name} sub={`.${b.last_octet} · in-path`} color={COLORS.firewall}
          onClick={onSelect && (() => onSelect(b))} selected={selected === b.name} />
      ))}
      <g transform={`translate(${cx - 95},${switchY - 15})`}>
        <rect width="190" height="30" rx="15" fill="#f1f5f9" stroke="#64748b" strokeWidth="1.5" />
        <text x="95" y="19" textAnchor="middle" fontSize="12" fill="#334155" fontFamily="ui-monospace,monospace">192.168.&lt;team&gt;.0/24</text>
      </g>
      {hostPos.map(({ b, x, y }) => (
        <Node key={b.name} x={x} y={y} w={116} h={44} label={b.name} sub={`.${b.last_octet} · ${platformOf(b.template)}`}
          color={COLORS[platformOf(b.template)]} onClick={onSelect && (() => onSelect(b))} selected={selected === b.name} />
      ))}
    </svg>
  )
}
