// src/components/MapView.jsx
import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useStore, selectActiveDrones, selectAllNodes, useUnits, fmtAlt, fmtSpeed, fmtDist } from '../store/useStore'

// ── Threat-aware drone icon ────────────────────────────────────────────────
function droneIcon(drone, imperial = false) {
  const score = drone.threat_score
  const color = score == null
    ? (drone.has_valid_rid ? '#39ff8a' : '#ff3f5a')
    : score >= 70 ? '#ff3f5a' : score >= 40 ? '#e8a020' : '#39ff8a'

  const animKey = 'dp_' + drone.drone_id.replace(/\W/g,'_')
  const pulseAnim = score >= 70 ? `@keyframes ${animKey} {
      0%,100%{transform:scale(1);box-shadow:0 0 0 0 rgba(255,63,90,0.7),0 0 8px #ff3f5a}
      50%{transform:scale(1.25);box-shadow:0 0 0 6px transparent,0 0 14px #ff3f5a}
    }` : ''

  const scoreLabel = score != null ? `<div style="
      position:absolute;top:-15px;left:50%;transform:translateX(-50%);
      font-family:'Share Tech Mono',monospace;font-size:8px;font-weight:bold;
      color:${color};background:rgba(8,11,14,0.9);
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
  const color = spoof > 0.7 ? '#ff3f5a' : spoof > 0.4 ? '#e8a020' : '#7ecfea'
  const html = `<div style="width:10px;height:10px;border:2px dashed ${color};
    border-radius:50%;background:rgba(0,0,0,0.4);box-shadow:0 0 8px ${color};"></div>`
  return L.divIcon({ html, className:'', iconSize:[10,10], iconAnchor:[5,5] })
}

// ── Node icon ──────────────────────────────────────────────────────────────
function nodeIcon(node) {
  const on = node.status === 'online'
  const color = on ? '#e8a020' : '#3a5570'
  const rings = on ? `
    <div style="position:absolute;inset:-16px;border-radius:50%;
      border:1px solid rgba(232,160,32,0.2);animation:nodeRing 3s ease-out infinite;"></div>
    <div style="position:absolute;inset:-16px;border-radius:50%;
      border:1px solid rgba(232,160,32,0.1);animation:nodeRing 3s ease-out 1.5s infinite;"></div>` : ''
  const html = `<div style="position:relative;width:20px;height:20px;display:flex;align-items:center;justify-content:center;">
    ${rings}
    <svg width="20" height="20" viewBox="0 0 20 20">
      <polygon points="10,1 19,5.5 19,14.5 10,19 1,14.5 1,5.5"
        fill="rgba(232,160,32,0.1)" stroke="${color}" stroke-width="1.5"/>
      <circle cx="10" cy="10" r="2.5" fill="${color}"/>
    </svg></div>`
  return L.divIcon({ html, className:'', iconSize:[20,20], iconAnchor:[10,10], popupAnchor:[0,-14] })
}

// ── Drone popup ────────────────────────────────────────────────────────────
function dronePopupHtml(drone) {
  const s = drone.threat_score
  const sc = s >= 70 ? '#ff3f5a' : s >= 40 ? '#e8a020' : '#39ff8a'
  const rid = drone.has_valid_rid
    ? `<span style="color:#39ff8a">✓ COMPLIANT</span>`
    : `<span style="color:#ff3f5a">⚠ NO RID</span>`
  const mlat = drone.mlat_lat != null ? `
    <hr style="border-color:#1e3040;margin:5px 0"/>
    <div style="color:#6a8a9a;font-size:9px;letter-spacing:1px;margin-bottom:3px">MLAT ESTIMATE</div>
    <div><span style="color:#3a5570">EST   </span>${drone.mlat_lat.toFixed(5)}, ${drone.mlat_lon.toFixed(5)}</div>
    <div><span style="color:#3a5570">ΔDIST </span>
      <span style="color:${drone.mlat_mismatch_m > 250 ? '#ff3f5a' : '#39ff8a'}">
        ${fmtDist(drone.mlat_mismatch_m, imperial)}
      </span></div>
    <div><span style="color:#3a5570">SPOOF </span>
      <span style="color:${(drone.spoof_confidence??0) > 0.6 ? '#ff3f5a' : '#39ff8a'}">
        ${((drone.spoof_confidence??0)*100).toFixed(0)}%
      </span></div>
    <div><span style="color:#3a5570">±RAD  </span>${fmtDist(drone.mlat_radius_m, imperial)}</div>` : ''

  return `<div style="font-family:'Share Tech Mono',monospace;font-size:11px;line-height:1.8;min-width:220px;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
      <div style="font-family:'Barlow Condensed',sans-serif;font-weight:700;font-size:14px;
        letter-spacing:1px;color:#e8a020;text-transform:uppercase;">${drone.drone_id}</div>
      ${s != null ? `<div style="font-family:'Share Tech Mono',monospace;font-size:12px;font-weight:bold;
        color:${sc};background:rgba(0,0,0,0.5);padding:2px 8px;
        border:1px solid ${sc};border-radius:2px;">${s.toFixed(0)}/100</div>` : ''}
    </div>
    <div>${rid}</div>
    <div style="color:#6a8a9a">${drone.ua_type || 'Unknown type'}</div>
    <hr style="border-color:#1e3040;margin:5px 0"/>
    <div><span style="color:#3a5570">ALT  </span>${fmtAlt(drone.alt_baro, imperial)}</div>
    <div><span style="color:#3a5570">AGL  </span>${fmtAlt(drone.height_agl, imperial)}</div>
    <div><span style="color:#3a5570">SPD  </span>${fmtSpeed(drone.speed_h, imperial)}</div>
    <div><span style="color:#3a5570">HDG  </span>${drone.heading?.toFixed(0)??'—'}°</div>
    <div><span style="color:#3a5570">OP   </span><span style="color:#c8dce8">${drone.operator_id||'—'}</span></div>
    <div style="margin-top:3px;color:#3a5570">via ${(drone.detecting_nodes||[]).join(', ')}</div>
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
  background:rgba(13,19,24,0.9); border:1px solid var(--border);
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
  background:rgba(13,19,24,0.9); border:1px solid var(--border);
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
    const map = L.map(mapRef.current, { center:[39.5,-98.35], zoom:5 })
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map)
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
      const tc = drone.threat_score >= 70 ? 'rgba(255,63,90,0.4)'
               : drone.threat_score >= 40 ? 'rgba(232,160,32,0.35)'
               : 'rgba(57,255,138,0.3)'
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
          .bindPopup(dronePopupHtml(drone), { maxWidth:280 })
          .on('click', () => selectDrone(id))
          .addTo(map)
        markersRef.current[id] = m
      }
      if (markersRef.current[id].isPopupOpen()) {
        markersRef.current[id].setPopupContent(dronePopupHtml(drone))
      }

      // MLAT overlays
      const hasMlat = drone.mlat_lat != null && drone.mlat_lon != null
      if (hasMlat && showMlat) {
        const mll   = [drone.mlat_lat, drone.mlat_lon]
        const spoof = drone.spoof_confidence ?? 0
        const r     = Math.max(drone.mlat_radius_m ?? 150, 50)
        const cc    = spoof > 0.7 ? '#ff3f5a' : spoof > 0.4 ? '#e8a020' : '#7ecfea'

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
          const lc = mm > 500 ? '#ff3f5a' : '#e8a020'
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
            color:#e8a020;margin-bottom:4px;text-transform:uppercase;">${node.node_id}</div>
          <div style="color:${node.status==='online'?'#39ff8a':'#ff3f5a'}">${node.status?.toUpperCase()}</div>
          <div style="color:#3a5570">CPU ${node.cpu_pct?.toFixed(0)||'—'}% MEM ${node.mem_pct?.toFixed(0)||'—'}%</div>
          <div style="color:#3a5570">${node.lat?.toFixed(5)}, ${node.lon?.toFixed(5)}</div>
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
            { bg:'#39ff8a', glow:'#39ff8a', label:'Low threat' },
            { bg:'#e8a020', glow:'#e8a020', label:'Medium threat' },
            { bg:'#ff3f5a', glow:'#ff3f5a', label:'High threat / No RID' },
          ].map(i => (
            <div key={i.label} className="map-legend-item">
              <div className="legend-dot" style={{background:i.bg, boxShadow:`0 0 4px ${i.glow}`}}/>
              {i.label}
            </div>
          ))}
          <div className="map-legend-item">
            <div style={{width:9,height:9,borderRadius:'50%',border:'2px dashed #7ecfea',flexShrink:0}}/>
            MLAT estimate
          </div>
          <div className="map-legend-item">
            <div style={{width:18,height:1,borderTop:'1px dashed #e8a020',flexShrink:0}}/>
            Position mismatch
          </div>
        </div>
      </div>
    </>
  )
}
