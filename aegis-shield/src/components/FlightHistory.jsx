// src/components/FlightHistory.jsx
// Historical flights: detections grouped into sorties. Pick a flight to see its
// path, operator, detecting nodes, altitude/speed profile, and a time-scrub
// replay; export as GPX/KML/CSV. No-RID / RF-only contacts listed separately.
import { useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { colors } from '../theme/colors'
import { useUnits, fmtAlt, fmtSpeed, RF_SIGNAL_LABELS } from '../store/useStore'

const RANGES = ['6h', '24h', '7d', '30d']
const THREATS = [['all', 'Any threat'], ['40', 'Med+'], ['70', 'High']]
const COMPLIANCE = [['all', 'All'], ['yes', 'Valid RID'], ['no', 'Invalid RID']]

function fmtDur(s) {
  if (s == null) return '—'
  const m = Math.floor(s / 60), sec = s % 60
  if (m >= 60) return `${Math.floor(m / 60)}h ${m % 60}m`
  return m ? `${m}m ${sec}s` : `${sec}s`
}
function fmtTime(iso) { try { return new Date(iso).toLocaleString() } catch { return iso } }
function ago(iso) { return (Date.now() - new Date(iso).getTime()) / 1000 }

// ── tiny single-series SVG profile ───────────────────────────────────────────
function Spark({ values, label, unit, color }) {
  const w = 300, h = 54, pad = 2
  const vals = values.filter(v => v != null)
  const lo = Math.min(...vals, 0), hi = Math.max(...vals, 1)
  const span = hi - lo || 1
  const n = values.length
  let d = '', pen = false
  values.forEach((v, i) => {
    if (v == null) { pen = false; return }
    const x = pad + (i / Math.max(n - 1, 1)) * (w - 2 * pad)
    const y = pad + (1 - (v - lo) / span) * (h - 2 * pad)
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)} `
    pen = true
  })
  return (
    <div className="fh-spark">
      <div className="fh-spark-lab">{label} <span className="fh-spark-unit">{unit}</span>
        <span className="fh-spark-rng">{vals.length ? `${lo.toFixed(0)}–${hi.toFixed(0)}` : '—'}</span></div>
      <svg viewBox={`0 0 ${w} ${h}`} className="fh-spark-svg" preserveAspectRatio="none">
        {vals.length > 1 && <path d={d} fill="none" stroke={color} strokeWidth="1.3" />}
      </svg>
    </div>
  )
}

export default function FlightHistory() {
  const imperial = useUnits()
  const [range, setRange]         = useState('24h')
  const [compliant, setCompliant] = useState('all')
  const [node, setNode]           = useState('')
  const [minThreat, setMinThreat] = useState('all')
  const [flights, setFlights]     = useState([])
  const [rf, setRf]               = useState([])
  const [selId, setSelId]         = useState(null)
  const [detail, setDetail]       = useState(null)
  const [idx, setIdx]             = useState(0)
  const [playing, setPlaying]     = useState(false)

  const mapRef = useRef(null)
  const leaflet = useRef(null)
  const layers = useRef([])
  const moverRef = useRef(null)
  const playTimer = useRef(null)

  // ── fetch list + rf history ────────────────────────────────────────────────
  useEffect(() => {
    let alive = true
    const q = new URLSearchParams({ range, compliant })
    if (node) q.set('node', node)
    if (minThreat !== 'all') q.set('min_threat', minThreat)
    fetch(`/api/flights?${q}`).then(r => r.json()).then(j => { if (alive) setFlights(j.flights || []) }).catch(() => {})
    fetch(`/api/flights/rf-contacts?range=${range}&dark_only=true`).then(r => r.json())
      .then(j => { if (alive) setRf(j.contacts || []) }).catch(() => {})
    return () => { alive = false }
  }, [range, compliant, node, minThreat])

  // ── fetch detail ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!selId) { setDetail(null); return }
    let alive = true
    setPlaying(false)
    fetch(`/api/flights/${encodeURIComponent(selId)}`).then(r => r.json())
      .then(j => { if (alive) { setDetail(j); setIdx((j.path?.length || 1) - 1) } }).catch(() => {})
    return () => { alive = false }
  }, [selId])

  // nodes present across flights (for the filter dropdown)
  const nodeOptions = useMemo(() => {
    const s = new Set()
    flights.forEach(f => (f.detecting_nodes || []).forEach(n => s.add(n)))
    return [...s].sort()
  }, [flights])

  // ── init map once ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!mapRef.current || leaflet.current) return
    const map = L.map(mapRef.current, { center: [39.5, -98.35], zoom: 5 })
    const key = import.meta.env.VITE_CARTO_API_KEY
    const kp = key ? `?key=${encodeURIComponent(key)}` : ''
    L.tileLayer(`https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}{r}.png${kp}`, {
      attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19,
    }).addTo(map)
    leaflet.current = map
    return () => { map.remove(); leaflet.current = null }
  }, [])

  // ── draw the selected flight ────────────────────────────────────────────────
  useEffect(() => {
    const map = leaflet.current
    if (!map) return
    layers.current.forEach(l => map.removeLayer(l)); layers.current = []
    moverRef.current = null
    if (!detail?.path?.length) return

    const pts = detail.path.filter(p => p.lat != null && p.lon != null).map(p => [p.lat, p.lon])
    if (!pts.length) return

    const line = L.polyline(pts, { color: colors.steelBlueLight, weight: 2 }).addTo(map)
    const start = L.circleMarker(pts[0], { radius: 6, color: colors.threatLow, fillColor: colors.threatLow, fillOpacity: 0.9 })
      .bindTooltip('Start').addTo(map)
    const end = L.circleMarker(pts[pts.length - 1], { radius: 6, color: colors.threatHigh, fillColor: colors.threatHigh, fillOpacity: 0.9 })
      .bindTooltip('End').addTo(map)
    layers.current.push(line, start, end)

    if (detail.operator) {
      const op = L.marker([detail.operator.lat, detail.operator.lon], {
        icon: L.divIcon({ className: '', html: `<div style="color:${colors.threatMed};font-size:16px">⌖</div>`, iconSize: [16, 16] }),
      }).bindTooltip('Operator').addTo(map)
      layers.current.push(op)
    }
    for (const nd of detail.nodes || []) {
      const nm = L.circleMarker([nd.lat, nd.lon], { radius: 4, color: colors.olive, fillColor: colors.olive, fillOpacity: 0.8 })
        .bindTooltip(`Node ${nd.node_id}`).addTo(map)
      layers.current.push(nm)
    }
    const mover = L.circleMarker(pts[0], { radius: 5, color: '#fff', weight: 2, fillColor: colors.steelBlue, fillOpacity: 1 }).addTo(map)
    moverRef.current = mover
    layers.current.push(mover)

    map.fitBounds(line.getBounds(), { padding: [40, 40], maxZoom: 16 })
  }, [detail])

  // ── move replay marker ───────────────────────────────────────────────────
  useEffect(() => {
    const p = detail?.path?.[idx]
    if (moverRef.current && p && p.lat != null) moverRef.current.setLatLng([p.lat, p.lon])
  }, [idx, detail])

  // ── play/pause timer ─────────────────────────────────────────────────────
  useEffect(() => {
    clearInterval(playTimer.current)
    if (!playing || !detail?.path?.length) return
    playTimer.current = setInterval(() => {
      setIdx(i => {
        if (i >= detail.path.length - 1) { setPlaying(false); return i }
        return i + 1
      })
    }, 250)
    return () => clearInterval(playTimer.current)
  }, [playing, detail])

  const recentCount = flights.filter(f => ago(f.end) < 3600).length
  const cur = detail?.path?.[idx]

  return (
    <div className="fh-root">
      <style>{css}</style>

      {/* ── left: filters + lists ── */}
      <div className="fh-side">
        <div className="fh-filters">
          <div className="fh-frow">
            {RANGES.map(r => <button key={r} className={`fh-chip ${range === r ? 'on' : ''}`} onClick={() => setRange(r)}>{r}</button>)}
          </div>
          <div className="fh-frow">
            {COMPLIANCE.map(([v, l]) => <button key={v} className={`fh-chip ${compliant === v ? 'on' : ''}`} onClick={() => setCompliant(v)}>{l}</button>)}
          </div>
          <div className="fh-frow">
            <select className="fh-sel" value={node} onChange={e => setNode(e.target.value)}>
              <option value="">All nodes</option>
              {nodeOptions.map(n => <option key={n} value={n}>{n}</option>)}
            </select>
            <select className="fh-sel" value={minThreat} onChange={e => setMinThreat(e.target.value)}>
              {THREATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
        </div>

        <div className="fh-listhead">Flights ({flights.length}){recentCount > 0 && <span className="fh-recent"> · {recentCount} ended &lt;1h</span>}</div>
        <div className="fh-list">
          {flights.length === 0 && <div className="fh-empty">No flights in range.</div>}
          {flights.map(f => (
            <div key={f.flight_id} className={`fh-item ${selId === f.flight_id ? 'sel' : ''}`} onClick={() => setSelId(f.flight_id)}>
              <div className="fh-item-top">
                <span className="fh-item-id">{f.drone_id}</span>
                {ago(f.end) < 3600 && <span className="fh-badge-recent">recent</span>}
                {f.threat_level && <span className="fh-badge" style={{ color: tcolor(f.threat_score) }}>{Math.round(f.threat_score || 0)}</span>}
              </div>
              <div className="fh-item-sub">
                {fmtTime(f.start)} · {fmtDur(f.duration_s)} · {f.point_count} pts · {f.node_count} node{f.node_count === 1 ? '' : 's'}
                {f.has_valid_rid === false && <span className="fh-invalid"> · invalid RID</span>}
              </div>
            </div>
          ))}
        </div>

        <div className="fh-listhead">No-RID / RF-only ({rf.length})</div>
        <div className="fh-rf">
          {rf.length === 0 && <div className="fh-empty">None in range.</div>}
          {rf.map(c => (
            <div key={c.contact_id} className="fh-rf-item" onClick={() => c.est_lat && leaflet.current?.setView([c.est_lat, c.est_lon], 15)}>
              <span className="fh-rf-sig">{RF_SIGNAL_LABELS[c.signal_type] || c.signal_type}</span>
              <span className="fh-rf-band">{c.band}</span>
              <span className="fh-rf-rssi">{c.best_rssi != null ? Math.round(c.best_rssi) + 'dBm' : '—'}</span>
              <span className="fh-rf-time">{fmtTime(c.last_seen).split(',')[1] || ''}</span>
            </div>
          ))}
        </div>
      </div>

      {/* ── right: map + replay + profile + export ── */}
      <div className="fh-main">
        <div ref={mapRef} className="fh-map" />
        {detail && (
          <div className="fh-detail">
            <div className="fh-detail-head">
              <div>
                <span className="fh-d-id">{detail.drone_id}</span>
                <span className="fh-d-meta">{detail.ua_type || ''} · {fmtTime(detail.start)} · {fmtDur(detail.duration_s)}</span>
              </div>
              <div className="fh-export">
                {['gpx', 'kml', 'csv'].map(fmt => (
                  <a key={fmt} className="fh-chip" href={`/api/flights/${encodeURIComponent(selId)}/export?format=${fmt}`} download>{fmt.toUpperCase()}</a>
                ))}
              </div>
            </div>

            <div className="fh-replay">
              <button className="fh-play" onClick={() => { if (idx >= detail.path.length - 1) setIdx(0); setPlaying(p => !p) }}>
                {playing ? '❚❚' : '▶'}
              </button>
              <input className="fh-slider" type="range" min={0} max={Math.max(detail.path.length - 1, 0)} value={idx}
                     onChange={e => { setPlaying(false); setIdx(+e.target.value) }} />
              <span className="fh-pos">{cur ? fmtTime(cur.t).split(',')[1]?.trim() : ''}</span>
            </div>
            {cur && (
              <div className="fh-readout">
                <span>ALT {fmtAlt(cur.alt_m, imperial)}</span>
                <span>SPD {fmtSpeed(cur.speed_ms, imperial)}</span>
                <span>HDG {cur.heading != null ? Math.round(cur.heading) + '°' : '—'}</span>
                <span>RSSI {cur.rssi != null ? Math.round(cur.rssi) + 'dBm' : '—'}</span>
                <span>NODE {cur.node_id || '—'}</span>
              </div>
            )}
            <div className="fh-sparks">
              <Spark values={detail.path.map(p => p.alt_m)}   label="Altitude" unit="m"   color={colors.steelBlueLight} />
              <Spark values={detail.path.map(p => p.speed_ms)} label="Speed"    unit="m/s" color={colors.olive} />
            </div>
          </div>
        )}
        {!detail && <div className="fh-pick">Select a flight to see its path, replay and profile.</div>}
      </div>
    </div>
  )
}

function tcolor(s) { return s >= 70 ? colors.threatHigh : s >= 40 ? colors.threatMed : colors.threatLow }

const css = `
.fh-root { display:grid; grid-template-columns:340px 1fr; height:100%; overflow:hidden; background:var(--bg0); }
.fh-side { border-right:1px solid var(--bg-border); display:flex; flex-direction:column; overflow:hidden; }
.fh-filters { padding:10px; border-bottom:1px solid var(--bg-border); display:flex; flex-direction:column; gap:6px; }
.fh-frow { display:flex; gap:4px; flex-wrap:wrap; }
.fh-chip { background:var(--bg1); border:1px solid var(--bg-border); color:var(--muted);
  font-family:var(--mono); font-size:11px; padding:3px 9px; border-radius:3px; cursor:pointer; text-decoration:none; }
.fh-chip.on { color:var(--text); border-color:var(--steel-blue); background:var(--bg2); }
.fh-sel { flex:1; background:var(--bg1); border:1px solid var(--bg-border); color:var(--text);
  font-family:var(--mono); font-size:11px; padding:3px 6px; border-radius:3px; }
.fh-listhead { padding:7px 10px; font-family:'Barlow Condensed',sans-serif; text-transform:uppercase;
  font-size:12px; letter-spacing:0.05em; color:var(--text-secondary); background:var(--bg1); border-bottom:1px solid var(--bg-border); }
.fh-recent { color:var(--threat-med); }
.fh-list { overflow-y:auto; flex:1; min-height:80px; }
.fh-empty { color:var(--muted); font-size:12px; padding:10px; }
.fh-item { padding:8px 10px; border-bottom:1px solid var(--bg-border); cursor:pointer; }
.fh-item:hover { background:var(--bg1); }
.fh-item.sel { background:var(--bg2); border-left:2px solid var(--steel-blue); }
.fh-item-top { display:flex; align-items:center; gap:6px; }
.fh-item-id { font-family:var(--mono); font-size:12px; color:var(--text); font-weight:700; }
.fh-badge { margin-left:auto; font-family:var(--mono); font-size:11px; font-weight:700; }
.fh-badge-recent { font-family:var(--mono); font-size:9px; color:var(--threat-med); border:1px solid var(--threat-med-border); border-radius:2px; padding:0 3px; }
.fh-item-sub { font-family:var(--mono); font-size:10px; color:var(--muted); margin-top:2px; }
.fh-invalid { color:var(--threat-high); }
.fh-rf { max-height:28%; overflow-y:auto; }
.fh-rf-item { display:grid; grid-template-columns:1fr 40px 54px 60px; gap:4px; padding:5px 10px;
  border-bottom:1px solid var(--bg-border); cursor:pointer; font-family:var(--mono); font-size:10px; }
.fh-rf-item:hover { background:var(--bg1); }
.fh-rf-sig { color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fh-rf-band { color:var(--steel-blue-light); }
.fh-rf-rssi, .fh-rf-time { color:var(--muted); }

.fh-main { position:relative; display:flex; flex-direction:column; overflow:hidden; }
.fh-map { flex:1; min-height:200px; }
.fh-pick { position:absolute; top:12px; left:50%; transform:translateX(-50%); z-index:500;
  background:var(--bg1); border:1px solid var(--bg-border); border-radius:4px; padding:6px 12px;
  font-family:var(--mono); font-size:11px; color:var(--muted); }
.fh-detail { border-top:1px solid var(--bg-border); background:var(--bg1); padding:10px 12px; }
.fh-detail-head { display:flex; justify-content:space-between; align-items:center; gap:8px; }
.fh-d-id { font-family:var(--mono); font-weight:700; color:var(--text); margin-right:8px; }
.fh-d-meta { font-family:var(--mono); font-size:10px; color:var(--muted); }
.fh-export { display:flex; gap:4px; }
.fh-replay { display:flex; align-items:center; gap:8px; margin-top:8px; }
.fh-play { background:var(--bg2); border:1px solid var(--bg-border); color:var(--text); cursor:pointer;
  border-radius:3px; width:28px; height:22px; font-size:10px; }
.fh-slider { flex:1; accent-color:var(--steel-blue); }
.fh-pos { font-family:var(--mono); font-size:10px; color:var(--text-secondary); min-width:70px; text-align:right; }
.fh-readout { display:flex; gap:14px; flex-wrap:wrap; margin-top:8px; font-family:var(--mono); font-size:10px; color:var(--text-secondary); }
.fh-sparks { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px; }
.fh-spark-lab { font-family:var(--mono); font-size:10px; color:var(--muted); display:flex; gap:6px; }
.fh-spark-unit { color:var(--text-secondary); }
.fh-spark-rng { margin-left:auto; }
.fh-spark-svg { width:100%; height:54px; display:block; background:var(--bg0); border:1px solid var(--bg-border); border-radius:3px; }
`
