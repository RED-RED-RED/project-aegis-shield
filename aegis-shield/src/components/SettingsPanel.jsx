// src/components/SettingsPanel.jsx
// GUI for the live-editable server settings (notifications, live-map windows)
// plus a read-only view of the rest. No auth — trusted-LAN deployment.
import { useEffect, useMemo, useState } from 'react'

export default function SettingsPanel() {
  const [snap, setSnap] = useState(null)
  const [dirty, setDirty] = useState({})
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [testMsg, setTestMsg] = useState('')

  async function load() {
    try {
      const r = await fetch('/api/settings')
      setSnap(await r.json())
    } catch { setMsg('Could not load settings') }
  }
  useEffect(() => { load() }, [])

  const groups = useMemo(() => {
    const g = {}
    for (const f of snap?.editable || []) (g[f.group] ||= []).push(f)
    return g
  }, [snap])

  const set = (key, val) => setDirty(d => ({ ...d, [key]: val }))
  const cur = f => (f.key in dirty ? dirty[f.key] : (f.secret ? '' : f.value))

  async function save() {
    if (Object.keys(dirty).length === 0) { setMsg('Nothing to save'); return }
    setSaving(true); setMsg('')
    try {
      const r = await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(dirty),
      })
      const j = await r.json()
      setMsg(`Saved ${j.count} setting${j.count === 1 ? '' : 's'}`)
      setDirty({}); await load()
    } catch { setMsg('Save failed') }
    setSaving(false)
  }

  async function sendTest() {
    setTestMsg('Sending…')
    try {
      const r = await fetch('/api/notify/test', { method: 'POST' })
      const j = await r.json()
      setTestMsg(j.sent ? '✓ Test push sent' : `✗ ${j.detail || 'failed'}`)
    } catch { setTestMsg('✗ request failed') }
  }

  if (!snap) return <div className="st-empty">Loading settings…</div>

  return (
    <div className="st-root">
      <style>{css}</style>

      {Object.entries(groups).map(([group, fields]) => (
        <div key={group} className="st-group">
          <div className="st-group-head">
            {group}
            {group === 'Notifications' && (
              <button className="st-test" onClick={sendTest}>Send test push</button>
            )}
          </div>
          {group === 'Notifications' && testMsg && <div className="st-testmsg">{testMsg}</div>}
          <div className="st-fields">
            {fields.map(f => (
              <label key={f.key} className="st-field">
                <span className="st-label">{f.label}</span>
                {f.type === 'bool' ? (
                  <input type="checkbox" checked={!!cur(f)} onChange={e => set(f.key, e.target.checked)} />
                ) : f.type === 'enum' ? (
                  <select value={cur(f)} onChange={e => set(f.key, e.target.value)}>
                    {f.choices.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                ) : f.secret ? (
                  <input type="password" value={cur(f)} placeholder={f.configured ? '•••••• (set — leave blank to keep)' : 'not set'}
                         onChange={e => set(f.key, e.target.value)} />
                ) : (
                  <input type={f.type === 'int' || f.type === 'float' ? 'number' : 'text'}
                         value={cur(f)} onChange={e => set(f.key, e.target.value)} />
                )}
              </label>
            ))}
          </div>
        </div>
      ))}

      <div className="st-actions">
        <button className="st-save" onClick={save} disabled={saving || Object.keys(dirty).length === 0}>
          {saving ? 'Saving…' : 'Save changes'}
        </button>
        {msg && <span className="st-msg">{msg}</span>}
        <span className="st-hint">applies live — no redeploy</span>
      </div>

      <div className="st-group">
        <div className="st-group-head">Read-only (set in .env, needs redeploy)</div>
        <div className="st-ro">
          {snap.readonly.map(r => (
            <div key={r.key} className="st-ro-row"><span>{r.key}</span><span>{String(r.value)}</span></div>
          ))}
        </div>
      </div>
    </div>
  )
}

const css = `
.st-root { padding:4px 2px 20px; max-width:720px; }
.st-empty { color:var(--muted); padding:12px; }
.st-group { margin-bottom:16px; }
.st-group-head { display:flex; align-items:center; justify-content:space-between;
  font-family:'Barlow Condensed',sans-serif; text-transform:uppercase; font-size:13px;
  letter-spacing:0.05em; color:var(--text); border-bottom:1px solid var(--bg-border);
  padding-bottom:4px; margin-bottom:8px; }
.st-test { background:var(--bg2); border:1px solid var(--bg-border); color:var(--text);
  font-family:var(--mono); font-size:10px; padding:2px 8px; border-radius:3px; cursor:pointer; }
.st-testmsg { font-family:var(--mono); font-size:11px; color:var(--text-secondary); margin-bottom:6px; }
.st-fields { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:8px 16px; }
.st-field { display:flex; align-items:center; justify-content:space-between; gap:10px;
  font-family:var(--mono); font-size:11px; color:var(--text-secondary); }
.st-label { flex-shrink:0; }
.st-field input[type=text], .st-field input[type=password], .st-field input[type=number], .st-field select {
  flex:1; min-width:0; max-width:220px; background:var(--bg1); border:1px solid var(--bg-border);
  color:var(--text); font-family:var(--mono); font-size:11px; padding:3px 6px; border-radius:3px; }
.st-field input[type=checkbox] { accent-color:var(--steel-blue); width:15px; height:15px; }
.st-actions { display:flex; align-items:center; gap:12px; margin:12px 0 20px; }
.st-save { background:var(--steel-blue); border:none; color:#fff; font-family:var(--mono);
  font-size:12px; padding:5px 16px; border-radius:3px; cursor:pointer; }
.st-save:disabled { opacity:0.5; cursor:default; }
.st-msg { font-family:var(--mono); font-size:11px; color:var(--threat-low); }
.st-hint { font-family:var(--mono); font-size:10px; color:var(--muted); margin-left:auto; }
.st-ro { display:grid; grid-template-columns:1fr 1fr; gap:2px 16px; }
.st-ro-row { display:flex; justify-content:space-between; font-family:var(--mono); font-size:10px;
  color:var(--muted); border-bottom:1px dotted var(--bg-border); padding:2px 0; }
.st-ro-row span:last-child { color:var(--text-secondary); }
`
