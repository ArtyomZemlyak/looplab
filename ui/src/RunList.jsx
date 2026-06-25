import React, { useEffect, useMemo, useState } from 'react'
import { get, fmt, fmtDate, fmtAgo, listProjects, createProject, patchProject, deleteProject, assignRun, renameRun, deleteRun } from './util.js'
import MapView from './MapView.jsx'
import StartRun from './StartRun.jsx'

const ALL = '__all__', UNASSIGNED = '__unassigned__'

// Small centered popup (replaces window.prompt for project create / run rename).
function Modal({ title, onClose, children }) {
  return <div className="overlay" onMouseDown={onClose}>
    <div className="modal" onMouseDown={e => e.stopPropagation()}>
      <div className="modal-h"><b>{title}</b><span style={{ flex: 1 }} />
        <button className="btn sm ghost" onClick={onClose} title="close">✕</button></div>
      <div className="modal-b">{children}</div>
    </div>
  </div>
}

function PromptModal({ title, label, placeholder, initial = '', confirm = 'Create', allowEmpty = false, onSubmit, onClose }) {
  const [v, setV] = useState(initial)
  const ok = allowEmpty || !!v.trim()
  const go = () => { if (ok) onSubmit(v.trim()) }
  return <Modal title={title} onClose={onClose}>
    {label && <div className="muted" style={{ marginBottom: 8 }}>{label}</div>}
    <input className="text" autoFocus placeholder={placeholder} value={v} onChange={e => setV(e.target.value)}
           onKeyDown={e => { if (e.key === 'Enter') go(); if (e.key === 'Escape') onClose() }} />
    <div className="modal-actions">
      <button className="btn sm ghost" onClick={onClose}>Cancel</button>
      <button className="btn sm primary" disabled={!ok} onClick={go}>{confirm}</button>
    </div>
  </Modal>
}

// Per-run "⋮" dropdown: open / rename / move (to any project or unassigned) / delete.
function RunMenu({ r, projects, onOpen, onMove, onRename, onDelete, onClose }) {
  return <>
    <div className="menu-backdrop" onClick={onClose} onDragStart={onClose} />
    <div className="run-menu" onClick={e => e.stopPropagation()}>
      <button className="mi" onClick={() => { onClose(); onOpen(r.run_id) }}>↗ Open</button>
      <button className="mi" onClick={() => { onClose(); onRename(r) }}>✎ Rename</button>
      <div className="mi-sep" />
      <div className="mi-label">Move to project</div>
      <div className="mi-scroll">
        <button className={'mi' + (!r.project_id ? ' on' : '')} onClick={() => { onClose(); onMove(r.run_id, UNASSIGNED) }}>○ — unassigned —</button>
        {projects.map(p => <button key={p.id} className={'mi' + (r.project_id === p.id ? ' on' : '')}
          onClick={() => { onClose(); onMove(r.run_id, p.id) }}>📁 {p.name}</button>)}
        {!projects.length && <div className="mi-empty">no projects yet</div>}
      </div>
      <div className="mi-sep" />
      <button className="mi danger" onClick={() => onDelete(r)}>✕ Delete run…</button>
    </div>
  </>
}

// children-by-parent index + the set of a project's own id plus all descendants (for run counts
// and "show runs in this project and everything under it" selection — nesting implies containment).
function indexProjects(projects) {
  const byParent = {}
  projects.forEach(p => { (byParent[p.parent_id || null] ||= []).push(p) })
  Object.values(byParent).forEach(a => a.sort((x, y) => x.name.localeCompare(y.name)))
  const subtree = (id) => {
    const out = new Set([id]); const stack = [id]
    while (stack.length) { const c = stack.pop(); (byParent[c] || []).forEach(k => { out.add(k.id); stack.push(k.id) }) }
    return out
  }
  return { byParent, subtree }
}

export default function RunList({ onOpen, onSettings }) {
  const [runs, setRuns] = useState(null)
  const [proj, setProj] = useState({ projects: [], assignments: {} })
  const [sel, setSel] = useState(ALL)
  const [expanded, setExpanded] = useState(() => new Set())
  const [renaming, setRenaming] = useState(null)   // project id being renamed (inline)
  const [dragRun, setDragRun] = useState(null)
  const [view, setView] = useState('list')         // 'list' | 'map' (semantic-zoom cross-run map)
  const [projModal, setProjModal] = useState(null) // {parent_id} → show create-project popup
  const [runMenu, setRunMenu] = useState(null)     // run_id whose ⋮ menu is open
  const [runRename, setRunRename] = useState(null) // run object being renamed (popup)
  const [starting, setStarting] = useState(false)  // show the New-run launch dialog
  // Sort + filter of the run list (client-side over the loaded summaries).
  const [sortKey, setSortKey] = useState('time')   // time | name | metric | task | nodes | phase
  const [sortDir, setSortDir] = useState('desc')   // asc | desc
  const [query, setQuery] = useState('')           // free-text over label/id/task/goal
  const [taskFilter, setTaskFilter] = useState(ALL)
  const [statusFilter, setStatusFilter] = useState('all')   // all | running | finished

  const loadRuns = () => get('/api/runs').then(setRuns).catch(() => setRuns([]))
  const loadProjects = () => listProjects().then(setProj).catch(() => {})
  useEffect(() => { loadRuns(); loadProjects(); const t = setInterval(loadRuns, 2500); return () => clearInterval(t) }, [])

  const { byParent, subtree } = useMemo(() => indexProjects(proj.projects), [proj.projects])
  const projName = useMemo(() => Object.fromEntries(proj.projects.map(p => [p.id, p.name])), [proj.projects])

  const runsOf = (id) => {
    const rs = runs || []
    if (id === ALL) return rs
    if (id === UNASSIGNED) return rs.filter(r => !proj.assignments[r.run_id])
    const set = subtree(id)
    return rs.filter(r => set.has(proj.assignments[r.run_id]))
  }
  const count = (id) => runsOf(id).length

  // Distinct task ids across all loaded runs — populates the task filter dropdown.
  const tasks = useMemo(
    () => Array.from(new Set((runs || []).map(r => r.task_id).filter(Boolean))).sort(),
    [runs])

  // The runs to render: project selection -> filters -> sort. Pure derivation of loaded summaries.
  const visible = useMemo(() => {
    let rs = runsOf(sel)
    const q = query.trim().toLowerCase()
    if (q) rs = rs.filter(r => [r.label, r.run_id, r.task_id, r.goal]
      .some(s => (s || '').toLowerCase().includes(q)))
    if (taskFilter !== ALL) rs = rs.filter(r => r.task_id === taskFilter)
    if (statusFilter === 'running') rs = rs.filter(r => !r.finished)
    else if (statusFilter === 'finished') rs = rs.filter(r => r.finished)

    const name = r => (r.label || r.run_id || '').toLowerCase()
    const metric = r => r.best_confirmed ?? r.best_metric
    const mul = sortDir === 'asc' ? 1 : -1
    const cmps = {
      time: (a, b) => mul * ((a.mtime || 0) - (b.mtime || 0)),
      name: (a, b) => mul * name(a).localeCompare(name(b)),
      task: (a, b) => mul * (a.task_id || '').localeCompare(b.task_id || '') || name(a).localeCompare(name(b)),
      nodes: (a, b) => mul * ((a.nodes || 0) - (b.nodes || 0)),
      phase: (a, b) => mul * (a.phase || '').localeCompare(b.phase || ''),
      metric: (a, b) => {                       // best metric; missing values sort last in BOTH dirs
        const av = metric(a), bv = metric(b)
        if (av == null || bv == null) return (av == null ? 1 : 0) - (bv == null ? 1 : 0)
        return mul * (av - bv)
      },
    }
    return [...rs].sort(cmps[sortKey] || (() => 0))
  }, [runs, sel, proj.assignments, query, taskFilter, statusFilter, sortKey, sortDir])

  const breadcrumb = useMemo(() => {
    if (sel === ALL || sel === UNASSIGNED) return []
    const path = []; let cur = proj.projects.find(p => p.id === sel)
    while (cur) { path.unshift(cur); cur = proj.projects.find(p => p.id === cur.parent_id) }
    return path
  }, [sel, proj.projects])

  const toggle = (id) => setExpanded(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const refresh = () => { loadProjects(); loadRuns() }

  const addProject = (parent_id) => setProjModal({ parent_id })
  const submitProject = async (name) => {
    const parent_id = projModal?.parent_id; setProjModal(null)
    const p = await createProject(name, parent_id)
    if (parent_id) setExpanded(s => new Set(s).add(parent_id))
    await loadProjects(); setSel(p.id)
  }
  const commitRename = async (id, name) => { setRenaming(null); if (name?.trim()) { await patchProject(id, { name: name.trim() }); loadProjects() } }
  const removeProject = async (id) => {
    if (!confirm(`Delete project "${projName[id]}"? Sub-projects and runs move up to its parent.`)) return
    await deleteProject(id); if (sel === id) setSel(ALL); refresh()
  }
  const moveRun = async (runId, project_id) => { await assignRun(runId, project_id === UNASSIGNED ? null : project_id); refresh() }
  const onDrop = async (project_id) => { if (dragRun) { await moveRun(dragRun, project_id); setDragRun(null) } }
  const submitRunRename = async (label) => { const id = runRename.run_id; setRunRename(null); await renameRun(id, label); loadRuns() }
  const removeRun = async (r) => {
    setRunMenu(null)
    if (!confirm(`Delete run "${r.label || r.run_id}" permanently? This removes its files on disk and cannot be undone.`)) return
    try { await deleteRun(r.run_id); refresh() }
    catch (e) { alert(/409/.test(e.message) ? 'This run is still live — pause or stop it before deleting.' : 'Delete failed: ' + e.message) }
  }

  const TreeNode = ({ p, depth }) => {
    const kids = byParent[p.id] || []
    const open = expanded.has(p.id)
    return <div className="ptree-node">
      <div className={'ptree-row' + (sel === p.id ? ' sel' : '')} style={{ paddingLeft: 6 + depth * 14 }}
           onClick={() => setSel(p.id)}
           onDragOver={e => { e.preventDefault() }} onDrop={() => onDrop(p.id)}>
        <span className="ptw" onClick={e => { e.stopPropagation(); toggle(p.id) }}>{kids.length ? (open ? '▾' : '▸') : '·'}</span>
        {renaming === p.id
          ? <input className="text ptree-rename" autoFocus defaultValue={p.name}
                   onClick={e => e.stopPropagation()}
                   onBlur={e => commitRename(p.id, e.target.value)}
                   onKeyDown={e => { if (e.key === 'Enter') commitRename(p.id, e.target.value); if (e.key === 'Escape') setRenaming(null) }} />
          : <span className="pname">📁 {p.name}</span>}
        <span className="pcount">{count(p.id)}</span>
        <span className="pacts" onClick={e => e.stopPropagation()}>
          <button className="ic" title="add sub-project" onClick={() => addProject(p.id)}>＋</button>
          <button className="ic" title="rename" onClick={() => setRenaming(p.id)}>✎</button>
          <button className="ic" title="delete" onClick={() => removeProject(p.id)}>✕</button>
        </span>
      </div>
      {open && kids.map(k => <TreeNode key={k.id} p={k} depth={depth + 1} />)}
    </div>
  }

  return (
    <div className="app">
      <div className="topbar"><span className="brand"><span className="dot">◉</span> LoopLab</span>
        <span className="muted">autonomous R&D — live runs</span>
        <span className="spacer" style={{ flex: 1 }} />
        <div className="seg">
          <button className={view === 'list' ? 'on' : ''} onClick={() => setView('list')}>☰ List</button>
          <button className={view === 'map' ? 'on' : ''} onClick={() => setView('map')}>🗺 Map</button>
        </div>
        <button className="btn sm primary" onClick={() => setStarting(true)}>▶ New run</button>
        <button className="btn sm ghost" title="settings" onClick={() => onSettings && onSettings()}>⚙ Settings</button>
      </div>
      {view === 'map' && <div style={{ flex: 1, minHeight: 0 }}><MapView onOpen={onOpen} /></div>}
      {view === 'list' && <div className="runlayout">
        <aside className="psidebar">
          <div className="psidebar-h">
            <b>Projects</b>
            <button className="btn sm" onClick={() => addProject(null)}>＋ New</button>
          </div>
          <div className={'ptree-row pseudo' + (sel === ALL ? ' sel' : '')} onClick={() => setSel(ALL)}
               onDragOver={e => e.preventDefault()} onDrop={() => onDrop(UNASSIGNED)}>
            <span className="ptw">▦</span><span className="pname">All runs</span><span className="pcount">{count(ALL)}</span>
          </div>
          <div className={'ptree-row pseudo' + (sel === UNASSIGNED ? ' sel' : '')} onClick={() => setSel(UNASSIGNED)}
               onDragOver={e => e.preventDefault()} onDrop={() => onDrop(UNASSIGNED)}>
            <span className="ptw">○</span><span className="pname">Unassigned</span><span className="pcount">{count(UNASSIGNED)}</span>
          </div>
          <div className="ptree">
            {(byParent[null] || []).map(p => <TreeNode key={p.id} p={p} depth={0} />)}
            {!proj.projects.length && <div className="muted" style={{ padding: 10, fontSize: 12 }}>No projects yet. Create one to organize runs.</div>}
          </div>
        </aside>

        <div className="runlist">
          <div className="crumbs">
            <span className="crumb" onClick={() => setSel(ALL)}>All runs</span>
            {breadcrumb.map(p => <React.Fragment key={p.id}><span className="sep">/</span>
              <span className="crumb" onClick={() => setSel(p.id)}>{p.name}</span></React.Fragment>)}
            {sel === UNASSIGNED && <><span className="sep">/</span><span className="crumb">Unassigned</span></>}
          </div>
          {runs && !!runsOf(sel).length && <div className="runbar">
            <input className="text runbar-q" placeholder="🔎 filter runs…" value={query}
                   onChange={e => setQuery(e.target.value)} />
            <select className="sel" value={statusFilter} onChange={e => setStatusFilter(e.target.value)} title="status">
              <option value="all">all status</option>
              <option value="running">running</option>
              <option value="finished">finished</option>
            </select>
            <select className="sel" value={taskFilter} onChange={e => setTaskFilter(e.target.value)} title="task">
              <option value={ALL}>all tasks</option>
              {tasks.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
            <span style={{ flex: 1 }} />
            <span className="muted runbar-count">{visible.length}/{runsOf(sel).length}</span>
            <select className="sel" value={sortKey} onChange={e => setSortKey(e.target.value)} title="sort by">
              <option value="time">time</option>
              <option value="name">name</option>
              <option value="metric">best metric</option>
              <option value="task">task</option>
              <option value="nodes">nodes</option>
              <option value="phase">phase</option>
            </select>
            <button className="btn sm ghost" title={sortDir === 'asc' ? 'ascending' : 'descending'}
                    onClick={() => setSortDir(d => d === 'asc' ? 'desc' : 'asc')}>{sortDir === 'asc' ? '↑' : '↓'}</button>
          </div>}
          {runs == null && <div className="notice">Loading runs…</div>}
          {runs && !runsOf(sel).length && <div className="notice">No runs here.{sel === ALL && <> Start one with
            <code> python -m looplab.cli run examples/toy_task.json --out runs/demo</code>.</>} Drag a run onto a project, or use its <b>Move</b> menu.</div>}
          {runs && !!runsOf(sel).length && !visible.length && <div className="notice">No runs match the filter.</div>}
          {runs && visible.map(r => (
            <div className="run-card" key={r.run_id} draggable
                 onDragStart={() => setDragRun(r.run_id)} onDragEnd={() => setDragRun(null)}>
              <span className="pill phase" onClick={() => onOpen(r.run_id)}>{r.phase}</span>
              <div onClick={() => onOpen(r.run_id)} style={{ cursor: 'pointer', flex: 1 }}>
                <div><b>{r.label || r.run_id}</b> <span className="muted">· {r.label ? r.run_id + ' · ' : ''}{r.task_id}</span>
                  {r.project_id && projName[r.project_id] && <span className="pill" style={{ marginLeft: 6 }}>📁 {projName[r.project_id]}</span>}</div>
                <div className="goal">{r.goal}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div>best <b>{fmt(r.best_confirmed ?? r.best_metric)}</b></div>
                <div className="muted">{r.nodes} nodes · {r.direction}</div>
                {r.mtime && <div className="muted run-when"
                  title={`started ${fmtDate(r.created)} · updated ${fmtDate(r.mtime)}`}>
                  {fmtAgo(r.mtime)}</div>}
              </div>
              <div className="run-actions">
                <button className="ic dots" title="run actions"
                        onClick={e => { e.stopPropagation(); setRunMenu(m => m === r.run_id ? null : r.run_id) }}>⋮</button>
                {runMenu === r.run_id && <RunMenu r={r} projects={proj.projects}
                  onOpen={onOpen} onMove={moveRun} onRename={setRunRename} onDelete={removeRun}
                  onClose={() => setRunMenu(null)} />}
              </div>
            </div>
          ))}
        </div>
      </div>}

      {projModal && <PromptModal
        title={projModal.parent_id ? 'New sub-project' : 'New project'}
        label={projModal.parent_id ? `Inside “${projName[projModal.parent_id]}”` : 'Group runs into a project folder.'}
        placeholder="e.g. baseline sweep" confirm="Create"
        onSubmit={submitProject} onClose={() => setProjModal(null)} />}

      {runRename && <PromptModal
        title="Rename run" label={`Display name for ${runRename.run_id} (clear it to fall back to the id).`}
        placeholder={runRename.run_id} initial={runRename.label || ''} confirm="Save" allowEmpty
        onSubmit={submitRunRename} onClose={() => setRunRename(null)} />}

      {starting && <StartRun onClose={() => setStarting(false)}
        onStarted={(id) => { setStarting(false); loadRuns(); onOpen(id) }} />}
    </div>
  )
}
