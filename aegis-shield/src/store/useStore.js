// src/store/useStore.js
import { create } from 'zustand'

const WS_URL = (window.location.protocol === 'https:' ? 'wss://' : 'ws://')
  + window.location.host + '/ws'

// ── Threat helpers ─────────────────────────────────────────────────────────
export const threatColor = (score) => {
  if (score == null) return 'var(--muted)'
  if (score >= 70)   return 'var(--danger)'
  if (score >= 40)   return 'var(--amber)'
  return 'var(--phosphor)'
}
export const threatBg = (score) => {
  if (score == null) return 'transparent'
  if (score >= 70)   return 'var(--danger-glow)'
  if (score >= 40)   return 'var(--amber-glow)'
  return 'var(--phosphor-glow)'
}
export const threatLabel = (score) => {
  if (score == null) return '—'
  if (score >= 70)   return 'HIGH'
  if (score >= 40)   return 'MED'
  return 'LOW'
}

export const FACTOR_LABELS = {
  no_operator_id:       { label: 'No Operator ID',    weight: 30 },
  position_mismatch:    { label: 'Position Mismatch',  weight: 25 },
  high_altitude_no_rid: { label: 'High Alt / No RID',  weight: 15 },
  unknown_ua_type:      { label: 'Unknown UA Type',    weight:  8 },
  single_node_only:     { label: 'Single Node Only',   weight:  7 },
  high_speed:           { label: 'Speed Anomaly',      weight:  8 },
  stale_gps:            { label: 'Stale GPS',          weight:  4 },
  no_description:       { label: 'No Self-ID',         weight:  3 },
}

export const useStore = create((set, get) => ({
  imperial: true,
  toggleUnits: () => set(s => ({ imperial: !s.imperial })),
  // ── Connection ──────────────────────────────────────────────────────────
  wsStatus:  'disconnected',
  wsRetries: 0,
  _ws:       null,

  // ── Live data ───────────────────────────────────────────────────────────
  drones:        {},      // drone_id → DroneTrack (includes threat + mlat fields)
  nodes:         {},      // node_id  → Node
  alerts:        [],      // AlertOut[], max 200
  recentPackets: [],      // raw detection events, max 150
  detectionRate: 0,
  lastStateTs:   null,

  // ── Threat / MLAT (polled from REST, merged into drones) ────────────────
  threatData:    {},      // drone_id → { threat_score, threat_level, threat_factors,
                          //              mlat_lat, mlat_lon, mlat_radius_m,
                          //              mlat_mismatch_m, spoof_confidence }
  _threatPollTimer: null,

  // ── Server health (polled from /health) ─────────────────────────────────
  serverHealth:       null,   // { cpu_pct, mem_pct, disk_pct, db, mqtt }
  _healthPollTimer:   null,

  // ── UI ──────────────────────────────────────────────────────────────────
  selectedDroneId: null,
  selectedNodeId:  null,
  activePanel:     'map',   // 'map' | 'threats' | 'packets' | 'arch'
  showMlatLayer:   true,    // toggle MLAT overlays on map

  // ── WebSocket ───────────────────────────────────────────────────────────
  connect() {
    if (get().wsStatus === 'connected') return
    set({ wsStatus: 'connecting' })
    const ws = new WebSocket(WS_URL)

    ws.onopen = () => {
      set({ wsStatus: 'connected', wsRetries: 0, _ws: ws })
      get()._startThreatPoll()
      get()._startHealthPoll()
    }
    ws.onmessage = (e) => {
      try { get()._handleMessage(JSON.parse(e.data)) }
      catch {}
    }
    ws.onclose = () => {
      set({ wsStatus: 'disconnected', _ws: null })
      get()._stopThreatPoll()
      get()._stopHealthPoll()
      const retries = get().wsRetries
      const delay = Math.min(1000 * Math.pow(2, retries), 30000)
      setTimeout(() => { set({ wsRetries: retries + 1 }); get().connect() }, delay)
    }
    ws.onerror = () => ws.close()
  },

  disconnect() {
    get()._ws?.close()
    get()._stopThreatPoll()
    set({ wsStatus: 'disconnected', _ws: null })
  },

  _handleMessage(msg) {
    switch (msg.type) {
      case 'live_state':  return get()._applyLiveState(msg.payload)
      case 'detection':   return get()._addPacket(msg.payload)
      case 'alert':       return get()._addAlert(msg.payload)
      case 'node_update': return get()._updateNode(msg.payload)
    }
  },

  _applyLiveState(payload) {
    const drones = {}
    const td = get().threatData
    for (const d of payload.drones || []) {
      drones[d.drone_id] = { ...d, ...(td[d.drone_id] || {}) }
    }
    const nodes = {}
    for (const n of payload.nodes || []) nodes[n.node_id] = n
    set({ drones, nodes,
          alerts: payload.recent_alerts || [],
          detectionRate: payload.detection_rate || 0,
          lastStateTs: payload.ts })
  },

  _addPacket(pkt) {
    set(s => ({ recentPackets: [pkt, ...s.recentPackets].slice(0, 150) }))
  },
  _addAlert(alert) {
    set(s => ({ alerts: [alert, ...s.alerts].slice(0, 200) }))
  },
  _updateNode(payload) {
    set(s => ({
      nodes: {
        ...s.nodes,
        [payload.node_id]: {
          ...(s.nodes[payload.node_id] || {}),
          node_id: payload.node_id,
          status:  payload.status,
          ...(payload.data?.system || {}),
          ...(payload.data?.gps ? { lat: payload.data.gps.lat, lon: payload.data.gps.lon } : {}),
        }
      }
    }))
  },

  // ── Threat polling ───────────────────────────────────────────────────────
  _startThreatPoll() {
    get()._stopThreatPoll()
    const tick = async () => {
      try {
        const res = await fetch('/api/analysis/threats?limit=100')
        if (!res.ok) return
        const rows = await res.json()
        const td = {}
        for (const r of rows) {
          td[r.drone_id] = {
            threat_score:     r.threat_score,
            threat_level:     r.threat_level,
            threat_factors:   r.threat_factors,
            mlat_lat:         r.mlat_lat,
            mlat_lon:         r.mlat_lon,
            mlat_radius_m:    r.mlat_radius_m,
            mlat_mismatch_m:  r.mlat_mismatch_m,
            spoof_confidence: r.spoof_confidence,
            mlat_node_count:  r.mlat_node_count,
          }
        }
        set({ threatData: td })
        // Merge into existing drones map
        set(s => {
          const drones = { ...s.drones }
          for (const [id, threat] of Object.entries(td)) {
            if (drones[id]) drones[id] = { ...drones[id], ...threat }
          }
          return { drones }
        })
      } catch {}
    }
    tick()
    const timer = setInterval(tick, 3000)   // poll every 3 s
    set({ _threatPollTimer: timer })
  },

  _stopThreatPoll() {
    const t = get()._threatPollTimer
    if (t) { clearInterval(t); set({ _threatPollTimer: null }) }
  },

  // ── Server health polling ────────────────────────────────────────────────
  _startHealthPoll() {
    get()._stopHealthPoll()
    const tick = async () => {
      try {
        const res = await fetch('/health')
        if (res.ok) set({ serverHealth: await res.json() })
      } catch {}
    }
    tick()
    set({ _healthPollTimer: setInterval(tick, 10000) })
  },
  _stopHealthPoll() {
    const t = get()._healthPollTimer
    if (t) { clearInterval(t); set({ _healthPollTimer: null }) }
  },

  // ── Alert actions ────────────────────────────────────────────────────────
  async acknowledgeAlert(id) {
    // Optimistic update
    set(s => ({ alerts: s.alerts.map(a => a.id === id ? { ...a, acknowledged: true } : a) }))
    try { await fetch(`/api/alerts/${id}/acknowledge`, { method: 'POST' }) } catch {}
  },
  async acknowledgeAll() {
    set(s => ({ alerts: s.alerts.map(a => ({ ...a, acknowledged: true })) }))
    try { await fetch('/api/alerts/acknowledge-all', { method: 'POST' }) } catch {}
  },

  // ── UI actions ───────────────────────────────────────────────────────────
  selectDrone(id)       { set({ selectedDroneId: id, selectedNodeId: null }) },
  selectNode(id)        { set({ selectedNodeId: id, selectedDroneId: null }) },
  clearSelection()      { set({ selectedDroneId: null, selectedNodeId: null }) },
  setPanel(p)           { set({ activePanel: p }) },
  toggleMlatLayer()     { set(s => ({ showMlatLayer: !s.showMlatLayer })) },
}))

// ── Selectors ──────────────────────────────────────────────────────────────

export const selectActiveDrones = s =>
  Object.values(s.drones).sort((a, b) =>
    (b.threat_score ?? -1) - (a.threat_score ?? -1))

export const selectAllNodes = s =>
  Object.values(s.nodes).sort((a, b) => a.node_id.localeCompare(b.node_id))

export const selectOpenAlerts = s =>
  s.alerts.filter(a => !a.acknowledged)

export const selectHighAlerts = s =>
  s.alerts.filter(a => !a.acknowledged && a.level === 'high')

export const selectHighThreatDrones = s =>
  Object.values(s.drones)
    .filter(d => (d.threat_score ?? 0) >= 70)
    .sort((a,b) => b.threat_score - a.threat_score)

export const selectMlatDrones = s =>
  Object.values(s.drones)
    .filter(d => d.mlat_lat != null && d.mlat_lon != null)


export const useUnits = () => useStore(s => s.imperial)
export const useToggleUnits = () => useStore(s => s.toggleUnits)

export function fmtAlt(metres, imperial) {
  if (metres == null) return '—'
  if (imperial) return `${Math.round(metres * 3.28084)} ft`
  return `${Math.round(metres)} m`
}

export function fmtSpeed(ms, imperial) {
  if (ms == null) return '—'
  if (imperial) return `${(ms * 2.23694).toFixed(1)} mph`
  return `${ms.toFixed(1)} m/s`
}

export function fmtDist(metres, imperial) {
  if (metres == null) return '—'
  if (imperial) return `${Math.round(metres * 3.28084)} ft`
  return `${Math.round(metres)} m`
}

export function unitLabel(imperial) {
  return imperial ? 'imperial' : 'metric'
}
