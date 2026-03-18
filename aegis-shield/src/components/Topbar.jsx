// src/components/Topbar.jsx
import { useCallback, useEffect, useState } from 'react'
import { useStore, selectActiveDrones, selectOpenAlerts, useUnits, useToggleUnits } from '../store/useStore'

const css = `
.topbar {
  grid-area: topbar;
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 20px;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  height: 48px;
  position: relative;
  z-index: 100;
  flex-shrink: 0;
}
.topbar::before {
  content: '';
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--amber-dim), transparent);
}
.tb-logo {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.tb-logo-hex {
  width: 26px; height: 26px;
  position: relative;
  display: flex; align-items: center; justify-content: center;
}
.tb-logo-hex svg { width: 100%; height: 100%; }
.tb-logo-text {
  font-family: var(--cond);
  font-weight: 900;
  font-size: 17px;
  letter-spacing: 4px;
  text-transform: uppercase;
  color: var(--amber);
  text-shadow: 0 0 12px var(--amber-dim);
}
.tb-logo-sub {
  font-family: var(--mono);
  font-size: 8px;
  color: var(--muted);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-top: 1px;
}
.tb-divider {
  width: 1px;
  height: 28px;
  background: var(--border);
  flex-shrink: 0;
}
.tb-stats {
  display: flex;
  gap: 20px;
  flex: 1;
}
.tb-stat {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.tb-stat-label {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 1.5px;
  color: var(--muted);
  text-transform: uppercase;
}
.tb-stat-val {
  font-family: var(--mono);
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
  line-height: 1;
}
.tb-stat-val.amber  { color: var(--amber); text-shadow: 0 0 8px var(--amber-dim); }
.tb-stat-val.green  { color: var(--phosphor); text-shadow: 0 0 8px var(--phosphor-dim); }
.tb-stat-val.danger { color: var(--danger); text-shadow: 0 0 8px var(--danger-dim); animation: pulse-danger 1.5s infinite; }
.tb-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text-dim);
  flex-shrink: 0;
}
.tb-status-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-connected    { background: var(--phosphor); animation: pulse-green 2s infinite; box-shadow: 0 0 6px var(--phosphor); }
.dot-connecting   { background: var(--amber); animation: pulse-amber 1s infinite; }
.dot-disconnected { background: var(--danger); }
.tb-time {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--text-dim);
  letter-spacing: 1px;
  flex-shrink: 0;
}
.tb-algo {
  display: flex;
  align-items: center;
  gap: 5px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 3px 8px;
  cursor: pointer;
  flex-shrink: 0;
  transition: border-color 0.15s, opacity 0.15s;
}
.tb-algo:disabled {
  cursor: default;
  opacity: 0.5;
}
.tb-algo-icon {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  flex-shrink: 0;
}
.tb-algo-icon.algo-ok       { background: var(--phosphor); box-shadow: 0 0 5px var(--phosphor); }
.tb-algo-icon.algo-disabled { background: var(--muted); }
.tb-algo-icon.algo-error    { background: var(--danger); box-shadow: 0 0 5px var(--danger-dim); }
.tb-algo-label {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.5px;
  color: var(--text-dim);
}
`

// algo state: 'disabled' | 'ok' | 'error' | 'testing'
function useAlgoStatus() {
  const [algoState, setAlgoState] = useState('disabled')

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const res = await fetch('/api/integrations/algo/status')
        if (!res.ok) throw new Error('http')
        const data = await res.json()
        if (cancelled) return
        setAlgoState(data.enabled ? 'ok' : 'disabled')
      } catch {
        if (!cancelled) setAlgoState('error')
      }
    }
    poll()
    const t = setInterval(poll, 15000)
    return () => { cancelled = true; clearInterval(t) }
  }, [])

  const testFlash = useCallback(async () => {
    setAlgoState('testing')
    try {
      const res = await fetch('/api/integrations/algo/test', { method: 'POST' })
      const data = await res.json()
      setAlgoState(data.success ? 'ok' : 'error')
    } catch {
      setAlgoState('error')
    }
  }, [])

  return { algoState, testFlash }
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
  const { algoState, testFlash } = useAlgoStatus()

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  const onlineNodes  = Object.values(nodes).filter(n => n.status === 'online').length
  const totalNodes   = Object.values(nodes).length
  const alertCount   = alerts.filter(a => a.level === 'high').length
  const noRidCount   = drones.filter(d => !d.has_valid_rid).length

  const hh = now.getUTCHours().toString().padStart(2,'0')
  const mm = now.getUTCMinutes().toString().padStart(2,'0')
  const ss = now.getUTCSeconds().toString().padStart(2,'0')

  return (
    <>
      <style>{css}</style>
      <header className="topbar">
        <div className="tb-logo">
          <div className="tb-logo-hex">
            <svg viewBox="0 0 26 26" fill="none">
              <polygon points="13,1 24,7 24,19 13,25 2,19 2,7"
                stroke="var(--amber)" strokeWidth="1.5"
                fill="rgba(232,160,32,0.08)"/>
              <text x="13" y="17" textAnchor="middle"
                fontFamily="var(--mono)" fontSize="9" fill="var(--amber)" fontWeight="bold">A</text>
            </svg>
          </div>
          <div>
            <div className="tb-logo-text">AEGIS<span style={{color:"var(--text-dim)",fontWeight:300,letterSpacing:2}}> SHIELD</span></div>
            <div className="tb-logo-sub">Airspace Protection Platform</div>
          </div>
        </div>

        <div className="tb-divider"/>

        <div className="tb-stats">
          <div className="tb-stat">
            <div className="tb-stat-label">Nodes Online</div>
            <div className={`tb-stat-val ${onlineNodes === totalNodes && totalNodes > 0 ? 'green' : onlineNodes > 0 ? 'amber' : 'danger'}`}>
              {onlineNodes}/{totalNodes || '—'}
            </div>
          </div>
          <div className="tb-stat">
            <div className="tb-stat-label">Active Drones</div>
            <div className="tb-stat-val amber">{drones.length}</div>
          </div>
          <div className="tb-stat">
            <div className="tb-stat-label">No RID</div>
            <div className={`tb-stat-val ${noRidCount > 0 ? 'danger' : 'green'}`}>{noRidCount}</div>
          </div>
          <div className="tb-stat">
            <div className="tb-stat-label">High Alerts</div>
            <div className={`tb-stat-val ${alertCount > 0 ? 'danger' : 'green'}`}>{alertCount}</div>
          </div>
          <div className="tb-stat">
            <div className="tb-stat-label">Det / min</div>
            <div className="tb-stat-val">{detRate.toFixed(1)}</div>
          </div>
        </div>

        <div className="tb-status">
          <div className={`tb-status-dot dot-${wsStatus}`}/>
          <span>{wsStatus.toUpperCase()}</span>
        </div>

        <button
          className="tb-algo"
          onClick={algoState !== 'disabled' ? testFlash : undefined}
          disabled={algoState === 'disabled' || algoState === 'testing'}
          title={
            algoState === 'disabled' ? 'Algo 8128 — disabled' :
            algoState === 'ok'       ? 'Algo 8128 — click to test flash' :
            algoState === 'testing'  ? 'Algo 8128 — sending test flash…' :
                                       'Algo 8128 — unreachable'
          }
        >
          <div className={`tb-algo-icon algo-${algoState === 'testing' ? 'ok' : algoState}`}/>
          <span className="tb-algo-label">8128</span>
        </button>

        <button
          onClick={toggleUnits}
          title="Toggle metric / imperial"
          style={{
            background: 'transparent',
            border: '1px solid var(--border)',
            borderRadius: '3px',
            color: imperial ? 'var(--amber)' : 'var(--ice)',
            fontFamily: 'var(--mono)',
            fontSize: '10px',
            padding: '3px 8px',
            cursor: 'pointer',
            letterSpacing: '0.5px',
            flexShrink: 0,
            transition: 'color 0.15s, border-color 0.15s',
          }}
        >
          {imperial ? 'IMPERIAL' : 'METRIC'}
        </button>

        <div className="tb-time">{hh}:{mm}:{ss} UTC</div>
      </header>
    </>
  )
}
