// src/components/MapView.jsx
import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useStore, selectActiveDrones, selectAllNodes, useUnits, fmtAlt, fmtSpeed, fmtDist } from '../store/useStore'
import { colors } from '../theme/colors'

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

  const drones      = useStore(selectActiveDrones)
  const nodes       = useStore(selectAllNodes)
  const showMlat    = useStore(s => s.showMlatLayer)
  const selectDrone = useStore(s => s.selectDrone)
  const toggleMlat  = useStore(s => s.toggleMlatLayer)
  const imperial    = useUnits()

  useEffect(() => {
    if (leafletRef.current) return

    const saved   = JSON.parse(localStorage.getItem('aegis_map_view') || 'null')
    const center  = saved?.center  ?? [39.5, -98.35]
    const zoom    = saved?.zoom    ?? 5

    const map = L.map(mapRef.current, { center, zoom })
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
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
        for (const r of [markersRef, mlatMarkRef, mlatCircRef, mlatLineRef, tracksRef]) {
          if (r.current[id]) { map.removeLayer(r.current[id]); delete r.current[id] }
        }
        delete trackPtsRef.current[id]
      }
    }

    for (const drone of drones) {
      if (!drone.lat || !drone.lon) continue
      const ll = [drone.lat, drone.lon]
      const id = drone.drone_id

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
      if (markersRef.current[id].isPopupOpen()) {
        markersRef.current[id].setPopupContent(dronePopupHtml(drone, imperial))
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
  }, [drones, showMlat, selectDrone, imperial])

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
