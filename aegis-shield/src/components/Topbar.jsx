// src/components/Topbar.jsx
import { useEffect, useState } from 'react'
import { useStore, selectActiveDrones, selectOpenAlerts, useUnits, useToggleUnits } from '../store/useStore'

const css = `
.cmd {
  grid-area: topbar;
  display: flex; align-items: center; gap: 16px;
  height: 48px; padding: 0 16px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--bg-border);
  box-shadow: inset 0 1px 0 #ffffff08;
  position: relative; z-index: 100; flex-shrink: 0;
}
.cmd-brand { display: flex; align-items: center; gap: 9px; flex-shrink: 0; }
.cmd-brand svg { width: 22px; height: 22px; }
.cmd-word {
  font-family: var(--cond); font-weight: 700; font-size: 19px;
  letter-spacing: .2em; color: var(--text-bright); line-height: 1;
}
.cmd-word span {
  color: var(--olive); font-weight: 600; font-size: 11px;
  letter-spacing: .2em; margin-left: 7px;
}
.cmd-sub {
  font-family: var(--cond); font-size: 9px; letter-spacing: .18em;
  text-transform: uppercase; color: var(--text-muted); margin-top: 3px;
}
.cmd-sep { width: 1px; height: 22px; background: var(--bg-border); flex-shrink: 0; }
.cmd-chips { display: flex; align-items: center; gap: 7px; flex-shrink: 0; }
.chip {
  display: flex; align-items: center; gap: 7px; height: 28px; padding: 0 10px;
  border: 1px solid var(--bg-border); border-radius: 2px; background: #0f1516;
}
.chip .k {
  font-family: var(--cond); font-weight: 600; font-size: 10px; letter-spacing: .13em;
  text-transform: uppercase; color: var(--text-secondary);
}
.chip .v { font-family: var(--mono); font-weight: 600; font-size: 13px; color: var(--text-primary); }
.chip .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); flex: none; }
.chip.ok .dot { background: var(--signal); box-shadow: 0 0 6px var(--signal); }
.chip.ok .v { color: var(--text-primary); }
.chip.warn { border-color: var(--threat-med-border); }
.chip.warn .v, .chip.warn .dot { color: var(--threat-med); background: var(--threat-med); }
.chip.warn .k { color: var(--threat-med); }
.chip.danger { border-color: var(--threat-high-border); background: var(--threat-high-bg); }
.chip.danger .v { color: var(--threat-high); }
.chip.danger .k { color: var(--threat-high); }
.chip.danger .dot { background: var(--threat-high); box-shadow: 0 0 6px var(--threat-high); animation: cmd-beat 1.4s infinite; }
.cmd-spacer { flex: 1; }
.cmd-link {
  display: flex; align-items: center; gap: 7px; flex-shrink: 0;
  font-family: var(--cond); font-weight: 600; font-size: 11px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--text-secondary);
}
.cmd-link .d { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.cmd-link.connected .d { background: var(--signal); box-shadow: 0 0 6px var(--signal); }
.cmd-link.connecting .d { background: var(--threat-med); animation: cmd-beat 1s infinite; }
.cmd-link.disconnected { color: var(--threat-high); }
.cmd-link.disconnected .d { background: var(--threat-high); }
.cmd-units {
  flex-shrink: 0; height: 26px; padding: 0 10px; cursor: pointer;
  background: transparent; border: 1px solid var(--bg-border); border-radius: 2px;
  font-family: var(--cond); font-weight: 600; font-size: 11px; letter-spacing: .1em;
  text-transform: uppercase; transition: color .15s, border-color .15s;
}
.cmd-units:hover { border-color: var(--steel-blue); }
.cmd-clock { display: flex; flex-direction: column; align-items: flex-end; line-height: 1.1; flex-shrink: 0; }
.cmd-clock .z { font-family: var(--mono); font-weight: 600; font-size: 16px; color: var(--text-bright); letter-spacing: .02em; }
.cmd-clock .z b { color: var(--olive); font-weight: 600; }
.cmd-clock .l { font-family: var(--cond); font-size: 10px; letter-spacing: .16em; text-transform: uppercase; color: var(--text-muted); }
@keyframes cmd-beat { 0%,100% { opacity: .4 } 50% { opacity: 1 } }
@media (prefers-reduced-motion: reduce) { .chip.danger .dot, .cmd-link .d { animation: none } }
@media (max-width: 1180px) { .cmd-sub, .cmd-link { display: none } .cmd { gap: 12px } }
`

function Chip({ label, value, tone }) {
  return (
    <div className={`chip ${tone || ''}`}>
      {(tone === 'ok' || tone === 'danger') && <span className="dot" />}
      <span className="k">{label}</span><span className="v">{value}</span>
    </div>
  )
}

export default function Topbar() {
  const wsStatus    = useStore(s => s.wsStatus)
  const drones      = useStore(selectActiveDrones)
  const nodes       = useStore(s => s.nodes)
  const alerts      = useStore(selectOpenAlerts)
  const detRate     = useStore(s => s.detectionRate)
  const imperial    = useUnits()
  const toggleUnits = useToggleUnits()
  const [now, setNow] = useState(new Date())

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  const onlineNodes = Object.values(nodes).filter(n => n.status === 'online').length
  const totalNodes  = Object.values(nodes).length
  const alertCount  = alerts.filter(a => a.level === 'high').length
  const noRidCount  = drones.filter(d => !d.has_valid_rid).length

  const p = n => n.toString().padStart(2, '0')
  const z = `${p(now.getUTCHours())}:${p(now.getUTCMinutes())}:${p(now.getUTCSeconds())}`
  const local = `${p(now.getHours())}:${p(now.getMinutes())}`

  const nodeTone = totalNodes === 0 ? 'danger' : onlineNodes === totalNodes ? 'ok' : onlineNodes > 0 ? 'warn' : 'danger'

  return (
    <>
      <style>{css}</style>
      <header className="cmd">
        <div className="cmd-brand">
          <svg viewBox="0 0 24 24" fill="none" stroke="var(--olive)" strokeWidth="1.7" strokeLinejoin="round">
            <path d="M12 2 4 5v6c0 5 3.5 8.5 8 11 4.5-2.5 8-6 8-11V5l-8-3Z"/>
            <path d="M12 7v10" strokeWidth="1.3"/><path d="M8.5 9.5 12 7l3.5 2.5" strokeWidth="1.3"/>
          </svg>
          <div>
            <div className="cmd-word">KESTREL<span>AIRSPACE</span></div>
            <div className="cmd-sub">Remote ID Detection Network</div>
          </div>
        </div>

        <div className="cmd-sep" />

        <div className="cmd-chips">
          <Chip label="Nodes" value={`${onlineNodes}/${totalNodes || '—'}`} tone={nodeTone} />
          <Chip label="Drones" value={drones.length} />
          <Chip label="No RID" value={noRidCount} tone={noRidCount > 0 ? 'danger' : 'ok'} />
          <Chip label="Alerts" value={alertCount} tone={alertCount > 0 ? 'danger' : ''} />
          <Chip label="Det/min" value={detRate.toFixed(1)} />
        </div>

        <div className="cmd-spacer" />

        <div className={`cmd-link ${wsStatus}`}>
          <span className="d" />{wsStatus === 'connected' ? 'Secure' : wsStatus}
        </div>

        <button className="cmd-units" onClick={toggleUnits} title="Toggle metric / imperial"
          style={{ color: imperial ? 'var(--olive)' : 'var(--steel-blue)' }}>
          {imperial ? 'Imperial' : 'Metric'}
        </button>

        <div className="cmd-sep" />

        <div className="cmd-clock">
          <span className="z">{z}<b>Z</b></span>
          <span className="l">{local} Local</span>
        </div>
      </header>
    </>
  )
}
