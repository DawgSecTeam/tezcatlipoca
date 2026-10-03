import { platformOf } from '../api'

// Auto-drawn from boxes.json: every team gets the same /24 (192.168.<team>.0/24, gateway .1,
// terraform/main.tf), so one generic team is drawn. An unmanaged pfSense box sits in-path
// between the engine and the team switch (docs/pfsense-inpath-2026-09-28.md).
const isFirewall = (b) => /pfsense|opnsense|firewall/i.test(b.template) || (b.unmanaged && /fw/i.test(b.name))

const DOT = { windows: 'var(--win)', linux: 'var(--lin)', firewall: 'var(--fw)', engine: 'var(--eng)' }

// Each node is a small floating tile: page-coloured face, bright rim, soft shadow, coloured dot.
function Node({ x, y, w, h, label, sub, kind, onClick, selected }) {
  return (
    <g onClick={onClick} className={onClick ? 'cursor-pointer [&:hover>rect]:[stroke:var(--accent)]' : ''} transform={`translate(${x - w / 2},${y - h / 2})`}>
      <rect width={w} height={h} rx="16" fill="var(--bg)" stroke={selected ? 'var(--accent)' : 'var(--edge)'} strokeWidth="2.5" filter="url(#tile-shadow)" style={{ transition: 'stroke .2s' }} />
      <circle cx="16" cy={h / 2} r="4" fill={DOT[kind]} />
      <text x={28} y={sub ? h / 2 - 2 : h / 2 + 4} fontSize="13" fontWeight="600" fill="var(--fg)">{label}</text>
      {sub && <text x={28} y={h / 2 + 14} fontSize="11" fill="var(--muted)">{sub}</text>}
    </g>
  )
}

export default function Topology({ boxes, onSelect, selected, height = 280 }) {
  const firewalls = boxes.filter(isFirewall)
  const hosts = boxes.filter((b) => !isFirewall(b)).sort((a, b) => a.last_octet - b.last_octet)

  const gap = 140
  const perRow = Math.max(1, Math.min(hosts.length, 6))
  const rows = Math.ceil(hosts.length / perRow) || 1
  const W = Math.max(600, perRow * gap + 60)
  const engineY = 44
  const fwY = 116
  const switchY = firewalls.length ? 184 : 124
  const hostY0 = switchY + 82
  const H = Math.max(height, hostY0 + rows * 78 + 10)
  const cx = W / 2

  const hostPos = hosts.map((b, i) => {
    const row = Math.floor(i / perRow)
    const inRow = Math.min(perRow, hosts.length - row * perRow)
    return { b, x: cx + ((i % perRow) - (inRow - 1) / 2) * gap, y: hostY0 + row * 78 }
  })

  if (!boxes.length) {
    return <div className="flex h-40 items-center justify-center text-sm text-faint">No boxes yet — add one to see the topology.</div>
  }

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: H }}>
      <defs>
        <filter id="tile-shadow" x="-30%" y="-40%" width="160%" height="200%">
          <feDropShadow dx="0" dy="6" stdDeviation="7" floodColor="#000" floodOpacity="0.16" />
        </filter>
      </defs>
      <line x1={cx} y1={engineY} x2={cx} y2={switchY} stroke="var(--line)" strokeWidth="2.5" />
      {hostPos.map(({ b, x, y }) => (
        <path key={b.name} d={`M${cx},${switchY} L${cx},${switchY + 34} L${x},${switchY + 34} L${x},${y}`} fill="none" stroke="var(--line)" strokeWidth="2" strokeLinejoin="round" />
      ))}

      <Node x={cx} y={engineY} w={170} h={46} label="Scoring engine" sub="Quotient" kind="engine" />
      {firewalls.map((b, i) => (
        <Node key={b.name} x={cx + (i - (firewalls.length - 1) / 2) * 160} y={fwY} w={136} h={46}
          label={b.name} sub={`.${b.last_octet} · in-path`} kind="firewall"
          onClick={onSelect && (() => onSelect(b))} selected={selected === b.name} />
      ))}
      <g transform={`translate(${cx - 100},${switchY - 16})`}>
        <rect width="200" height="32" rx="16" fill="var(--sunken)" />
        <text x="100" y="20.5" textAnchor="middle" fontSize="12" fill="var(--muted)" fontFamily="ui-monospace,monospace">192.168.&lt;team&gt;.0/24</text>
      </g>
      {hostPos.map(({ b, x, y }) => (
        <Node key={b.name} x={x} y={y} w={122} h={46} label={b.name} sub={`.${b.last_octet} · ${platformOf(b.template)}`}
          kind={platformOf(b.template)} onClick={onSelect && (() => onSelect(b))} selected={selected === b.name} />
      ))}
    </svg>
  )
}
