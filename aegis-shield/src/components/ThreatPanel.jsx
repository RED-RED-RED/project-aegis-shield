// src/components/ThreatPanel.jsx
// Full-panel threat assessment view. Shows all active drones sorted by threat
// score, with animated score gauge, factor breakdown bars, and MLAT detail.

import { useState } from 'react'
import { formatDistanceToNow } from 'date-fns'
import { useStore, selectActiveDrones, FACTOR_LABELS, threatColor, threatBg, threatLabel, useUnits, fmtAlt, fmtSpeed, fmtDist } from '../store/useStore'

const css = `
.tp-wrap {
  width: 100%; height: 100%;
  display: flex; flex-direction: column;
  background: var(--bg0); overflow: hidden;
}

/* ── Header bar ─────────────────────────────────────────── */
.tp-header {
  display: flex; align-items: center; gap: 14px;
  padding: 8px 16px;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.tp-header-title {
  font-family: var(--cond); font-weight: 900; font-size: 11px;
  letter-spacing: 3px; text-transform: uppercase; color: var(--olive);
}
.tp-stat {
  display: flex; flex-direction: column; gap: 1px;
  padding: 0 12px;
  border-left: 1px solid var(--border);
}
.tp-stat-label { font-family:var(--mono); font-size:8px; letter-spacing:1.5px; color:var(--muted); text-transform:uppercase; }
.tp-stat-val   { font-family:var(--mono); font-size:15px; font-weight:600; }
.tv-danger { color: var(--danger); text-shadow: 0 0 8px var(--danger-dim); }
.tv-amber  { color: var(--amber);  text-shadow: 0 0 8px var(--amber-dim); }
.tv-green  { color: var(--olive); }

/* ── Body: two-column layout ───────────────────────────── */
.tp-body {
  display: grid;
  grid-template-columns: 320px 1fr;
  flex: 1; overflow: hidden;
}
.tp-list {
  border-right: 1px solid var(--border);
  overflow-y: auto; padding: 6px 0;
}
.tp-detail { overflow-y: auto; padding: 20px; }

/* ── Drone threat row ──────────────────────────────────── */
.tdr {
  padding: 10px 14px;
  border-bottom: 1px solid rgba(42,48,40,0.4);
  cursor: pointer; transition: background 0.1s;
  display: flex; align-items: center; gap: 12px;
}
.tdr:hover { background: var(--bg2); }
.tdr.selected { background: rgba(74,111,165,0.05); border-left: 2px solid var(--steel-blue); }
.tdr-gauge {
  flex-shrink: 0;
  position: relative; width: 42px; height: 42px;
}
.tdr-gauge svg { width:42px; height:42px; transform:rotate(-90deg); }
.gauge-track { fill:none; stroke:var(--bg3); stroke-width:4; }
.gauge-fill  { fill:none; stroke-width:4; stroke-linecap:round; transition:stroke-dashoffset 0.6s ease; }
.tdr-score-label {
  position:absolute; inset:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center;
}
.tdr-score-num {
  font-family:var(--mono); font-size:12px; font-weight:bold; line-height:1;
}
.tdr-score-lv {
  font-family:var(--cond); font-size:7px; letter-spacing:1.5px; font-weight:700;
  text-transform:uppercase; margin-top:1px;
}
.tdr-info { flex:1; min-width:0; }
.tdr-id {
  font-family:var(--mono); font-size:11px; color:var(--text);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.tdr-meta {
  display:flex; gap:6px; font-family:var(--mono); font-size:9px;
  color:var(--text-dim); margin-top:3px; flex-wrap:wrap;
}
.tdr-flags { display:flex; gap:4px; margin-top:4px; flex-wrap:wrap; }
.flag {
  font-family:var(--cond); font-weight:700; font-size:7px;
  letter-spacing:1px; text-transform:uppercase;
  padding:1px 5px; border-radius:2px; border:1px solid;
}
.flag-danger { color:var(--danger); border-color:var(--danger-dim); background:var(--danger-glow); }
.flag-amber  { color:var(--amber);  border-color:var(--amber-dim);  background:var(--amber-glow); }
.flag-ice    { color:var(--ice);    border-color:var(--ice-dim);    background:rgba(74,111,165,0.06); }

/* ── Detail pane ───────────────────────────────────────── */
.detail-hero {
  display:flex; align-items:flex-start; gap:20px;
  margin-bottom:20px;
}
.detail-big-gauge {
  flex-shrink:0; position:relative; width:88px; height:88px;
}
.detail-big-gauge svg { width:88px; height:88px; transform:rotate(-90deg); }
.dbg-track { fill:none; stroke:var(--bg3); stroke-width:7; }
.dbg-fill  { fill:none; stroke-width:7; stroke-linecap:round; transition:stroke-dashoffset 0.8s ease; }
.dbg-label {
  position:absolute; inset:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:2px;
}
.dbg-num {
  font-family:var(--mono); font-size:22px; font-weight:bold; line-height:1;
}
.dbg-lv {
  font-family:var(--cond); font-size:9px; letter-spacing:2px; font-weight:900;
  text-transform:uppercase;
}
.detail-id-block { flex:1; }
.detail-drone-id {
  font-family:var(--mono); font-size:16px; color:var(--text);
  margin-bottom:4px; word-break:break-all;
}
.detail-type { font-size:11px; color:var(--text-dim); margin-bottom:8px; }
.detail-chips { display:flex; gap:5px; flex-wrap:wrap; }
.detail-chip {
  font-family:var(--mono); font-size:9px;
  padding:2px 7px; border-radius:2px; border:1px solid;
}

/* ── Section heading ───────────────────────────────────── */
.detail-section-head {
  font-family:var(--cond); font-weight:700; font-size:9px;
  letter-spacing:2px; text-transform:uppercase;
  color:var(--muted); margin:16px 0 8px;
}

/* ── Factor bars ───────────────────────────────────────── */
.factor-row {
  display:grid; grid-template-columns:140px 1fr 48px 36px;
  align-items:center; gap:8px;
  margin-bottom:5px;
}
.factor-label { font-family:var(--mono); font-size:9px; color:var(--text-dim); }
.factor-track {
  height:5px; background:var(--bg3); border-radius:3px; overflow:hidden;
}
.factor-fill { height:100%; border-radius:3px; transition:width 0.6s ease; }
.factor-pct { font-family:var(--mono); font-size:9px; color:var(--text-dim); text-align:right; }
.factor-wt  { font-family:var(--mono); font-size:8px; color:var(--muted); text-align:right; }

/* ── MLAT section ──────────────────────────────────────── */
.mlat-card {
  background:var(--bg2); border:1px solid var(--border);
  border-radius:4px; padding:12px 14px;
}
.mlat-row {
  display:flex; justify-content:space-between; align-items:center;
  padding:4px 0; border-bottom:1px solid rgba(42,48,40,0.4);
  font-family:var(--mono); font-size:10px;
}
.mlat-row:last-child { border-bottom:none; }
.mlat-key { color:var(--muted); }
.mlat-val { color:var(--text); }
.spoof-bar-wrap { display:flex; align-items:center; gap:8px; flex:1; margin-left:12px; }
.spoof-bar-track { flex:1; height:4px; background:var(--bg3); border-radius:2px; overflow:hidden; }
.spoof-bar-fill { height:100%; border-radius:2px; transition:width 0.6s; }

/* ── No selection ──────────────────────────────────────── */
.tp-empty {
  display:flex; align-items:center; justify-content:center;
  height:100%; flex-direction:column; gap:8px;
  font-family:var(--mono); font-size:10px; color:var(--muted);
  opacity:0.6;
}
.tp-empty-icon { font-size:28px; opacity:0.3; }
`

// ── Circular gauge SVG ─────────────────────────────────────────────────────
function Gauge({ score, size = 42, strokeWidth = 4 }) {
  const r      = (size - strokeWidth) / 2
  const circ   = 2 * Math.PI * r
  const filled = ((score ?? 0) / 100) * circ
  const color  = (score ?? 0) >= 70 ? 'var(--danger)'
               : (score ?? 0) >= 40 ? 'var(--amber)'
               : 'var(--olive)'
  const level  = threatLabel(score)

  return (
    <div style={{ position:'relative', width:size, height:size, flexShrink:0 }}>
      <svg width={size} height={size} style={{ transform:'rotate(-90deg)' }}>
        <circle className="gauge-track" cx={size/2} cy={size/2} r={r} strokeWidth={strokeWidth}/>
        <circle className="gauge-fill" cx={size/2} cy={size/2} r={r}
          stroke={color} strokeWidth={strokeWidth}
          strokeDasharray={`${filled} ${circ - filled}`}
          strokeDashoffset={0}/>
      </svg>
      <div className="tdr-score-label">
        <div className="tdr-score-num" style={{ color, fontSize: size < 60 ? 12 : 22 }}>
          {score != null ? score.toFixed(0) : '—'}
        </div>
        <div className="tdr-score-lv" style={{ color, fontSize: size < 60 ? 7 : 9 }}>{level}</div>
      </div>
    </div>
  )
}

// ── Factor bar row ─────────────────────────────────────────────────────────
function FactorBar({ name, value, weight }) {
  const contrib = value * weight
  const pct     = value * 100
  const color   = contrib >= 15 ? 'var(--danger)'
                : contrib >= 6  ? 'var(--amber)'
                : 'var(--olive)'
  const { label } = FACTOR_LABELS[name] || { label: name }

  return (
    <div className="factor-row">
      <div className="factor-label">{label}</div>
      <div className="factor-track">
        <div className="factor-fill" style={{ width: `${pct}%`, background: color }}/>
      </div>
      <div className="factor-pct" style={{ color }}>{pct.toFixed(0)}%</div>
      <div className="factor-wt">{weight}pt</div>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────
export default function ThreatPanel() {
  const drones     = useStore(selectActiveDrones)
  const [selId, setSel] = useState(null)
  const selectDrone = useStore(s => s.selectDrone)
  const imperial   = useUnits()

  const drone = drones.find(d => d.drone_id === selId) || (drones.length ? drones[0] : null)

  const highCount = drones.filter(d => (d.threat_score??0) >= 70).length
  const medCount  = drones.filter(d => (d.threat_score??0) >= 40 && (d.threat_score??0) < 70).length
  const mlatCount = drones.filter(d => d.mlat_lat != null).length

  // Parse threat_factors — server returns either a dict or a stringified dict
  let factors = null
  if (drone?.threat_factors) {
    try {
      const raw = typeof drone.threat_factors === 'string'
        ? JSON.parse(drone.threat_factors.replace(/'/g,'"').replace(/True/g,'true').replace(/False/g,'false'))
        : drone.threat_factors
      factors = raw?.factors ?? null
    } catch {}
  }

  return (
    <>
      <style>{css}</style>
      <div className="tp-wrap">

        {/* Header */}
        <div className="tp-header">
          <div className="tp-header-title">Threat Assessment</div>
          <div className="tp-stat">
            <div className="tp-stat-label">High</div>
            <div className={`tp-stat-val ${highCount > 0 ? 'tv-danger' : 'tv-green'}`}>{highCount}</div>
          </div>
          <div className="tp-stat">
            <div className="tp-stat-label">Medium</div>
            <div className={`tp-stat-val ${medCount > 0 ? 'tv-amber' : 'tv-green'}`}>{medCount}</div>
          </div>
          <div className="tp-stat">
            <div className="tp-stat-label">MLAT Active</div>
            <div className="tp-stat-val tv-amber">{mlatCount}</div>
          </div>
          <div className="tp-stat">
            <div className="tp-stat-label">Total Drones</div>
            <div className="tp-stat-val">{drones.length}</div>
          </div>
        </div>

        <div className="tp-body">

          {/* Left: drone list */}
          <div className="tp-list">
            {drones.length === 0 && (
              <div style={{padding:'16px',fontFamily:'var(--mono)',fontSize:9,color:'var(--muted)'}}>
                No active drones
              </div>
            )}
            {drones.map(d => {
              const score = d.threat_score
              const stale = d.last_seen && (Date.now() - new Date(d.last_seen).getTime()) > 5 * 60 * 1000
              const flags = []
              if (!d.has_valid_rid)               flags.push({ label:'NO RID',  cls:'flag-danger' })
              if (d.mlat_mismatch_m > 250)         flags.push({ label:`Δ${fmtDist(d.mlat_mismatch_m, imperial)}`, cls:'flag-danger' })
              if ((d.spoof_confidence??0) > 0.6)  flags.push({ label:'SPOOF?',  cls:'flag-danger' })
              if ((d.speed_h??0) > 25)             flags.push({ label:'SPEED',   cls:'flag-amber' })
              if (d.mlat_lat != null)              flags.push({ label:'MLAT',    cls:'flag-ice' })
              if (stale)                           flags.push({ label:'STALE',   cls:'flag-ice' })

              return (
                <div key={d.drone_id}
                  className={`tdr ${(selId??drone?.drone_id)===d.drone_id ? 'selected':''}`}
                  style={stale ? {opacity:0.5} : undefined}
                  onClick={() => { setSel(d.drone_id); selectDrone(d.drone_id) }}>
                  <Gauge score={score} size={42} strokeWidth={4}/>
                  <div className="tdr-info">
                    <div className="tdr-id">{d.drone_id}</div>
                    <div className="tdr-meta">
                      <span>{d.ua_type || 'unknown'}</span>
                      {d.height_agl != null && <span>▲{fmtAlt(d.height_agl, imperial)}</span>}
                      {d.speed_h    != null && <span>⟶{fmtSpeed(d.speed_h, imperial)}</span>}
                    </div>
                    <div className="tdr-flags">
                      {flags.map(f => (
                        <span key={f.label} className={`flag ${f.cls}`}>{f.label}</span>
                      ))}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>

          {/* Right: detail */}
          <div className="tp-detail">
            {!drone ? (
              <div className="tp-empty">
                <div className="tp-empty-icon">◈</div>
                <div>No drones detected</div>
              </div>
            ) : (
              <>
                {/* Hero: big gauge + ID block */}
                <div className="detail-hero">
                  <Gauge score={drone.threat_score} size={88} strokeWidth={7}/>
                  <div className="detail-id-block">
                    <div className="detail-drone-id">{drone.drone_id}</div>
                    <div className="detail-type">{drone.ua_type || 'Unknown aircraft type'}</div>
                    <div className="detail-chips">
                      {drone.has_valid_rid
                        ? <span className="detail-chip" style={{color:'var(--olive)',borderColor:'var(--olive-dim)',background:'rgba(74,124,89,0.1)'}}>✓ VALID RID</span>
                        : <span className="detail-chip" style={{color:'var(--danger)',borderColor:'var(--danger-dim)',background:'var(--danger-glow)'}}>⚠ NO RID</span>
                      }
                      {drone.operator_id
                        ? <span className="detail-chip" style={{color:'var(--olive)',borderColor:'var(--olive-dim)',background:'rgba(74,124,89,0.1)'}}>{drone.operator_id}</span>
                        : <span className="detail-chip" style={{color:'var(--danger)',borderColor:'var(--danger-dim)',background:'var(--danger-glow)'}}>NO OPERATOR ID</span>
                      }
                      {drone.last_transport && (
                        <span className="detail-chip" style={{color:'var(--ice)',borderColor:'var(--ice-dim)',background:'rgba(74,111,165,0.06)'}}>
                          {drone.last_transport === 'wifi_nan' ? 'WiFi NAN' : 'BT5 LR'}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {/* Position + flight data */}
                <div className="detail-section-head">Flight Data</div>
                <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:6,marginBottom:4}}>
                  {[
                    { label:'Altitude',  val: fmtAlt(drone.alt_baro, imperial) },
                    { label:'Height AGL',val: fmtAlt(drone.height_agl, imperial) },
                    { label:'Speed',     val: fmtSpeed(drone.speed_h, imperial) },
                    { label:'Heading',   val: drone.heading   != null ? drone.heading.toFixed(0)+'°'      : '—' },
                    { label:'Position',  val: drone.lat       != null ? `${drone.lat.toFixed(4)}, ${drone.lon.toFixed(4)}` : '—' },
                    { label:'Detections',val: (drone.detection_count ?? 0).toString() },
                    { label:'Nodes',     val: (drone.detecting_nodes||[]).join(', ') || '—' },
                    { label:'Last RSSI', val: drone.last_rssi != null ? drone.last_rssi+' dBm' : '—' },
                  ].map(({ label, val }) => (
                    <div key={label} style={{background:'var(--bg2)',border:'1px solid var(--border)',borderRadius:3,padding:'5px 8px'}}>
                      <div style={{fontFamily:'var(--mono)',fontSize:7,letterSpacing:1,color:'var(--muted)',textTransform:'uppercase',marginBottom:2}}>{label}</div>
                      <div style={{fontFamily:'var(--mono)',fontSize:11,color:'var(--text)'}}>{val}</div>
                    </div>
                  ))}
                </div>

                {/* Factor breakdown */}
                {factors && (
                  <>
                    <div className="detail-section-head">Threat Factors</div>
                    {Object.entries(FACTOR_LABELS)
                      .sort(([,a],[,b]) => b.weight - a.weight)
                      .map(([key, { weight }]) => (
                        <FactorBar key={key} name={key} value={factors[key]??0} weight={weight}/>
                      ))
                    }
                  </>
                )}

                {/* MLAT section */}
                {drone.mlat_lat != null && (
                  <>
                    <div className="detail-section-head">MLAT Position Estimate</div>
                    <div className="mlat-card">
                      {[
                        { key:'Estimated Position', val:`${drone.mlat_lat.toFixed(5)}, ${drone.mlat_lon.toFixed(5)}` },
                        { key:'Broadcast Position', val:`${drone.lat?.toFixed(5)}, ${drone.lon?.toFixed(5)}` },
                        { key:'Position Mismatch',  val: <span style={{color: drone.mlat_mismatch_m>250?'var(--danger)':'var(--olive)'}}>{fmtDist(drone.mlat_mismatch_m, imperial)}</span> },
                        { key:'Confidence Radius',  val: `±${fmtDist(drone.mlat_radius_m, imperial)}` },
                        { key:'Node Count',         val: `${drone.mlat_node_count} nodes` },
                      ].map(({ key, val }) => (
                        <div key={key} className="mlat-row">
                          <span className="mlat-key">{key}</span>
                          <span className="mlat-val">{val}</span>
                        </div>
                      ))}
                      <div className="mlat-row">
                        <span className="mlat-key">Spoof Confidence</span>
                        <div className="spoof-bar-wrap">
                          <div className="spoof-bar-track">
                            <div className="spoof-bar-fill" style={{
                              width:`${(drone.spoof_confidence??0)*100}%`,
                              background: (drone.spoof_confidence??0) > 0.7 ? 'var(--danger)'
                                        : (drone.spoof_confidence??0) > 0.4 ? 'var(--amber)'
                                        : 'var(--olive)'
                            }}/>
                          </div>
                          <span className="mlat-val" style={{
                            color: (drone.spoof_confidence??0) > 0.7 ? 'var(--danger)'
                                 : (drone.spoof_confidence??0) > 0.4 ? 'var(--amber)'
                                 : 'var(--olive)',
                            minWidth: 36, textAlign:'right'
                          }}>
                            {((drone.spoof_confidence??0)*100).toFixed(0)}%
                          </span>
                        </div>
                      </div>
                    </div>
                  </>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </>
  )
}
