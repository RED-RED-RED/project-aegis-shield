// src/App.jsx
import { useEffect } from 'react'
import { useStore } from './store/useStore'
import Topbar      from './components/Topbar'
import Sidebar     from './components/Sidebar'
import RightPanel  from './components/RightPanel'
import MapView     from './components/MapView'
import PacketFeed  from './components/PacketFeed'
import ArchView    from './components/ArchView'
import ThreatPanel from './components/ThreatPanel'

const css = `
.app-shell {
  display: grid;
  grid-template-areas:
    "topbar  topbar  topbar"
    "sidebar main    rpanel";
  grid-template-rows: 48px 1fr;
  grid-template-columns: 210px 1fr 280px;
  height: 100vh;
  overflow: hidden;
  background: var(--bg0);
}
.app-shell.full-main {
  grid-template-columns: 210px 1fr 0px;
}
.app-main {
  grid-area: main;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--bg0);
  position: relative;
}
`

export default function App() {
  const connect     = useStore(s => s.connect)
  const activePanel = useStore(s => s.activePanel)

  useEffect(() => { connect() }, [connect])

  // Threats panel takes full width — hide right panel
  const fullMain = activePanel === 'threats'

  return (
    <>
      <style>{css}</style>
      <div className={`app-shell ${fullMain ? 'full-main' : ''}`}>
        <Topbar />
        <Sidebar />
        <main className="app-main">
          {activePanel === 'map'     && <MapView />}
          {activePanel === 'threats' && <ThreatPanel />}
          {activePanel === 'packets' && <PacketFeed />}
          {activePanel === 'arch'    && <ArchView />}
        </main>
        {!fullMain && <RightPanel />}
      </div>
    </>
  )
}
