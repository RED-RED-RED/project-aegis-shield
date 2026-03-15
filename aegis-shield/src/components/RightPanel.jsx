// src/components/RightPanel.jsx
import { useStore, selectActiveDrones, selectOpenAlerts, threatColor, threatLabel, useUnits, fmtAlt, fmtSpeed, fmtDist } from '../store/useStore'
import { formatDistanceToNow } from 'date-fns'

const css = `
.rpanel {
  grid-area: rpanel;
  background: var(--bg1);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  width: 280px;
  flex-shrink: 0;
}
.rp-section {
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.rp-section.flex1 { flex: 1; overflow: hidden; }
.rp-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px 6px;
  font-family: var(--cond);
  font-weight: 700;
  font-size: 9px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--muted);
  flex-shrink: 0;
}
.rp-count {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 8px;
  font-family: var(--mono);
  font-weight: bold;
}
.rc-amber  { background: var(--amber-glow); color: var(--amber); border: 1px solid var(--amber-dim); }
.rc-danger { background: var(--danger-glow); color: var(--danger); border: 1px solid var(--danger-dim); }
.rc-green  { background: var(--phosphor-glow); color: var(--phosphor); border: 1px solid var(--phosphor-dim); }
.rp-scroll {
  overflow-y: auto;
  flex: 1;
}

/* Drone rows */
.drone-row {
  padding: 8px 12px;
  border-bottom: 1px solid rgba(30,48,64,0.5);
  cursor: pointer;
  transition: background 0.1s;
}
.drone-row:hover    { background: var(--bg3); }
.drone-row.selected { background: rgba(232,160,32,0.06); border-left: 2px solid var(--amber); }
.dr-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 3px;
}
.dr-id {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text);
}
.dr-badge {
  font-family: var(--cond);
  font-weight: 700;
  font-size: 8px;
  letter-spacing: 1px;
  padding: 2px 6px;
  border-radius: 2px;
  text-transform: uppercase;
}
.db-ok      { background: var(--phosphor-glow); color: var(--phosphor); border: 1px solid var(--phosphor-dim); }
.db-alert   { background: var(--danger-glow);   color: var(--danger);   border: 1px solid var(--danger-dim); }
.db-unknown { background: var(--amber-glow);    color: var(--amber);    border: 1px solid var(--amber-dim); }
.dr-meta {
  display: flex;
  gap: 8px;
  font-family: var(--mono);
  font-size: 9px;
  color: var(--text-dim);
}
.dr-meta-t {
  font-size: 8px;
  color: var(--ice);
}

/* Detail grid */
.detail-section {
  padding: 10px 12px;
}
.detail-title {
  font-family: var(--cond);
  font-weight: 700;
  font-size: 9px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--amber);
  margin-bottom: 8px;
}
.detail-id {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--text);
  margin-bottom: 2px;
}
.detail-type {
  font-size: 10px;
  color: var(--text-dim);
  margin-bottom: 8px;
}
.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 5px;
  margin-bottom: 6px;
}
.detail-cell {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 5px 8px;
}
.dc-label { font-size: 7px; letter-spacing: 1px; color: var(--muted); text-transform: uppercase; margin-bottom: 2px; }
.dc-val   { font-family: var(--mono); font-size: 12px; color: var(--text); }
.dc-val.amber  { color: var(--amber); }
.dc-val.green  { color: var(--phosphor); }
.dc-val.danger { color: var(--danger); }
.dc-val.ice    { color: var(--ice); }
.detail-row {
  display: flex;
  gap: 6px;
  font-family: var(--mono);
  font-size: 10px;
  padding: 3px 0;
  border-bottom: 1px solid rgba(30,48,64,0.4);
}
.detail-row-label { color: var(--muted); width: 72px; flex-shrink: 0; }
.detail-row-val   { color: var(--text); }

/* Alerts */
.alert-row {
  padding: 7px 12px;
  border-bottom: 1px solid rgba(30,48,64,0.4);
  border-left: 3px solid transparent;
  cursor: pointer;
  transition: background 0.1s;
}
.alert-row:hover { background: var(--bg3); }
.ar-high   { border-left-color: var(--danger); }
.ar-medium { border-left-color: var(--amber); }
.ar-low    { border-left-color: var(--ice); }
.alert-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 4px;
  margin-bottom: 2px;
}
.alert-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.3;
  flex: 1;
}
.alert-time {
  font-family: var(--mono);
  font-size: 8px;
  color: var(--muted);
  flex-shrink: 0;
}
.alert-desc {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--text-dim);
  line-height: 1.4;
}
.empty-msg {
  padding: 16px 12px;
  font-family: var(--mono);
  font-size: 9px;
  color: var(--muted);
  text-align: center;
}
`

export default function RightPanel() {
  const drones     = useStore(selectActiveDrones)
  const alerts     = useStore(selectOpenAlerts)
  const selected   = useStore(s => s.selectedDroneId)
  const selectDrone = useStore(s => s.selectDrone)

  const selectedDrone = drones.find(d => d.drone_id === selected)
  const noRidCount = drones.filter(d => !d.has_valid_rid).length

  return (
    <>
      <style>{css}</style>
      <div className="rpanel">

        {/* Drone list */}
        <div className="rp-section" style={{maxHeight: selectedDrone ? '180px' : '45%'}}>
          <div className="rp-title">
            Active Drones
            <span className={`rp-count ${noRidCount > 0 ? 'rc-danger' : 'rc-amber'}`}>
              {drones.length}
            </span>
          </div>
          <div className="rp-scroll">
            {drones.length === 0 && <div className="empty-msg">No active drones</div>}
            {drones.map(drone => (
              <DroneRow
                key={drone.drone_id}
                drone={drone}
                selected={selected === drone.drone_id}
                onClick={() => selectDrone(drone.drone_id)}
              />
            ))}
          </div>
        </div>

        {/* Selected drone detail */}
        {selectedDrone && (
          <div className="rp-section">
            <DroneDetail drone={selectedDrone} />
          </div>
        )}

        {/* Alerts */}
        <div className="rp-section flex1">
          <div className="rp-title">
            Active Alerts
            <span className={`rp-count ${alerts.filter(a=>a.level==='high').length > 0 ? 'rc-danger' : 'rc-green'}`}>
              {alerts.filter(a=>a.level==='high').length} HIGH
            </span>
          </div>
          <div className="rp-scroll">
            {alerts.length === 0 && <div className="empty-msg">No active alerts</div>}
            {alerts.slice(0, 50).map(alert => (
              <AlertRow key={alert.id} alert={alert} />
            ))}
          </div>
        </div>

      </div>
    </>
  )
}

function DroneRow({ drone, selected, onClick }) {
  const noRid   = !drone.has_valid_rid
  const score   = drone.threat_score
  const sc      = threatColor(score)
  const imperial = useUnits()
  return (
    <div className={`drone-row ${selected ? 'selected' : ''}`} onClick={onClick}>
      <div className="dr-header">
        <span className="dr-id">{drone.drone_id}</span>
        <div style={{display:'flex',alignItems:'center',gap:4}}>
          {score != null && (
            <span style={{
              fontFamily:'var(--mono)',fontSize:9,fontWeight:'bold',
              color:sc,background:'rgba(0,0,0,0.4)',
              padding:'1px 5px',borderRadius:2,border:`1px solid ${sc}`,
            }}>{score.toFixed(0)}</span>
          )}
          <span className={`dr-badge ${noRid ? 'db-alert' : 'db-ok'}`}>
            {noRid ? 'NO RID' : 'OK'}
          </span>
        </div>
      </div>
      <div className="dr-meta">
        <span>▲ {fmtAlt(drone.alt_baro, imperial)}</span>
        <span>⟶ {fmtSpeed(drone.speed_h, imperial)}</span>
        <span className="dr-meta-t">{drone.last_transport?.toUpperCase() || '—'}</span>
        {drone.mlat_mismatch_m > 250 && (
          <span style={{color:'var(--danger)'}}>Δ{fmtDist(drone.mlat_mismatch_m, imperial)}</span>
        )}
      </div>
    </div>
  )
}

function DroneDetail({ drone }) {
  const noRid  = !drone.has_valid_rid
  const imperial = useUnits()
  return (
    <div className="detail-section">
      <div className="detail-title">Selected Drone</div>
      <div className="detail-id">{drone.drone_id}</div>
      <div className="detail-type">{drone.ua_type || 'Unknown type'}</div>
      <div className="detail-grid">
        <div className="detail-cell">
          <div className="dc-label">Status</div>
          <div className={`dc-val ${noRid ? 'danger' : 'green'}`}>{noRid ? '⚠ NO RID' : '✓ OK'}</div>
        </div>
        <div className="detail-cell">
          <div className="dc-label">Alt (baro)</div>
          <div className="dc-val amber">{fmtAlt(drone.alt_baro, imperial)}</div>
        </div>
        <div className="detail-cell">
          <div className="dc-label">Height AGL</div>
          <div className="dc-val amber">{fmtAlt(drone.height_agl, imperial)}</div>
        </div>
        <div className="detail-cell">
          <div className="dc-label">Speed</div>
          <div className="dc-val">{fmtSpeed(drone.speed_h, imperial)}</div>
        </div>
        <div className="detail-cell">
          <div className="dc-label">Heading</div>
          <div className="dc-val">{drone.heading != null ? drone.heading.toFixed(0)+'°' : '—'}</div>
        </div>
        <div className="detail-cell">
          <div className="dc-label">RSSI</div>
          <div className="dc-val ice">{drone.last_rssi != null ? drone.last_rssi+' dBm' : '—'}</div>
        </div>
      </div>
      <div className="detail-row">
        <span className="detail-row-label">Operator</span>
        <span className="detail-row-val" style={{color: drone.operator_id ? 'var(--phosphor)' : 'var(--danger)'}}>
          {drone.operator_id || '— NONE —'}
        </span>
      </div>
      <div className="detail-row">
        <span className="detail-row-label">ID Type</span>
        <span className="detail-row-val">{drone.id_type || '—'}</span>
      </div>
      <div className="detail-row">
        <span className="detail-row-label">Detections</span>
        <span className="detail-row-val">{drone.detection_count || 0}</span>
      </div>
      <div className="detail-row" style={{border:'none'}}>
        <span className="detail-row-label">Seen By</span>
        <span className="detail-row-val">{(drone.detecting_nodes || []).join(', ') || '—'}</span>
      </div>
    </div>
  )
}

function AlertRow({ alert }) {
  const levelClass = alert.level === 'high' ? 'ar-high' : alert.level === 'medium' ? 'ar-medium' : 'ar-low'
  let timeStr = ''
  try {
    timeStr = formatDistanceToNow(new Date(alert.created_at), { addSuffix: true })
  } catch { timeStr = '—' }

  return (
    <div className={`alert-row ${levelClass}`}>
      <div className="alert-header">
        <div className="alert-title">{alert.title}</div>
        <div className="alert-time">{timeStr}</div>
      </div>
      {alert.description && (
        <div className="alert-desc">{alert.description.slice(0, 100)}</div>
      )}
    </div>
  )
}
