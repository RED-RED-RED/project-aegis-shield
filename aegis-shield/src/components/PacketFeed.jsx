// src/components/PacketFeed.jsx
import { useEffect, useRef } from 'react'
import { useStore } from '../store/useStore'

const css = `
.pf-wrap {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg0);
}
.pf-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--bg1);
  flex-shrink: 0;
}
.pf-toolbar-label {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--muted);
}
.pf-count {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--amber);
}
.pf-cols {
  display: grid;
  grid-template-columns: 68px 58px 44px 38px 1fr;
  gap: 0 10px;
  padding: 4px 14px;
  border-bottom: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--muted);
  flex-shrink: 0;
}
.pf-feed {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.pf-row {
  display: grid;
  grid-template-columns: 68px 58px 44px 38px 1fr;
  gap: 0 10px;
  padding: 2px 14px;
  font-family: var(--mono);
  font-size: 10px;
  line-height: 1.6;
  border-bottom: 1px solid rgba(30,48,64,0.25);
  transition: background 0.1s;
}
.pf-row:hover { background: var(--bg2); }
.pf-row.new   { animation: sweep-in 0.2s ease; }
.pf-time  { color: var(--muted); }
.pf-node  { color: var(--amber); }
.pf-type  {}
.pf-wifi  { color: var(--ice); }
.pf-bt    { color: #b39ddb; }
.pf-band  {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 8px;
  font-family: var(--mono);
  padding: 1px 4px;
  border-radius: 3px;
  letter-spacing: 0;
  line-height: 1.4;
  white-space: nowrap;
  align-self: center;
}
.pf-band-24  { background: rgba(30,120,200,0.25); color: #64b5f6; }
.pf-band-5   { background: rgba(130,60,200,0.25); color: #ce93d8; }
.pf-band-unk { background: rgba(80,80,80,0.2);    color: var(--muted); }
.pf-data  { color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pf-drone { color: var(--text); }
`

export default function PacketFeed() {
  const packets  = useStore(s => s.recentPackets)
  const feedRef  = useRef(null)
  const prevLen  = useRef(0)

  // Auto-scroll to bottom when new packets arrive
  useEffect(() => {
    if (packets.length > prevLen.current && feedRef.current) {
      feedRef.current.scrollTop = 0   // newest at top
    }
    prevLen.current = packets.length
  }, [packets.length])

  return (
    <>
      <style>{css}</style>
      <div className="pf-wrap">
        <div className="pf-toolbar">
          <span className="pf-toolbar-label">Live Packet Feed</span>
          <span className="pf-count">{packets.length} events</span>
        </div>
        <div className="pf-cols">
          <span>Time</span>
          <span>Node</span>
          <span>Radio</span>
          <span>Band</span>
          <span>Data</span>
        </div>
        <div className="pf-feed" ref={feedRef}>
          {packets.length === 0 && (
            <div style={{padding:'16px 14px',fontFamily:'var(--mono)',fontSize:9,color:'var(--muted)'}}>
              Waiting for detections…
            </div>
          )}
          {packets.map((pkt, i) => {
            const t = new Date(pkt.ts * 1000)
            const timeStr = [t.getHours(), t.getMinutes(), t.getSeconds()]
              .map(n => n.toString().padStart(2,'0')).join(':')
            const isNew = i < 3

            return (
              <div key={`${pkt.ts}-${i}`} className={`pf-row ${isNew ? 'new' : ''}`}>
                <span className="pf-time">{timeStr}</span>
                <span className="pf-node">{pkt.node_id}</span>
                <span className={`pf-type ${pkt.transport === 'wifi_nan' ? 'pf-wifi' : 'pf-bt'}`}>
                  {pkt.transport === 'wifi_nan' ? 'WiFi' : 'BT'}
                </span>
                <span className={`pf-band ${
                  pkt.band === '2.4' ? 'pf-band-24' :
                  pkt.band === '5'   ? 'pf-band-5'  : 'pf-band-unk'
                }`}>
                  {pkt.band ?? '—'}
                </span>
                <span className="pf-data">
                  <span className="pf-drone">{pkt.drone_id}</span>
                  {' '}
                  {pkt.rssi != null && `rssi=${pkt.rssi}dBm`}
                  {' '}
                  {pkt.alt_baro != null && `alt=${pkt.alt_baro.toFixed(0)}m`}
                  {' '}
                  {pkt.speed_h != null && `spd=${pkt.speed_h.toFixed(1)}m/s`}
                  {' '}
                  {pkt.status && `[${pkt.status}]`}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </>
  )
}
