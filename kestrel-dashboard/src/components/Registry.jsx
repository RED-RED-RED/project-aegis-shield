// src/components/Registry.jsx
// Drone registry: every drone ever seen, with make/model, an editable category
// (which drives alert suppress/escalate), and a timestamped notes log.
import { useEffect, useMemo, useState } from 'react'
import { colors } from '../theme/colors'

// Category key -> badge color. Keys come from the server's category ladder.
const CAT_COLOR = {
  authorized:     colors.steelBlue,
  unknown:        colors.textMuted,
  known_benign:   colors.threatLow,
  nuisance:       colors.olive,
  prior_incident: colors.threatMed,
  watchlist:      colors.threatMed,
  hostile:        colors.threatHigh,
}
const catColor = k => CAT_COLOR[k] || colors.textMuted

function fmtTime(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

export default function Registry() {
  const [cats, setCats] = useState([])
  const [drones, setDrones] = useState([])
  const [q, setQ] = useState('')
  const [catFilter, setCatFilter] = useState('')
  const [selId, setSelId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [noteDraft, setNoteDraft] = useState('')
  const [err, setErr] = useState(null)

  const catLabel = useMemo(() => Object.fromEntries(cats.map(c => [c.key, c.label])), [cats])

  async function loadList() {
    try {
      const j = await (await fetch('/api/registry')).json()
      setDrones(j.drones || []); setErr(null)
    } catch (e) { setErr(String(e.message || e)) }
  }
  async function loadDetail(id) {
    try { setDetail(await (await fetch(`/api/registry/${encodeURIComponent(id)}`)).json()) }
    catch { setDetail(null) }
  }
  useEffect(() => {
    fetch('/api/registry/categories').then(r => r.json()).then(j => setCats(j.categories || [])).catch(() => {})
    loadList()
  }, [])
  useEffect(() => { if (selId) loadDetail(selId) }, [selId])

  async function setCategory(id, category) {
    try {
      await fetch(`/api/registry/${encodeURIComponent(id)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category }),
      })
      setDrones(ds => ds.map(d => d.drone_id === id ? { ...d, category } : d))
      setDetail(d => d && d.drone_id === id ? { ...d, category } : d)
    } catch { /* best effort */ }
  }

  async function addNote(id) {
    const body = noteDraft.trim()
    if (!body) return
    try {
      const j = await (await fetch(`/api/registry/${encodeURIComponent(id)}/notes`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body }),
      })).json()
      if (j.note) {
        setDetail(d => d ? { ...d, notes: [j.note, ...(d.notes || [])] } : d)
        setDrones(ds => ds.map(d => d.drone_id === id ? { ...d, note_count: (d.note_count || 0) + 1 } : d))
        setNoteDraft('')
      }
    } catch { /* best effort */ }
  }

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return drones.filter(d => {
      if (catFilter && d.category !== catFilter) return false
      if (!needle) return true
      return [d.make, d.model, d.drone_id].filter(Boolean).join(' ').toLowerCase().includes(needle)
    })
  }, [drones, q, catFilter])

  const annotatedCount = drones.filter(d => d.category && d.category !== 'unknown').length

  return (
    <div className="rg-root">
      <style>{css}</style>

      <div className="rg-side">
        <div className="rg-filters">
          <input className="rg-search" type="text" value={q} placeholder="Search make / model / serial…"
                 onChange={e => setQ(e.target.value)} />
          <div className="rg-frow">
            <button className={`rg-chip ${catFilter === '' ? 'on' : ''}`} onClick={() => setCatFilter('')}>All</button>
            {cats.filter(c => c.key !== 'unknown').map(c => (
              <button key={c.key} className={`rg-chip ${catFilter === c.key ? 'on' : ''}`}
                      style={catFilter === c.key ? { borderColor: catColor(c.key), color: catColor(c.key) } : {}}
                      onClick={() => setCatFilter(catFilter === c.key ? '' : c.key)}>{c.label}</button>
            ))}
          </div>
        </div>

        <div className="rg-listhead">Drones ({shown.length}{drones.length !== shown.length ? ` of ${drones.length}` : ''}) · {annotatedCount} tagged</div>
        {err && <div className="rg-err">Couldn’t load registry: {err}</div>}
        <div className="rg-list">
          {shown.length === 0 && <div className="rg-empty">{drones.length === 0 ? 'No drones seen yet.' : 'No drones match.'}</div>}
          {shown.map(d => (
            <div key={d.drone_id} className={`rg-item ${selId === d.drone_id ? 'sel' : ''}`} onClick={() => setSelId(d.drone_id)}>
              <div className="rg-item-top">
                <span className="rg-item-id">{d.drone_id}</span>
                <span className="rg-badge" style={{ color: catColor(d.category), borderColor: catColor(d.category) }}>
                  {catLabel[d.category] || d.category}
                </span>
              </div>
              <div className="rg-item-sub">
                {[d.make, d.model].filter(Boolean).join(' ') || 'Unidentified'}
                {' · '}{d.detection_count || 0} det
                {d.note_count > 0 && <span className="rg-note-dot"> · ✎ {d.note_count}</span>}
                {' · '}seen {fmtTime(d.last_seen).split(',')[0]}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="rg-main">
        {!detail && <div className="rg-empty-main">Select a drone to view and annotate it.</div>}
        {detail && (
          <div className="rg-detail">
            <div className="rg-d-head">
              <div>
                <div className="rg-d-id">{detail.drone_id}</div>
                <div className="rg-d-make">{[detail.make, detail.model].filter(Boolean).join(' ') || 'Unidentified'}
                  {detail.id_source === 'unverified' && <span className="rg-warn"> · unverified serial</span>}</div>
              </div>
              <span className="rg-badge lg" style={{ color: catColor(detail.category), borderColor: catColor(detail.category) }}>
                {catLabel[detail.category] || detail.category}
              </span>
            </div>

            <div className="rg-d-grid">
              <div><span className="rg-k">First seen</span><span className="rg-v">{fmtTime(detail.first_seen)}</span></div>
              <div><span className="rg-k">Last seen</span><span className="rg-v">{fmtTime(detail.last_seen)}</span></div>
              <div><span className="rg-k">Detections</span><span className="rg-v">{detail.detection_count || 0}</span></div>
              <div><span className="rg-k">Valid RID</span><span className="rg-v">{detail.has_valid_rid === false ? '⚠ No' : 'Yes'}</span></div>
              <div><span className="rg-k">Operator ID</span><span className="rg-v">{detail.operator_id || '— none —'}</span></div>
              <div><span className="rg-k">Seen by</span><span className="rg-v">{(detail.detecting_nodes || []).join(', ') || '—'}</span></div>
            </div>

            <div className="rg-section">
              <div className="rg-section-h">Category</div>
              <div className="rg-catbtns">
                {cats.map(c => (
                  <button key={c.key}
                          className={`rg-catbtn ${detail.category === c.key ? 'on' : ''}`}
                          style={detail.category === c.key ? { background: catColor(c.key), borderColor: catColor(c.key), color: '#0b0d0a' } : { borderColor: catColor(c.key), color: catColor(c.key) }}
                          onClick={() => setCategory(detail.drone_id, c.key)}
                          title={c.behavior === 'suppress' ? 'Suppresses alerts' : c.behavior === 'escalate' ? 'Escalates alerts (emergency push)' : ''}>
                    {c.label}{c.behavior === 'escalate' ? ' ⚠' : c.behavior === 'suppress' ? ' ⨯' : ''}
                  </button>
                ))}
              </div>
              <div className="rg-hint">Watchlist and Hostile escalate to an emergency push; Authorized suppresses routine pushes.</div>
            </div>

            <div className="rg-section">
              <div className="rg-section-h">Notes</div>
              <div className="rg-noteadd">
                <input className="rg-noteinput" type="text" value={noteDraft} placeholder="Add a note…"
                       onChange={e => setNoteDraft(e.target.value)}
                       onKeyDown={e => { if (e.key === 'Enter') addNote(detail.drone_id) }} />
                <button className="rg-notebtn" onClick={() => addNote(detail.drone_id)}>Add</button>
              </div>
              <div className="rg-notes">
                {(detail.notes || []).length === 0 && <div className="rg-empty">No notes yet.</div>}
                {(detail.notes || []).map(n => (
                  <div key={n.id} className="rg-note">
                    <div className="rg-note-t">{fmtTime(n.created_at)}</div>
                    <div className="rg-note-b">{n.body}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const css = `
.rg-root { display:grid; grid-template-columns:360px 1fr; height:100%; overflow:hidden; background:var(--bg0); }
.rg-side { border-right:1px solid var(--bg-border); display:flex; flex-direction:column; overflow:hidden; }
.rg-filters { padding:8px; border-bottom:1px solid var(--bg-border); display:flex; flex-direction:column; gap:6px; }
.rg-search { background:var(--bg1); border:1px solid var(--bg-border); color:var(--text); font-family:var(--mono); font-size:11px; padding:4px 6px; border-radius:3px; }
.rg-search::placeholder { color:var(--muted); }
.rg-frow { display:flex; flex-wrap:wrap; gap:4px; }
.rg-chip { background:var(--bg1); border:1px solid var(--bg-border); color:var(--text-secondary); font-family:var(--mono); font-size:10px; padding:2px 7px; border-radius:10px; cursor:pointer; }
.rg-chip.on { color:var(--text); border-color:var(--steel-blue); }
.rg-listhead { padding:7px 10px; font-family:var(--cond); text-transform:uppercase; font-size:12px; letter-spacing:0.05em; color:var(--text-secondary); border-bottom:1px solid var(--bg-border); }
.rg-list { overflow-y:auto; flex:1; }
.rg-item { padding:7px 10px; border-bottom:1px solid var(--bg-border); cursor:pointer; }
.rg-item:hover { background:var(--bg1); }
.rg-item.sel { background:var(--bg-elevated); border-left:2px solid var(--steel-blue); }
.rg-item-top { display:flex; align-items:center; justify-content:space-between; gap:6px; }
.rg-item-id { font-family:var(--mono); font-size:11px; color:var(--text); word-break:break-all; }
.rg-item-sub { font-family:var(--mono); font-size:10px; color:var(--text-secondary); margin-top:2px; }
.rg-note-dot { color:var(--olive); }
.rg-badge { font-family:var(--mono); font-size:9px; border:1px solid; border-radius:2px; padding:0 5px; white-space:nowrap; flex-shrink:0; }
.rg-badge.lg { font-size:11px; padding:2px 8px; }
.rg-err { color:var(--threat-high); font-family:var(--mono); font-size:11px; padding:8px 10px; }
.rg-empty, .rg-empty-main { color:var(--muted); font-family:var(--mono); font-size:11px; padding:12px; }
.rg-main { overflow-y:auto; }
.rg-detail { padding:14px 18px; max-width:760px; }
.rg-d-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; border-bottom:1px solid var(--bg-border); padding-bottom:10px; }
.rg-d-id { font-family:var(--mono); font-size:15px; color:var(--text); word-break:break-all; }
.rg-d-make { font-family:var(--mono); font-size:12px; color:var(--olive); margin-top:2px; }
.rg-warn { color:var(--threat-high); }
.rg-d-grid { display:grid; grid-template-columns:1fr 1fr; gap:4px 20px; margin:12px 0; }
.rg-d-grid > div { display:flex; justify-content:space-between; font-family:var(--mono); font-size:11px; border-bottom:1px dotted var(--bg-border); padding:3px 0; }
.rg-k { color:var(--muted); } .rg-v { color:var(--text); }
.rg-section { margin-top:16px; }
.rg-section-h { font-family:var(--cond); text-transform:uppercase; font-size:13px; letter-spacing:0.05em; color:var(--text); border-bottom:1px solid var(--bg-border); padding-bottom:4px; margin-bottom:8px; }
.rg-catbtns { display:flex; flex-wrap:wrap; gap:6px; }
.rg-catbtn { background:var(--bg1); border:1px solid; font-family:var(--mono); font-size:11px; padding:3px 10px; border-radius:3px; cursor:pointer; }
.rg-catbtn.on { font-weight:bold; }
.rg-hint { font-family:var(--mono); font-size:10px; color:var(--muted); margin-top:6px; }
.rg-noteadd { display:flex; gap:6px; margin-bottom:8px; }
.rg-noteinput { flex:1; background:var(--bg1); border:1px solid var(--bg-border); color:var(--text); font-family:var(--mono); font-size:11px; padding:4px 6px; border-radius:3px; }
.rg-noteinput::placeholder { color:var(--muted); }
.rg-notebtn { background:var(--steel-blue); border:none; color:#fff; font-family:var(--mono); font-size:11px; padding:4px 12px; border-radius:3px; cursor:pointer; }
.rg-notes { display:flex; flex-direction:column; gap:6px; }
.rg-note { border-left:2px solid var(--bg-border); padding:2px 0 2px 8px; }
.rg-note-t { font-family:var(--mono); font-size:9px; color:var(--muted); }
.rg-note-b { font-family:var(--mono); font-size:11px; color:var(--text); white-space:pre-wrap; word-break:break-word; }
`
