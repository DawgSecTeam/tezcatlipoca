import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import RFB from '@novnc/novnc/core/rfb.js'
import keysyms from '@novnc/novnc/core/input/keysymdef.js'
import { api, kindOf } from '../api'
import { Button, Modal, PlatformBadge, ThemeToggle } from '../components/ui'
import { Icon, Logo } from '../components/kit'

// One Proxmox console per tab: ask the portal for a one-shot relay, then noVNC over it.
// The portal never hands the browser a Proxmox credential — only the relay id and the VNC
// ticket (noVNC's RFB password), which is useless without the relay (portal/console.py).

const STATUS = {
  connecting: ['connecting…', 'text-muted'],
  connected: ['connected', 'text-accent'],
  lost: ['connection lost', 'text-danger'],
  closed: ['disconnected', 'text-muted'],
}

function TypeTextModal({ open, onClose, rfb }) {
  const [text, setText] = useState('')
  const [progress, setProgress] = useState(null)
  const send = async () => {
    const chars = [...text.replace(/\r\n/g, '\n')]
    for (let i = 0; i < chars.length; i++) {
      if (!rfb.current) break
      const ch = chars[i]
      const keysym = ch === '\n' ? 0xff0d : ch === '\t' ? 0xff09 : keysyms.lookup(ch.codePointAt(0))
      rfb.current.sendKey(keysym, null, true)
      rfb.current.sendKey(keysym, null, false)
      if (i % 20 === 0) {
        setProgress(`${i}/${chars.length}`)
        await new Promise((r) => setTimeout(r, 15))
      }
    }
    setProgress(null)
    onClose()
    rfb.current?.focus()
  }
  return (
    <Modal open={open} onClose={onClose} title="Type text" wide>
      <p className="mb-3 text-sm text-muted">The console has no clipboard, so this is sent as keystrokes into whatever has focus on the box. Plain ASCII; keep it to commands, not files.</p>
      <textarea className="field font-mono" rows={8} value={text} onChange={(e) => setText(e.target.value)} autoFocus placeholder="sudo systemctl status apache2" />
      <div className="mt-5 flex items-center justify-end gap-3">
        {progress && <span className="text-xs tabular-nums text-faint">{progress}</span>}
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!text || !!progress} onClick={send}><Icon name="play" />Type it</Button>
      </div>
    </Modal>
  )
}

export default function Console() {
  const { team, box } = useParams()
  const screen = useRef(null)
  const rfb = useRef(null)
  const [state, setState] = useState('connecting')
  const [refusal, setRefusal] = useState(null)
  const [info, setInfo] = useState(null)
  const [typing, setTyping] = useState(false)

  const connect = useCallback(async () => {
    rfb.current?.disconnect()
    rfb.current = null
    setRefusal(null)
    setState('connecting')
    let minted
    try {
      minted = await api.console(team, box)
    } catch (e) {
      if (e.status === 401) { window.location.href = '/'; return }
      setRefusal(e)
      setState('closed')
      return
    }
    setInfo(minted.box)
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    screen.current.replaceChildren()
    const r = new RFB(screen.current, `${scheme}://${window.location.host}${minted.ws_path}`, {
      credentials: { password: minted.password },
      shared: true,
      wsProtocols: ['binary'],
    })
    // PVE's vncwebsocket only completes the RFB 3.3 handshake (live-found 2026-10-04,
    // docs/reports/firewall-live-2026-10-04-report.md); noVNC has no public knob for the
    // version cap, so lower the internal one before the server greeting arrives.
    r._rfbMaxVersion = 3.3
    r.scaleViewport = true
    r.background = 'transparent'
    r.addEventListener('connect', () => { setState('connected'); r.focus() })
    r.addEventListener('disconnect', (e) => setState(e.detail.clean ? 'closed' : 'lost'))
    r.addEventListener('securityfailure', (e) => setRefusal(new Error(`console refused: ${e.detail.reason || 'authentication failed'}`)))
    rfb.current = r
  }, [team, box])

  useEffect(() => {
    connect()
    return () => { rfb.current?.disconnect(); rfb.current = null }
  }, [connect])
  useEffect(() => { document.title = `${box} · console` }, [box])

  const live = state === 'connected'
  const [label, tone] = STATUS[state]
  return (
    <div className="flex h-screen flex-col gap-5 p-6">
      <div className="tile flex flex-wrap items-center gap-3 !rounded-full px-4 py-2">
        <Link to="/" className="flex items-center gap-2 pr-2 text-sm text-muted transition hover:text-accent" title="All boxes">
          <Logo className="h-5 w-5" /><Icon name="back" className="h-3.5 w-3.5" />
        </Link>
        <span className="font-mono text-sm font-semibold">{box}</span>
        <span className="text-xs text-faint">{team}</span>
        {info && <PlatformBadge platform={kindOf(info)} />}
        <span data-status={refusal ? 'refused' : state} className={`flex items-center gap-1.5 text-xs font-semibold ${tone}`}>
          <span className="h-1.5 w-1.5 rounded-full bg-current" />{refusal ? 'refused' : label}
        </span>
        <span className="flex-1" />
        <Button variant="ghost" disabled={!live} onClick={() => rfb.current?.sendCtrlAltDel()}>Ctrl-Alt-Del</Button>
        <Button variant="ghost" disabled={!live} onClick={() => setTyping(true)}><Icon name="edit" />Type text…</Button>
        <Button variant="ghost" onClick={() => screen.current?.requestFullscreen?.()}>Fullscreen</Button>
        {!live && state !== 'connecting' && <Button variant="primary" onClick={connect}>Reconnect</Button>}
        <ThemeToggle />
      </div>
      {refusal && (
        <div className="flex items-start gap-3 rounded-2xl bg-danger-soft px-4 py-2.5 text-sm text-danger">
          <Icon name="warn" className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{refusal.status === 423 ? "Box access hasn't opened yet. Try again once the competition starts." : refusal.message}</span>
        </div>
      )}
      <div ref={screen} className="screen-well min-h-0 flex-1" />
      <TypeTextModal open={typing} onClose={() => setTyping(false)} rfb={rfb} />
    </div>
  )
}
