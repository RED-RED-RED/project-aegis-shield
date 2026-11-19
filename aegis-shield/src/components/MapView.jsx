// src/components/MapView.jsx
import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useStore, selectActiveDrones, selectAllNodes, selectRfContacts, isStale,
         RF_SIGNAL_LABELS, RF_BAND_LABELS, useUnits, fmtAlt, fmtSpeed, fmtDist } from '../store/useStore'
import { colors } from '../theme/colors'

// ── RF contact marker (SDR-classified link; no exact position) ──────────────
function rfColor(c) {
  if (c.correlated === true) return colors.threatLow          // Remote ID lines up
  // Uncorrelated: color by threat score, so an uncorroborated ISM link (lone
  // 900 MHz meter, scored low) is muted, not alarming red. Only escalated
  // dark links (medium/high) read as a threat.
  const s = c.threat_score ?? 0
  return s >= 70 ? colors.threatHigh : s >= 40 ? colors.threatMed : colors.steelBlueLight
}
function rfEscalated(c) {
  return c.correlated === false && (c.threat_score ?? 0) >= 40
}
function rfIcon(c) {
  const color = rfColor(c)
  const label = RF_SIGNAL_LABELS[c.signal_type] || c.signal_type || 'RF'
  // A diamond, distinct from the round drone markers, with the signal label.
  const html = `
    <div style="position:relative;display:flex;align-items:center;justify-content:center;width:14px;height:14px;">
      <div style="
        position:absolute;top:-14px;left:50%;transform:translateX(-50%);
        font-family:'Share Tech Mono',monospace;font-size:8px;font-weight:bold;
        color:${color};background:rgba(21,24,20,0.95);padding:1px 4px;border-radius:2px;
        border:1px solid ${color};white-space:nowrap;">${label}${rfEscalated(c) ? ' ⚠' : ''}</div>
      <div style="width:10px;height:10px;background:${color};opacity:0.85;
        transform:rotate(45deg);border:1px solid ${color};box-shadow:0 0 6px ${color};"></div>
    </div>`
  return L.divIcon({ html, className:'', iconSize:[14,28], iconAnchor:[7,7], popupAnchor:[0,-16] })
}
function rfPopupHtml(c, imperial) {
  const color = rfColor(c)
  const corr = rfEscalated(c)
    ? `<span style="color:${colors.threatHigh}">NO Remote ID — possible non-compliant drone</span>`
    : c.correlated === false
      ? '<span style="color:var(--text-muted)">No Remote ID — uncorroborated (likely ground/ISM)</span>'
      : c.correlated === true
        ? `<span style="color:${colors.threatLow}">Correlates with Remote ID ${c.correlated_drone_id || ''}</span>`
        : '<span style="color:var(--text-muted)">correlation unknown</span>'
  return `
    <div style="font-family:'Share Tech Mono',monospace;font-size:10px;line-height:1.7;min-width:190px;">
      <div style="font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:13px;
        color:${color};margin-bottom:4px;text-transform:uppercase;">
        ${RF_SIGNAL_LABELS[c.signal_type] || c.signal_type} <span style="color:var(--text-muted)">${RF_BAND_LABELS[c.band]||c.band}</span></div>
      <div><span style="color:${colors.textMuted}">FREQ  </span>${c.center_freq_hz ? (c.center_freq_hz/1e6).toFixed(1)+' MHz' : '—'}</div>
      <div><span style="color:${colors.textMuted}">RSSI  </span>${c.last_rssi != null ? Math.round(c.last_rssi)+' dBm' : '—'}</div>
      <div><span style="color:${colors.textMuted}">CONF  </span>${c.confidence != null ? Math.round(c.confidence*100)+'%' : '—'}</div>
      <div><span style="color:${colors.textMuted}">SCORE </span><span style="color:${color}">${c.threat_score != null ? c.threat_score.toFixed(0) : '—'}</span></div>
      <div style="margin-top:3px;">${corr}</div>
      <div style="margin-top:3px;color:${colors.textMuted}">seen by ${(c.detecting_nodes||[]).join(', ')}</div>
      <div style="color:${colors.textMuted};font-size:8px;margin-top:2px;">ring = RSSI range estimate, not a position fix</div>
    </div>`
}

// ── Threat-aware drone icon ────────────────────────────────────────────────
function droneIcon(drone, imperial = false) {
  const score = drone.threat_score
  const color = score == null
    ? (drone.has_valid_rid ? colors.threatLow : colors.threatHigh)
    : score >= 70 ? colors.threatHigh : score >= 40 ? colors.threatMed : colors.threatLow

  const animKey = 'dp_' + drone.drone_id.replace(/\W/g,'_')
  const pulseAnim = score >= 70 ? `@keyframes ${animKey} {
      0%,100%{transform:scale(1);box-shadow:0 0 0 0 rgba(224,96,96,0.7),0 0 8px ${colors.threatHigh}}
      50%{transform:scale(1.25);box-shadow:0 0 0 6px transparent,0 0 14px ${colors.threatHigh}}
    }` : ''

  const scoreLabel = score != null ? `<div style="
      position:absolute;top:-15px;left:50%;transform:translateX(-50%);
      font-family:'Share Tech Mono',monospace;font-size:8px;font-weight:bold;
      color:${color};background:rgba(21,24,20,0.95);
      padding:1px 4px;border-radius:2px;border:1px solid ${color};white-space:nowrap;
    ">${score.toFixed(0)}</div>` : ''

  const html = `<style>${pulseAnim}</style>
    <div style="position:relative;display:flex;align-items:center;justify-content:center;width:12px;height:12px;">
      ${scoreLabel}
      <div style="width:12px;height:12px;background:${color};border-radius:50%;
        border:2px solid ${color};box-shadow:0 0 6px ${color};cursor:pointer;
        ${score >= 70 ? `animation:${animKey} 1s infinite;` : ''}
      "></div>
    </div>`

  return L.divIcon({ html, className:'', iconSize:[12,28], iconAnchor:[6,22], popupAnchor:[0,-26] })
}

// Operator plausibility: flag a pilot position implausibly far from the drone.
// Visual cue only; the server's operator_spoof alert (alert_operator_max_km) is
// authoritative. Kept in sync with the server default.
const OPERATOR_FAR_KM = 5
function haversineKm(a, b) {
  const toRad = d => d * Math.PI / 180
  const dLat = toRad(b[0] - a[0]), dLon = toRad(b[1] - a[1])
  const s = Math.sin(dLat/2) ** 2 + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon/2) ** 2
  return 2 * 6371 * Math.asin(Math.sqrt(s))
}

// ── Operator (pilot) marker ────────────────────────────────────────────────
// The ground position the drone broadcasts for its operator. Distinct from the
// drone marker: a steel-blue pin with a person glyph (red when implausibly far).
function operatorIcon(far = false) {
  const c = far ? colors.threatHigh : colors.steelBlueLight
  const html = `<div style="display:flex;flex-direction:column;align-items:center;">
    <div style="width:18px;height:18px;border-radius:50%;background:${colors.bgElevated};
      border:2px solid ${c};display:flex;align-items:center;justify-content:center;
      box-shadow:0 0 6px ${c}80;font-size:11px;line-height:1;">${far ? '⚠' : '👤'}</div>
    <div style="width:2px;height:6px;background:${c};"></div>
  </div>`
  return L.divIcon({ html, className:'', iconSize:[18,24], iconAnchor:[9,24], popupAnchor:[0,-24] })
}

function operatorPopupHtml(drone, imperial = false, distKm = null) {
  const far = distKm != null && distKm > OPERATOR_FAR_KM
  const dist = distKm == null ? '' :
    `<div><span style="color:${colors.textMuted}">DIST  </span>` +
    `<span style="color:${far ? colors.threatHigh : colors.textPrimary}">${fmtDist(distKm * 1000, imperial)}</span></div>`
  const warn = far ? `<div style="color:${colors.threatHigh};margin-top:3px;">⚠ Implausibly far — possible spoof</div>` : ''
  return `<div style="font-family:var(--mono,monospace);font-size:11px;min-width:150px;">
    <div style="font-weight:bold;letter-spacing:1px;color:${far ? colors.threatHigh : colors.steelBlueLight};text-transform:uppercase;">Operator</div>
    <div style="color:${colors.textMuted};margin-bottom:3px;">pilot / ground station</div>
    <div><span style="color:${colors.textMuted}">DRONE </span>${drone.drone_id}</div>
    <div><span style="color:${colors.textMuted}">OP ID </span>${drone.operator_id || '—'}</div>
    <div><span style="color:${colors.textMuted}">POS   </span>${drone.operator_lat.toFixed(5)}, ${drone.operator_lon.toFixed(5)}</div>
    ${dist}${warn}
  </div>`
}

// ── MLAT marker ────────────────────────────────────────────────────────────
function mlatIcon(spoof) {
  const color = spoof > 0.7 ? colors.threatHigh : spoof > 0.4 ? colors.threatMed : colors.steelBlueLight
  const html = `<div style="width:10px;height:10px;border:2px dashed ${color};
    border-radius:50%;background:rgba(0,0,0,0.4);box-shadow:0 0 8px ${color};"></div>`
  return L.divIcon({ html, className:'', iconSize:[10,10], iconAnchor:[5,5] })
}

// ── Node icon ──────────────────────────────────────────────────────────────
function nodeIcon(node) {
  const on = node.status === 'online'
  const color = on ? colors.nodeOnline : colors.nodeOffline
  const rings = on ? `
    <div style="position:absolute;inset:-16px;border-radius:50%;
      border:1px solid rgba(74,111,165,0.2);animation:nodeRing 3s ease-out infinite;"></div>
    <div style="position:absolute;inset:-16px;border-radius:50%;
      border:1px solid rgba(74,111,165,0.1);animation:nodeRing 3s ease-out 1.5s infinite;"></div>` : ''
  const html = `<div style="position:relative;width:20px;height:20px;display:flex;align-items:center;justify-content:center;">
    ${rings}
    <svg width="20" height="20" viewBox="0 0 20 20">
      <polygon points="10,1 19,5.5 19,14.5 10,19 1,14.5 1,5.5"
        fill="rgba(74,111,165,0.1)" stroke="${color}" stroke-width="1.5"/>
      <circle cx="10" cy="10" r="2.5" fill="${color}"/>
    </svg></div>`
  return L.divIcon({ html, className:'', iconSize:[20,20], iconAnchor:[10,10], popupAnchor:[0,-14] })
}

// ── Drone popup ────────────────────────────────────────────────────────────
function dronePopupHtml(drone, imperial = false) {
  const s = drone.threat_score
  const sc = s >= 70 ? colors.threatHigh : s >= 40 ? colors.threatMed : colors.threatLow
  const rid = drone.has_valid_rid
    ? `<span style="color:${colors.threatLow}">✓ COMPLIANT</span>`
    : `<span style="color:${colors.threatHigh}">⚠ NO RID</span>`
  const mlat = drone.mlat_lat != null ? `
    <hr style="border-color:${colors.bgBorder};margin:5px 0"/>
    <div style="color:${colors.textMuted};font-size:9px;letter-spacing:1px;margin-bottom:3px">MLAT ESTIMATE</div>
    <div><span style="color:${colors.textMuted}">EST   </span>${drone.mlat_lat.toFixed(5)}, ${drone.mlat_lon.toFixed(5)}</div>
    <div><span style="color:${colors.textMuted}">ΔDIST </span>
      <span style="color:${drone.mlat_mismatch_m > 250 ? colors.threatHigh : colors.threatLow}">
        ${fmtDist(drone.mlat_mismatch_m, imperial)}
      </span></div>
    <div><span style="color:${colors.textMuted}">SPOOF </span>
      <span style="color:${(drone.spoof_confidence??0) > 0.6 ? colors.threatHigh : colors.threatLow}">
        ${((drone.spoof_confidence??0)*100).toFixed(0)}%
      </span></div>
    <div><span style="color:${colors.textMuted}">±RAD  </span>${fmtDist(drone.mlat_radius_m, imperial)}</div>` : ''

  return `<div style="font-family:'Share Tech Mono',monospace;font-size:11px;line-height:1.8;min-width:220px;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
      <div style="font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:14px;
        letter-spacing:1px;color:${colors.steelBlueLight};text-transform:uppercase;">${drone.drone_id}</div>
      ${s != null ? `<div style="font-family:'Share Tech Mono',monospace;font-size:12px;font-weight:bold;
        color:${sc};background:rgba(0,0,0,0.5);padding:2px 8px;
        border:1px solid ${sc};border-radius:2px;">${s.toFixed(0)}/100</div>` : ''}
    </div>
    <div>${rid}</div>
    <div style="color:${colors.textMuted}">${drone.ua_type || 'Unknown type'}</div>
    <hr style="border-color:${colors.bgBorder};margin:5px 0"/>
    <div><span style="color:${colors.textMuted}">ALT  </span>${fmtAlt(drone.alt_baro, imperial)}</div>
    <div><span style="color:${colors.textMuted}">AGL  </span>${fmtAlt(drone.height_agl, imperial)}</div>
    <div><span style="color:${colors.textMuted}">SPD  </span>${fmtSpeed(drone.speed_h, imperial)}</div>
    <div><span style="color:${colors.textMuted}">HDG  </span>${drone.heading?.toFixed(0)??'—'}°</div>
    <div><span style="color:${colors.textMuted}">OP   </span><span style="color:${colors.textPrimary}">${drone.operator_id||'—'}</span></div>
    <div style="margin-top:3px;color:${colors.textMuted}">via ${(drone.detecting_nodes||[]).join(', ')}</div>
    ${mlat}
  </div>`
}

// ── CSS ────────────────────────────────────────────────────────────────────
const css = `
@keyframes nodeRing { 0%{transform:scale(0.3);opacity:0.8} 100%{transform:scale(1);opacity:0} }
.map-wrap { position:relative; width:100%; height:100%; }
.map-wrap .leaflet-container { width:100%; height:100%; }
.map-legend {
  position:absolute; bottom:24px; left:14px; z-index:800;
  background:rgba(21,24,20,0.92); border:1px solid var(--border);
  border-radius:4px; padding:10px 12px; backdrop-filter:blur(8px);
  font-family:var(--mono); font-size:9px;
}
.map-legend-title {
  font-family:var(--cond); font-weight:700; font-size:9px; letter-spacing:2px;
  text-transform:uppercase; color:var(--muted); margin-bottom:7px;
}
.map-legend-item { display:flex; align-items:center; gap:7px; color:var(--text-dim); margin-bottom:4px; }
.legend-dot { width:9px; height:9px; border-radius:50%; flex-shrink:0; }
.map-mlat-toggle {
  position:absolute; top:56px; right:10px; z-index:800;
  background:rgba(21,24,20,0.92); border:1px solid var(--border);
  border-radius:4px; padding:5px 10px; font-family:var(--mono); font-size:9px;
  color:var(--muted); cursor:pointer; display:flex; align-items:center; gap:6px;
  backdrop-filter:blur(8px); transition:all 0.15s;
}
.map-mlat-toggle:hover { border-color:var(--border-hi); color:var(--text); }
.map-mlat-toggle.on { border-color:var(--ice-dim); color:var(--ice); }
.mlat-dot { width:7px; height:7px; border-radius:50%; border:2px dashed var(--ice); flex-shrink:0; }
`

// ── Component ──────────────────────────────────────────────────────────────
export default function MapView() {
  const mapRef      = useRef(null)
  const leafletRef  = useRef(null)
  const markersRef  = useRef({})
  const mlatMarkRef = useRef({})
  const mlatCircRef = useRef({})
  const mlatLineRef = useRef({})
  const nodeMarkRef = useRef({})
  const tracksRef   = useRef({})
  const trackPtsRef = useRef({})
  const rfMarkRef   = useRef({})
  const rfCircRef   = useRef({})
  const opMarkRef   = useRef({})
  const opLineRef   = useRef({})

  const drones      = useStore(selectActiveDrones)
  const nodes       = useStore(selectAllNodes)
  const rfContacts  = useStore(selectRfContacts)
  const showMlat    = useStore(s => s.showMlatLayer)
  const selectDrone = useStore(s => s.selectDrone)
  const toggleMlat  = useStore(s => s.toggleMlatLayer)
  const liveActiveS = useStore(s => s.liveActiveS)
  const imperial    = useUnits()

  useEffect(() => {
    if (leafletRef.current) return

    const saved   = JSON.parse(localStorage.getItem('aegis_map_view') || 'null')
    const center  = saved?.center  ?? [39.5, -98.35]
    const zoom    = saved?.zoom    ?? 5

    const map = L.map(mapRef.current, { center, zoom })
    // CARTO raster basemaps require an API key since Sept 2026.  The key is
    // read at build time from VITE_CARTO_API_KEY (aegis-shield/.env.local,
    // written by deploy-aegis-server.sh; never commit it).
    const cartoKey = import.meta.env.VITE_CARTO_API_KEY
    if (!cartoKey) {
      console.warn('VITE_CARTO_API_KEY is not set; the CARTO basemap will show "API KEY REQUIRED". See aegis-shield/.env.example.')
    }
    const keyParam = cartoKey ? `?key=${encodeURIComponent(cartoKey)}` : ''
    L.tileLayer(`https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}{r}.png${keyParam}`, {
      attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
      maxZoom: 19,
    }).addTo(map)

    map.on('moveend', () => {
      const c = map.getCenter()
      localStorage.setItem('aegis_map_view', JSON.stringify({
        center: [c.lat, c.lng],
        zoom:   map.getZoom(),
      }))
    })

    leafletRef.current = map
    return () => { map.remove(); leafletRef.current = null }
  }, [])

  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    const live = new Set(drones.map(d => d.drone_id))

    for (const id of Object.keys(markersRef.current)) {
      if (!live.has(id)) {
        for (const r of [markersRef, mlatMarkRef, mlatCircRef, mlatLineRef, tracksRef, opMarkRef, opLineRef]) {
          if (r.current[id]) { map.removeLayer(r.current[id]); delete r.current[id] }
        }
        delete trackPtsRef.current[id]
      }
    }

    for (const drone of drones) {
      if (!drone.lat || !drone.lon) continue
      const ll = [drone.lat, drone.lon]
      const id = drone.drone_id
      const stale = isStale(drone, liveActiveS)
      const op = stale ? 0.4 : 1.0

      // Track
      if (!trackPtsRef.current[id]) trackPtsRef.current[id] = []
      const pts = trackPtsRef.current[id]
      const last = pts[pts.length-1]
      if (!last || last[0] !== drone.lat || last[1] !== drone.lon) {
        pts.push(ll); if (pts.length > 60) pts.shift()
      }
      const tc = drone.threat_score >= 70 ? 'rgba(224,96,96,0.4)'
               : drone.threat_score >= 40 ? 'rgba(200,146,74,0.35)'
               : 'rgba(74,124,89,0.3)'
      if (tracksRef.current[id]) {
        tracksRef.current[id].setLatLngs(pts).setStyle({ color: tc })
      } else if (pts.length > 1) {
        tracksRef.current[id] = L.polyline(pts, { color:tc, weight:1.5, dashArray:'4 3' }).addTo(map)
      }

      // Broadcast marker
      if (markersRef.current[id]) {
        markersRef.current[id].setLatLng(ll).setIcon(droneIcon(drone, imperial))
      } else {
        const m = L.marker(ll, { icon:droneIcon(drone, imperial), zIndexOffset:10 })
          .bindPopup(dronePopupHtml(drone, imperial), { maxWidth:280 })
          .on('click', () => selectDrone(id))
          .addTo(map)
        markersRef.current[id] = m
      }
      markersRef.current[id].setOpacity(op)
      if (tracksRef.current[id]) tracksRef.current[id].setStyle({ opacity: stale ? 0.25 : 1 })
      if (markersRef.current[id].isPopupOpen()) {
        markersRef.current[id].setPopupContent(dronePopupHtml(drone, imperial))
      }

      // Operator (pilot) position, when the drone broadcasts it
      const hasOp = drone.operator_lat != null && drone.operator_lon != null &&
                    !(drone.operator_lat === 0 && drone.operator_lon === 0)
      if (hasOp) {
        const oll = [drone.operator_lat, drone.operator_lon]
        const opKm = haversineKm(ll, oll)
        const opFar = opKm > OPERATOR_FAR_KM
        const tether = opFar ? colors.threatHigh : colors.steelBlueLight
        if (opMarkRef.current[id]) {
          opMarkRef.current[id].setLatLng(oll).setIcon(operatorIcon(opFar))
        } else {
          opMarkRef.current[id] = L.marker(oll, { icon:operatorIcon(opFar), zIndexOffset:8 })
            .bindPopup(operatorPopupHtml(drone, imperial, opKm), { maxWidth:240 })
            .on('click', () => selectDrone(id))
            .addTo(map)
        }
        opMarkRef.current[id].setOpacity(op)
        if (opMarkRef.current[id].isPopupOpen()) {
          opMarkRef.current[id].setPopupContent(operatorPopupHtml(drone, imperial, opKm))
        }
        // Tether line drone -> operator (red when implausibly far)
        if (opLineRef.current[id]) {
          opLineRef.current[id].setLatLngs([ll, oll]).setStyle({ color:tether })
        } else {
          opLineRef.current[id] = L.polyline([ll, oll], {
            color:tether, weight:1, dashArray:'3 4', opacity:0.6
          }).addTo(map)
        }
        opLineRef.current[id].setStyle({ opacity: stale ? 0.2 : (opFar ? 0.8 : 0.6) })
      } else {
        for (const r of [opMarkRef, opLineRef]) {
          if (r.current[id]) { map.removeLayer(r.current[id]); delete r.current[id] }
        }
      }

      // MLAT overlays
      const hasMlat = drone.mlat_lat != null && drone.mlat_lon != null
      if (hasMlat && showMlat) {
        const mll   = [drone.mlat_lat, drone.mlat_lon]
        const spoof = drone.spoof_confidence ?? 0
        const r     = Math.max(drone.mlat_radius_m ?? 150, 50)
        const cc    = spoof > 0.7 ? colors.threatHigh : spoof > 0.4 ? colors.threatMed : colors.steelBlueLight

        if (mlatMarkRef.current[id]) {
          mlatMarkRef.current[id].setLatLng(mll).setIcon(mlatIcon(spoof))
          mlatMarkRef.current[id].setTooltipContent(`MLAT — ${id}<br/>Δ${fmtDist(drone.mlat_mismatch_m, imperial)} mismatch`)
        } else {
          mlatMarkRef.current[id] = L.marker(mll, { icon:mlatIcon(spoof), zIndexOffset:5 })
            .bindTooltip(`MLAT — ${id}<br/>Δ${fmtDist(drone.mlat_mismatch_m, imperial)} mismatch`, {
              direction:'top', opacity:0.9
            }).addTo(map)
        }

        if (mlatCircRef.current[id]) {
          mlatCircRef.current[id].setLatLng(mll).setRadius(r).setStyle({ color:cc })
        } else {
          mlatCircRef.current[id] = L.circle(mll, {
            radius:r, color:cc, weight:1, dashArray:'4 4',
            fillColor:cc, fillOpacity:0.04,
          }).addTo(map)
        }

        const mm = drone.mlat_mismatch_m ?? 0
        if (mm > 100) {
          const lc = mm > 500 ? colors.threatHigh : colors.threatMed
          if (mlatLineRef.current[id]) {
            mlatLineRef.current[id].setLatLngs([ll, mll]).setStyle({ color:lc })
          } else {
            mlatLineRef.current[id] = L.polyline([ll, mll], {
              color:lc, weight:1, dashArray:'2 5', opacity:0.7
            }).addTo(map)
          }
        }
      } else {
        for (const r of [mlatMarkRef, mlatCircRef, mlatLineRef]) {
          if (r.current[id]) { map.removeLayer(r.current[id]); delete r.current[id] }
        }
      }
    }
  }, [drones, showMlat, selectDrone, imperial, liveActiveS])

  // ── RF contacts (SDR links) ───────────────────────────────────────────────
  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    const live = new Set(rfContacts.map(c => c.contact_id))

    for (const id of Object.keys(rfMarkRef.current)) {
      if (!live.has(id)) {
        for (const r of [rfMarkRef, rfCircRef]) {
          if (r.current[id]) { map.removeLayer(r.current[id]); delete r.current[id] }
        }
      }
    }

    for (const c of rfContacts) {
      if (c.est_lat == null || c.est_lon == null) continue
      const id = c.contact_id
      const ll = [c.est_lat, c.est_lon]
      const color = rfColor(c)
      const radius = Math.max(c.est_radius_m ?? 500, 50)
      const stale = isStale(c, liveActiveS)

      if (rfCircRef.current[id]) {
        rfCircRef.current[id].setLatLng(ll).setRadius(radius)
          .setStyle({ color, fillColor: color, opacity: stale ? 0.3 : 1, fillOpacity: stale ? 0.02 : 0.05 })
      } else {
        rfCircRef.current[id] = L.circle(ll, {
          radius, color, weight: 1, dashArray: '2 6',
          fillColor: color, fillOpacity: 0.05,
        }).addTo(map)
      }

      if (rfMarkRef.current[id]) {
        rfMarkRef.current[id].setLatLng(ll).setIcon(rfIcon(c))
        if (rfMarkRef.current[id].isPopupOpen())
          rfMarkRef.current[id].setPopupContent(rfPopupHtml(c, imperial))
      } else {
        rfMarkRef.current[id] = L.marker(ll, { icon: rfIcon(c), zIndexOffset: 8 })
          .bindPopup(rfPopupHtml(c, imperial), { maxWidth: 260 })
          .addTo(map)
      }
      rfMarkRef.current[id].setOpacity(stale ? 0.4 : 1)
    }
  }, [rfContacts, imperial, liveActiveS])

  useEffect(() => {
    const map = leafletRef.current
    if (!map) return
    for (const node of nodes) {
      if (!node.lat || !node.lon) continue
      const popupHtml = `
        <div style="font-family:'Share Tech Mono',monospace;font-size:10px;line-height:1.7;">
          <div style="font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:13px;
            color:${colors.steelBlueLight};margin-bottom:4px;text-transform:uppercase;">${node.node_id}</div>
          <div style="color:${node.status==='online'?colors.nodeOnline:colors.threatHigh}">${node.status?.toUpperCase()}</div>
          <div style="color:${colors.textMuted}">CPU ${node.cpu_pct?.toFixed(0)||'—'}% MEM ${node.mem_pct?.toFixed(0)||'—'}%</div>
          <div style="color:${colors.textMuted}">${node.lat?.toFixed(5)}, ${node.lon?.toFixed(5)}</div>
        </div>`
      if (nodeMarkRef.current[node.node_id]) {
        nodeMarkRef.current[node.node_id].setLatLng([node.lat,node.lon]).setIcon(nodeIcon(node))
      } else {
        nodeMarkRef.current[node.node_id] = L.marker([node.lat,node.lon], {
          icon:nodeIcon(node), zIndexOffset:-10
        }).bindPopup(popupHtml).addTo(map)
      }
    }
  }, [nodes])

  return (
    <>
      <style>{css}</style>
      <div className="map-wrap">
        <div ref={mapRef} style={{width:'100%',height:'100%'}}/>
        <div className={`map-mlat-toggle ${showMlat ? 'on' : ''}`} onClick={toggleMlat}>
          <div className="mlat-dot"/>
          MLAT {showMlat ? 'ON' : 'OFF'}
        </div>
        <div className="map-legend">
          <div className="map-legend-title">Legend</div>
          {[
            { bg:colors.threatLow,  glow:colors.threatLow,  label:'Low threat' },
            { bg:colors.threatMed,  glow:colors.threatMed,  label:'Medium threat' },
            { bg:colors.threatHigh, glow:colors.threatHigh, label:'High threat / No RID' },
          ].map(i => (
            <div key={i.label} className="map-legend-item">
              <div className="legend-dot" style={{background:i.bg, boxShadow:`0 0 4px ${i.glow}`}}/>
              {i.label}
            </div>
          ))}
          <div className="map-legend-item">
            <div style={{width:9,height:9,background:'var(--amber)',transform:'rotate(45deg)',
              boxShadow:'0 0 4px var(--amber)',flexShrink:0}}/>
            RF link (SDR)
          </div>
          <div className="map-legend-item">
            <div style={{width:9,height:9,borderRadius:'50%',border:`2px dashed ${colors.steelBlueLight}`,flexShrink:0}}/>
            MLAT estimate
          </div>
          <div className="map-legend-item">
            <div style={{width:18,height:1,borderTop:`1px dashed ${colors.threatMed}`,flexShrink:0}}/>
            Position mismatch
          </div>
        </div>
      </div>
    </>
  )
}
