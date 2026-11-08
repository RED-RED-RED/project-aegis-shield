// src/components/SystemView.jsx
// System health: live status cards + historical cpu/mem/disk/temp charts for
// every node and the server, on one page. Future home for GUI settings.
import { useEffect, useRef, useState } from 'react'
import { colors } from '../theme/colors'
import SettingsPanel from './SettingsPanel'

const SERVER_ID = '__server__'
const RANGES = ['1h', '6h', '24h', '7d']

// Warning / critical thresholds per metric (value >= warn is amber, >= crit red)
const THRESH = {
  cpu_pct:  { warn: 85, crit: 95, max: 100, unit: '%',  label: 'CPU' },
  mem_pct:  { warn: 85, crit: 95, max: 100, unit: '%',  label: 'Memory' },
  disk_pct: { warn: 85, crit: 95, max: 100, unit: '%',  label: 'Disk' },
  temp_c:   { warn: 70, crit: 80, max: 90,  unit: '°C', label: 'Temp' },
}

// Series colors: server fixed to steel blue; nodes cycle a distinct palette.
const NODE_PALETTE = [colors.olive, colors.threatLow, colors.threatMed,
                      colors.steelBlueLight, '#B07AA1', '#76B7B2']

function seriesColor(nodeId, sortedNodeIds) {
  if (nodeId === SERVER_ID) return colors.steelBlue
  const i = sortedNodeIds.indexOf(nodeId)
  return NODE_PALETTE[i % NODE_PALETTE.length]
}

function label(nodeId) { return nodeId === SERVER_ID ? 'AEGIS Server' : nodeId }

function valColor(metric, v) {
  if (v == null) return 'var(--muted)'
  const t = THRESH[metric]
  if (!t) return 'var(--text)'
  if (v >= t.crit) return colors.threatHigh
  if (v >= t.warn) return colors.threatMed
  return 'var(--text)'
}

function fmtUptime(s) {
  if (s == null) return '—'
  s = Math.floor(s)
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${m}m`
  return `${m}m`
}

// ── Multi-series SVG line chart (no deps) ────────────────────────────────────
function LineChart({ metric, series, width = 520, height = 150 }) {
  const t = THRESH[metric]
  const padL = 30, padB = 16, padT = 8, padR = 8
  const w = width - padL - padR, h = height - padT - padB
  const yMax = t.max

  // Collect all timestamps to build the x-domain.
  let tMin = Infinity, tMax = -Infinity
  for (const s of series) for (const p of s.points) {
    if (p.v == null) continue
    const ms = new Date(p.t).getTime()
    if (ms < tMin) tMin = ms
    if (ms > tMax) tMax = ms
  }
  const span = tMax - tMin || 1
  const x = ms => padL + ((ms - tMin) / span) * w
  const y = v => padT + (1 - v / yMax) * h

  const warnY = y(t.warn)
  const critY = y(t.crit)

  function pathFor(points) {
    let d = '', pen = false
    for (const p of points) {
      if (p.v == null) { pen = false; continue }
      const px = x(new Date(p.t).getTime()), py = y(Math.min(p.v, yMax))
      d += `${pen ? 'L' : 'M'}${px.toFixed(1)} ${py.toFixed(1)} `
      pen = true
    }
    return d
  }

  const hasData = tMax >= tMin && isFinite(tMin)
  const gridVals = [0, 0.25, 0.5, 0.75, 1].map(f => f * yMax)

  return (
    <div className="sv-chart">
      <div className="sv-chart-title">{t.label} <span className="sv-chart-unit">{t.unit}</span></div>
      <svg viewBox={`0 0 ${width} ${height}`} className="sv-svg" preserveAspectRatio="none">
        {/* gridlines + y labels */}
        {gridVals.map((gv, i) => (
          <g key={i}>
            <line x1={padL} y1={y(gv)} x2={width - padR} y2={y(gv)} stroke="var(--bg-border)" strokeWidth="0.5" />
            <text x={padL - 4} y={y(gv) + 3} textAnchor="end" className="sv-axis">{Math.round(gv)}</text>
          </g>
        ))}
        {/* warn / crit bands */}
        <rect x={padL} y={critY} width={w} height={Math.max(0, y(t.warn) - critY)} fill={colors.threatMed} opacity="0.07" />
        <rect x={padL} y={padT} width={w} height={Math.max(0, critY - padT)} fill={colors.threatHigh} opacity="0.07" />
        <line x1={padL} y1={warnY} x2={width - padR} y2={warnY} stroke={colors.threatMed} strokeWidth="0.6" strokeDasharray="3 3" opacity="0.6" />
        {/* series */}
        {hasData && series.map(s => (
          <path key={s.id} d={pathFor(s.points)} fill="none" stroke={s.color} strokeWidth="1.3" />
        ))}
        {!hasData && <text x={width / 2} y={height / 2} textAnchor="middle" className="sv-nodata">no data in range</text>}
      </svg>
    </div>
  )
}

export default function SystemView() {
  const [tab, setTab] = useState('health')
  const [range, setRange] = useState('24h')
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const timer = useRef(null)

  useEffect(() => {
    let alive = true
    async function load() {
      try {
        const res = await fetch(`/api/system/health/history?range=${range}`)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const j = await res.json()
        if (alive) { setData(j); setErr(null) }
      } catch (e) {
        if (alive) setErr(String(e.message || e))
      }
    }
    load()
    timer.current = setInterval(load, 15000)
    return () => { alive = false; clearInterval(timer.current) }
  }, [range])

  const series = data?.series || {}
  const current = data?.current || {}
  const nodeIds = Object.keys({ ...series, ...current })
  // Real nodes sorted, server last.
  const realNodes = nodeIds.filter(n => n !== SERVER_ID).sort()
  const ordered = [...realNodes, ...(nodeIds.includes(SERVER_ID) ? [SERVER_ID] : [])]

  // Build per-metric series arrays for the charts.
  function metricSeries(metric) {
    return ordered
      .filter(n => series[n])
      .map(n => ({
        id: n,
        color: seriesColor(n, realNodes),
        points: series[n].map(p => ({ t: p.t, v: p[metric] })),
      }))
      .filter(s => s.points.some(p => p.v != null))
  }

  return (
    <div className="sv-root">
      <style>{css}</style>

      <div className="sv-head">
        <div className="sv-tabs">
          <button className={`sv-tab ${tab === 'health' ? 'active' : ''}`} onClick={() => setTab('health')}>System Health</button>
          <button className={`sv-tab ${tab === 'settings' ? 'active' : ''}`} onClick={() => setTab('settings')}>Settings</button>
        </div>
        {tab === 'health' && (
          <div className="sv-ranges">
            {RANGES.map(r => (
              <button key={r} className={`sv-range ${range === r ? 'active' : ''}`} onClick={() => setRange(r)}>{r}</button>
            ))}
          </div>
        )}
      </div>

      {tab === 'settings' && <SettingsPanel />}

      {tab === 'health' && (<>
      {err && <div className="sv-err">Couldn’t load health history: {err}</div>}

      {/* Current status cards */}
      <div className="sv-cards">
        {ordered.length === 0 && <div className="sv-empty">No health data yet. Nodes report on heartbeat; the server samples itself every few seconds.</div>}
        {ordered.map(n => {
          const c = current[n] || {}
          const isServer = n === SERVER_ID
          return (
            <div key={n} className="sv-card">
              <div className="sv-card-head">
                <span className="sv-dot" style={{ background: seriesColor(n, realNodes) }} />
                <span className="sv-card-name">{label(n)}</span>
                <span className="sv-card-up">{fmtUptime(c.uptime_s)}</span>
              </div>
              <div className="sv-metrics">
                <Metric m="cpu_pct"  v={c.cpu_pct} />
                <Metric m="mem_pct"  v={c.mem_pct} />
                <Metric m="disk_pct" v={c.disk_pct} />
                {!isServer && <Metric m="temp_c" v={c.temp_c} />}
              </div>
              <div className="sv-extra">
                {isServer ? (
                  <>
                    <Chip ok={c.db_ok}   label="DB" />
                    <Chip ok={c.mqtt_ok} label="MQTT" />
                  </>
                ) : (
                  <span className="sv-sats" style={{ color: c.gps_fix ? colors.threatLow : 'var(--muted)' }}>
                    GPS {c.gps_fix ? 'fix' : 'no fix'} · {c.satellites ?? 0} sats
                  </span>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* Legend */}
      {ordered.length > 0 && (
        <div className="sv-legend">
          {ordered.map(n => (
            <span key={n} className="sv-leg-item">
              <span className="sv-dot" style={{ background: seriesColor(n, realNodes) }} />{label(n)}
            </span>
          ))}
        </div>
      )}

      {/* Charts */}
      <div className="sv-charts">
        {['cpu_pct', 'mem_pct', 'disk_pct', 'temp_c'].map(m => (
          <LineChart key={m} metric={m} series={metricSeries(m)} />
        ))}
      </div>

      <div className="sv-note">History retained ~7 days · refreshes every 15s · dashed line = warning threshold</div>
      </>)}
    </div>
  )
}

function Metric({ m, v }) {
  const t = THRESH[m]
  const pct = v == null ? 0 : Math.min(v / t.max * 100, 100)
  return (
    <div className="sv-metric">
      <div className="sv-metric-top">
        <span className="sv-metric-label">{t.label}</span>
        <span className="sv-metric-val" style={{ color: valColor(m, v) }}>
          {v == null ? '—' : `${v.toFixed(0)}${t.unit}`}
        </span>
      </div>
      <div className="sv-bar"><div className="sv-bar-fill" style={{ width: `${pct}%`, background: valColor(m, v) }} /></div>
    </div>
  )
}

function Chip({ ok, label }) {
  return (
    <span className="sv-chip" style={{
      color: ok ? colors.threatLow : colors.threatHigh,
      borderColor: ok ? colors.threatLowBorder : colors.threatHighBorder,
    }}>{label} {ok ? 'ok' : 'down'}</span>
  )
}

const css = `
.sv-root { height:100%; overflow-y:auto; padding:16px 18px; background:var(--bg0); }
.sv-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }
.sv-title { font-family:'Barlow Condensed',sans-serif; font-weight:700; font-size:20px;
  text-transform:uppercase; letter-spacing:0.04em; color:var(--text); }
.sv-tabs { display:flex; gap:4px; }
.sv-tab { background:none; border:none; border-bottom:2px solid transparent; color:var(--muted);
  font-family:'Barlow Condensed',sans-serif; font-weight:700; font-size:19px; text-transform:uppercase;
  letter-spacing:0.04em; padding:0 6px 2px; cursor:pointer; }
.sv-tab.active { color:var(--text); border-bottom-color:var(--steel-blue); }
.sv-ranges { display:flex; gap:4px; }
.sv-range { background:var(--bg1); border:1px solid var(--bg-border); color:var(--muted);
  font-family:'Share Tech Mono',monospace; font-size:11px; padding:3px 10px; border-radius:3px; cursor:pointer; }
.sv-range.active { color:var(--text); border-color:var(--steel-blue); background:var(--bg2); }
.sv-err { color:var(--threat-high); font-size:12px; margin-bottom:10px; }
.sv-empty { color:var(--muted); font-size:12px; padding:12px; }

.sv-cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:10px; margin-bottom:16px; }
.sv-card { background:var(--bg1); border:1px solid var(--bg-border); border-radius:5px; padding:10px 12px; }
.sv-card-head { display:flex; align-items:center; gap:7px; margin-bottom:8px; }
.sv-dot { width:9px; height:9px; border-radius:50%; flex-shrink:0; }
.sv-card-name { font-family:'Share Tech Mono',monospace; font-size:12px; color:var(--text); font-weight:700; }
.sv-card-up { margin-left:auto; font-family:'Share Tech Mono',monospace; font-size:10px; color:var(--muted); }
.sv-metrics { display:flex; flex-direction:column; gap:5px; }
.sv-metric-top { display:flex; justify-content:space-between; font-family:'Share Tech Mono',monospace; font-size:10px; }
.sv-metric-label { color:var(--muted); }
.sv-metric-val { font-weight:700; }
.sv-bar { height:3px; background:var(--bg-border); border-radius:2px; overflow:hidden; margin-top:2px; }
.sv-bar-fill { height:100%; border-radius:2px; transition:width 0.4s ease; }
.sv-extra { margin-top:8px; display:flex; gap:6px; align-items:center; }
.sv-sats { font-family:'Share Tech Mono',monospace; font-size:10px; }
.sv-chip { font-family:'Share Tech Mono',monospace; font-size:10px; border:1px solid; border-radius:3px; padding:1px 6px; }

.sv-legend { display:flex; flex-wrap:wrap; gap:14px; margin-bottom:10px; }
.sv-leg-item { display:flex; align-items:center; gap:5px; font-family:'Share Tech Mono',monospace; font-size:10px; color:var(--text-secondary); }

.sv-charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:12px; }
.sv-chart { background:var(--bg1); border:1px solid var(--bg-border); border-radius:5px; padding:8px 10px; }
.sv-chart-title { font-family:'Share Tech Mono',monospace; font-size:11px; color:var(--text); margin-bottom:4px; }
.sv-chart-unit { color:var(--muted); }
.sv-svg { width:100%; height:150px; display:block; }
.sv-axis { fill:var(--muted); font-family:'Share Tech Mono',monospace; font-size:7px; }
.sv-nodata { fill:var(--muted); font-family:'Share Tech Mono',monospace; font-size:10px; }
.sv-note { color:var(--muted); font-size:10px; margin-top:12px; font-family:'Share Tech Mono',monospace; }
`
