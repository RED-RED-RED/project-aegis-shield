// src/components/ArchView.jsx
const css = `
.arch-wrap {
  width: 100%; height: 100%;
  overflow: auto;
  padding: 32px 40px;
  background: var(--bg0);
}
.arch-title {
  font-family: var(--cond);
  font-weight: 900;
  font-size: 11px;
  letter-spacing: 4px;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 32px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.arch-title::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}
.arch-diagram {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0;
  max-width: 760px;
  margin: 0 auto;
}
.arch-box {
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 14px 18px;
  background: var(--bg2);
  text-align: center;
  transition: border-color 0.2s, box-shadow 0.2s;
  position: relative;
}
.arch-box:hover {
  border-color: var(--steel-blue-dim);
  box-shadow: 0 0 12px rgba(74,111,165,0.1);
}
.arch-box.server {
  border-color: var(--steel-blue-dim);
  background: rgba(74,111,165,0.04);
  box-shadow: 0 0 16px rgba(74,111,165,0.08);
  min-width: 380px;
}
.arch-box.node {
  border-color: var(--olive-dim);
  background: rgba(74,124,89,0.04);
  min-width: 130px;
}
.arch-box.radio {
  min-width: 88px;
  padding: 8px 10px;
}
.arch-box-title {
  font-family: var(--cond);
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--text);
  margin-bottom: 5px;
}
.arch-box-sub {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--text-dim);
  line-height: 1.8;
}
.arch-box-sub.amber { color: var(--amber); }
.arch-arrow {
  width: 1px;
  height: 28px;
  background: linear-gradient(var(--steel-blue-dim), var(--olive-dim));
  margin: 0 auto;
  position: relative;
}
.arch-arrow::after {
  content: '';
  position: absolute;
  bottom: -4px; left: -3px;
  width: 7px; height: 7px;
  border-right: 1px solid var(--olive-dim);
  border-bottom: 1px solid var(--olive-dim);
  transform: rotate(45deg);
}
.arch-nodes-row {
  display: flex;
  gap: 14px;
  justify-content: center;
  align-items: flex-start;
}
.arch-node-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0;
}
.arch-radios-row {
  display: flex;
  gap: 5px;
  margin-top: 0;
}
.arch-radio-arrow {
  width: 1px; height: 16px;
  background: var(--olive-dim);
  margin: 0 auto;
}
.arch-label {
  font-family: var(--mono);
  font-size: 8px;
  color: var(--muted);
  background: var(--bg0);
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 2px;
  text-align: center;
}
.code-block {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 12px 16px;
  font-family: var(--mono);
  font-size: 10px;
  line-height: 1.8;
  width: 100%;
  max-width: 760px;
  margin: 0 auto;
}
.ck { color: var(--ice); }
.cv { color: var(--olive); }
.cs { color: var(--amber); }
.cm { color: var(--muted); }
.arch-section-label {
  font-family: var(--cond);
  font-weight: 700;
  font-size: 9px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--muted);
  margin: 24px 0 12px;
  width: 100%;
  max-width: 760px;
}
.flow-diagram {
  display: flex;
  align-items: center;
  gap: 0;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
  width: 100%;
  max-width: 760px;
}
.flow-step {
  flex: 1;
  padding: 10px 14px;
  text-align: center;
  border-right: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 9px;
}
.flow-step:last-child { border-right: none; }
.flow-step-name {
  font-family: var(--cond);
  font-weight: 700;
  font-size: 10px;
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-bottom: 3px;
}
.fs-amber { color: var(--amber); }
.fs-purple { color: var(--steel-blue-light); }
.fs-ice { color: var(--ice); }
.fs-green { color: var(--olive); }
.flow-arrow-cell {
  width: 20px;
  text-align: center;
  color: var(--muted);
  font-size: 14px;
  flex-shrink: 0;
}
`

const nodes = [
  { id: 'ARGUS-01', radios: ['WiFi NAN\nAlfa AWUS036ACM\nmt76x2u driver', 'BT5 LR\nnRF52840\nCoded PHY', 'RTL-SDR\nv3 dongle\n2.4GHz sweep'] },
  { id: 'ARGUS-02', radios: ['WiFi NAN\nAlfa AWUS036ACM\nmt76x2u driver', 'BT5 LR\nnRF52840\nCoded PHY'] },
  { id: 'ARGUS-03', radios: ['WiFi NAN\nAlfa AWUS036ACM\nmt76x2u driver', 'BT5 LR\nnRF52840\nCoded PHY', 'RTL-SDR\nv3 dongle\n2.4GHz sweep'] },
]

export default function ArchView() {
  return (
    <>
      <style>{css}</style>
      <div className="arch-wrap">
        <div className="arch-title">System Architecture</div>

        {/* Data flow */}
        <div className="flow-diagram">
          {[
            { name:'Pi Node', sub:'Scanner processes', cls:'fs-purple' },
            { name:'OpenDroneID', sub:'Frame parser', cls:'fs-ice' },
            { name:'paho-mqtt', sub:'MQTT publish', cls:'fs-amber' },
            { name:'Mosquitto', sub:'Broker', cls:'fs-amber' },
            { name:'FastAPI', sub:'MQTT consumer', cls:'fs-ice' },
            { name:'TimescaleDB', sub:'Hypertable store', cls:'fs-green' },
            { name:'WebSocket', sub:'Live push /ws', cls:'fs-green' },
          ].map((step, i, arr) => (
            <>
              <div key={step.name} className="flow-step">
                <div className={`flow-step-name ${step.cls}`}>{step.name}</div>
                <div style={{color:'var(--muted)',fontSize:8}}>{step.sub}</div>
              </div>
              {i < arr.length - 1 && <div key={`a${i}`} className="flow-arrow-cell">›</div>}
            </>
          ))}
        </div>

        {/* Architecture diagram */}
        <div className="arch-section-label">Network Topology</div>
        <div className="arch-diagram">
          {/* Server */}
          <div className="arch-box server">
            <div className="arch-box-title">⬡ AEGIS Platform</div>
            <div className="arch-box-sub amber">Raspberry Pi 4 (4–8GB RAM) or x86 mini-PC</div>
            <div className="arch-box-sub" style={{marginTop:4}}>
              TimescaleDB (hypertable: detections) · Mosquitto MQTT<br/>
              FastAPI + WebSocket · React AEGIS Shield · Nginx
            </div>
          </div>

          <div className="arch-arrow"/>

          {/* Network layer */}
          <div style={{display:'flex',alignItems:'center',gap:0,width:'100%',justifyContent:'center'}}>
            <div className="arch-label">LAN / VPN (WireGuard) &nbsp;·&nbsp; MQTT QoS-1 &nbsp;·&nbsp; JSON payloads</div>
          </div>

          <div className="arch-arrow"/>

          {/* Nodes */}
          <div className="arch-nodes-row">
            {nodes.map(node => (
              <div key={node.id} className="arch-node-col">
                <div className="arch-box node">
                  <div className="arch-box-title" style={{fontSize:11}}>{node.id}</div>
                  <div className="arch-box-sub">RPi Zero 2W<br/>Python 3.12<br/>GPS + NTP</div>
                </div>
                <div className="arch-radio-arrow"/>
                <div className="arch-radios-row">
                  {node.radios.map((r, i) => (
                    <div key={i} className="arch-box radio">
                      <div className="arch-box-title" style={{fontSize:9,color: i===0?'var(--ice)':i===1?'var(--steel-blue-light)':'var(--olive)'}}>
                        {r.split('\n')[0]}
                      </div>
                      <div className="arch-box-sub" style={{fontSize:8}}>
                        {r.split('\n').slice(1).map((l,j)=><div key={j}>{l}</div>)}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Code stack */}
        <div className="arch-section-label">Software Stack</div>
        <div className="code-block">
          <span className="cm"># Node agent (each Pi)</span><br/>
          <span className="ck">wifi_scanner</span>  = <span className="cv">scapy</span> + monitor_mode  <span className="cm"># 802.11 NAN action frames</span><br/>
          <span className="ck">bt_scanner</span>    = <span className="cv">bleak</span> + raw_hci        <span className="cm"># BT4 legacy + BT5 Coded PHY</span><br/>
          <span className="ck">parser</span>        = <span className="cv">opendroneid-python</span>      <span className="cm"># ASTM F3411-22a decode</span><br/>
          <span className="ck">publisher</span>     = <span className="cv">paho-mqtt</span>  QoS=1<br/>
          <span className="ck">gps</span>           = <span className="cv">gpsd</span> + <span className="cv">pynmea2</span>  /dev/ttyAMA0<br/>
          <br/>
          <span className="cm"># Central server</span><br/>
          <span className="ck">broker</span>        = <span className="cv">mosquitto</span>  2.x  <span className="cm"># with auth + LWT</span><br/>
          <span className="ck">db</span>            = <span className="cv">TimescaleDB</span> 16  <span className="cm"># hypertable: 1-day chunks</span><br/>
          <span className="ck">api</span>           = <span className="cv">FastAPI</span> + <span className="cv">asyncpg</span> + <span className="cv">aiomqtt</span><br/>
          <span className="ck">websocket</span>     = <span className="cs">ws://aegis-server/ws</span>  <span className="cm"># 500ms live state push</span><br/>
          <span className="ck">ui</span>            = <span className="cv">React 18</span> + <span className="cv">Leaflet</span> + <span className="cv">Zustand</span> + <span className="cv">Vite</span>
        </div>

        {/* MQTT schema */}
        <div className="arch-section-label">MQTT Topic Schema</div>
        <div className="code-block">
          <span className="cs">argus/&lt;node_id&gt;/detection</span>  <span className="cm">QoS=1  full RID frame + node GPS + RSSI</span><br/>
          <span className="cs">argus/&lt;node_id&gt;/heartbeat</span>  <span className="cm">QoS=0  CPU / mem / temp / GPS every 10s</span><br/>
          <span className="cs">argus/&lt;node_id&gt;/status</span>     <span className="cm">QoS=1  retained  online|offline (LWT)</span><br/>
          <span className="cs">argus/&lt;node_id&gt;/rf_event</span>   <span className="cm">QoS=0  SDR RF burst anomaly (optional)</span>
        </div>

      </div>
    </>
  )
}
