// src/components/Sidebar.jsx
import { useStore, selectAllNodes, selectActiveDrones, selectOpenAlerts, useUnits } from '../store/useStore'

const css = `
.sidebar {
  grid-area: sidebar;
  background: var(--bg1);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  width: 210px;
  flex-shrink: 0;
}
.sb-nav {
  padding: 10px 8px 4px;
  border-bottom: 1px solid var(--border);
}
.sb-nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 3px;
  cursor: pointer;
  font-family: var(--cond);
  font-weight: 600;
  font-size: 11px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--text-dim);
  transition: all 0.12s;
  border: 1px solid transparent;
  margin-bottom: 2px;
}
.sb-nav-item:hover {
  background: var(--bg3);
  color: var(--text);
}
.sb-nav-item.active {
  background: var(--steel-blue-dim);
  border-color: var(--steel-blue-dim);
  color: var(--steel-blue);
}
.sb-nav-icon {
  font-size: 13px;
  width: 16px;
  text-align: center;
  flex-shrink: 0;
}
.sb-nav-badge {
  margin-left: auto;
  background: var(--danger);
  color: #fff;
  font-size: 9px;
  font-family: var(--mono);
  padding: 1px 5px;
  border-radius: 8px;
  font-weight: bold;
}
.sb-nodes {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}
.sb-section {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--muted);
  padding: 4px 14px 6px;
}
.sb-node {
  padding: 8px 12px;
  margin: 2px 6px;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.12s;
}
.sb-node:hover { background: var(--bg3); }
.sb-node.selected {
  background: rgba(74,111,165,0.06);
  border-color: var(--steel-blue-dim);
}
.sb-node.offline { opacity: 0.45; }
.sb-node-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 3px;
}
.sb-node-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.nd-online  { background: var(--node-online); box-shadow: 0 0 4px var(--node-online); }
.nd-warn    { background: var(--node-warning); box-shadow: 0 0 4px var(--node-warning); }
.nd-offline { background: var(--muted); }
.sb-node-name {
  font-family: var(--cond);
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--text);
  flex: 1;
}
.sb-node-id {
  font-family: var(--mono);
  font-size: 8px;
  color: var(--muted);
}
.sb-node-meta {
  display: flex;
  gap: 6px;
  font-family: var(--mono);
  font-size: 9px;
  color: var(--text-dim);
}
.sb-node-radios {
  display: flex;
  gap: 3px;
  margin-top: 4px;
}
.sb-radio {
  font-size: 7px;
  font-family: var(--mono);
  letter-spacing: 0.5px;
  padding: 1px 4px;
  border-radius: 2px;
  border: 1px solid;
  text-transform: uppercase;
}
.r-wifi { color: var(--ice);            border-color: var(--ice-dim);      background: rgba(74,111,165,0.06); }
.r-bt   { color: var(--steel-blue-light); border-color: var(--steel-blue-dim); background: rgba(74,111,165,0.06); }
.r-sdr  { color: var(--olive);    border-color: var(--olive-dim);    background: rgba(74,124,89,0.06); }
.sb-health {
  padding: 10px 12px;
  border-top: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 9px;
  color: var(--text-dim);
}
.sb-health-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 5px;
}
.sb-bar-track {
  flex: 1;
  height: 3px;
  background: var(--bg3);
  border-radius: 2px;
  overflow: hidden;
}
.sb-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.5s;
}
.bf-amber  { background: var(--node-warning); }
.bf-green  { background: var(--olive); }
.bf-danger { background: var(--danger); }
.sb-health-label { width: 28px; color: var(--muted); }
.sb-health-val   { width: 32px; text-align: right; color: var(--text-dim); }
`


export default function Sidebar() {
  const nodes        = useStore(selectAllNodes)
  const drones       = useStore(selectActiveDrones)
  const alerts       = useStore(selectOpenAlerts)
  const selectedNode = useStore(s => s.selectedNodeId)
  const activePanel  = useStore(s => s.activePanel)
  const setPanel     = useStore(s => s.setPanel)
  const selectNode   = useStore(s => s.selectNode)
  const serverHealth = useStore(s => s.serverHealth)
  const imperial     = useUnits()

  const highAlerts = alerts.filter(a => a.level === 'high').length

  const selected = nodes.find(n => n.node_id === selectedNode) || nodes[0]

  return (
    <>
      <style>{css}</style>
      <aside className="sidebar">
        {/* Navigation */}
        <nav className="sb-nav">
          {[
            { id: 'map',     icon: '◈', label: 'Live Map' },
            { id: 'threats', icon: '◉', label: 'Threats' },
            { id: 'packets', icon: '≋', label: 'Packet Feed' },
            { id: 'arch',    icon: '⬡', label: 'Architecture' },
          ].map(item => (
            <div
              key={item.id}
              className={`sb-nav-item ${activePanel === item.id ? 'active' : ''}`}
              onClick={() => setPanel(item.id)}
            >
              <span className="sb-nav-icon">{item.icon}</span>
              {item.label}
              {item.id === 'map' && highAlerts > 0 && (
                <span className="sb-nav-badge">{highAlerts}</span>
              )}
            </div>
          ))}
        </nav>

        {/* Node list */}
        <div className="sb-nodes">
          <div className="sb-section">ARGUS Nodes</div>
          {nodes.length === 0 && (
            <div style={{padding:'8px 14px',fontFamily:'var(--mono)',fontSize:9,color:'var(--muted)'}}>
              Awaiting node heartbeats…
            </div>
          )}
          {nodes.map(node => {
            const st = node.status === 'online' ? 'nd-online'
                     : node.status === 'warn'   ? 'nd-warn'
                                                : 'nd-offline'
            const radios = node.radios?.length ? node.radios : ['wifi','bt']
            const detections = drones.filter(d =>
              d.detecting_nodes?.includes(node.node_id)).length

            return (
              <div
                key={node.node_id}
                className={`sb-node ${selectedNode === node.node_id ? 'selected' : ''} ${node.status === 'offline' ? 'offline' : ''}`}
                onClick={() => selectNode(node.node_id)}
              >
                <div className="sb-node-header">
                  <div className={`sb-node-dot ${st}`}/>
                  <div className="sb-node-name">{node.node_id}</div>
                  <div className="sb-node-id">{detections}▲</div>
                </div>
                <div className="sb-node-meta">
                  <span style={{color:'var(--muted)'}}>
                    {node.cpu_pct != null ? `CPU ${node.cpu_pct.toFixed(0)}%` : '—'}
                  </span>
                  <span>{node.gps_fix ? '⌖ GPS' : '⌖ NO FIX'}</span>
                </div>
                <div className="sb-node-radios">
                  {radios.includes('wifi') && <span className="sb-radio r-wifi">WiFi</span>}
                  {radios.includes('bt')   && <span className="sb-radio r-bt">BT5</span>}
                  {radios.includes('sdr')  && <span className="sb-radio r-sdr">SDR</span>}
                </div>
              </div>
            )
          })}
        </div>

        {/* Server health */}
        {serverHealth && (
          <div className="sb-health" style={{borderTop:'1px solid var(--border)'}}>
            <div style={{fontFamily:'var(--cond)',fontWeight:700,fontSize:9,letterSpacing:2,textTransform:'uppercase',color:'var(--muted)',marginBottom:6}}>
              Server Health
            </div>
            {[
              { label:'CPU', val: serverHealth.cpu_pct,  cls: serverHealth.cpu_pct  > 80 ? 'bf-danger' : 'bf-amber' },
              { label:'MEM', val: serverHealth.mem_pct,  cls: serverHealth.mem_pct  > 85 ? 'bf-danger' : 'bf-green' },
              { label:'DSK', val: serverHealth.disk_pct, cls: serverHealth.disk_pct > 90 ? 'bf-danger' : 'bf-amber' },
            ].map(({ label, val, cls }) => (
              <div key={label} className="sb-health-row">
                <span className="sb-health-label">{label}</span>
                <div className="sb-bar-track">
                  <div className={`sb-bar-fill ${cls}`} style={{width:`${val}%`}}/>
                </div>
                <span className="sb-health-val">{val.toFixed(0)}%</span>
              </div>
            ))}
            <div style={{display:'flex',gap:10,marginTop:4}}>
              {[
                { label:'DB',   val: serverHealth.db   },
                { label:'MQTT', val: serverHealth.mqtt },
              ].map(({label, val}) => (
                <div key={label} style={{display:'flex',alignItems:'center',gap:4,fontFamily:'var(--mono)',fontSize:9,color:'var(--text-dim)'}}>
                  <div style={{
                    width:6,height:6,borderRadius:'50%',flexShrink:0,
                    background: val==='ok' ? 'var(--node-online)' : val==='warn' ? 'var(--node-warning)' : 'var(--danger)',
                    boxShadow: val==='ok' ? '0 0 4px var(--node-online)' : val==='warn' ? '0 0 4px var(--node-warning)' : '0 0 4px var(--danger)',
                  }}/>
                  {label}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Selected node health mini-bars */}
        {selected && selected.cpu_pct != null && (
          <div className="sb-health">
            <div style={{fontFamily:'var(--cond)',fontWeight:700,fontSize:9,letterSpacing:2,textTransform:'uppercase',color:'var(--muted)',marginBottom:6}}>
              {selected.node_id} Health
            </div>
            {[
              { label:'CPU', val: selected.cpu_pct,  cls: selected.cpu_pct > 80 ? 'bf-danger' : 'bf-amber' },
              { label:'MEM', val: selected.mem_pct,  cls: selected.mem_pct > 85 ? 'bf-danger' : 'bf-green' },
              { label:'DSK', val: selected.disk_pct, cls: selected.disk_pct > 90 ? 'bf-danger' : 'bf-amber' },
            ].map(({ label, val, cls }) => val != null && (
              <div key={label} className="sb-health-row">
                <span className="sb-health-label">{label}</span>
                <div className="sb-bar-track">
                  <div className={`sb-bar-fill ${cls}`} style={{width:`${val}%`}}/>
                </div>
                <span className="sb-health-val">{val.toFixed(0)}%</span>
              </div>
            ))}
            {selected.temp_c != null && (
              <div style={{color:'var(--muted)',marginTop:2}}>
                TEMP {imperial ? `${(selected.temp_c * 9/5 + 32).toFixed(1)}°F` : `${selected.temp_c.toFixed(1)}°C`} &nbsp; UP {fmtUptime(selected.uptime_s)}
              </div>
            )}
          </div>
        )}
      </aside>
    </>
  )
}

function fmtUptime(s) {
  if (!s) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m`
}
