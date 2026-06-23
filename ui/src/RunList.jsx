import React, { useEffect, useMemo, useState } from 'react'
import { get, fmt, listProjects, createProject, patchProject, deleteProject, assignRun } from './util.js'
import MapView from './MapView.jsx'

const ALL = '__all__', UNASSIGNED = '__unassigned__'

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

export default function RunList({ onOpen }) {
  const [runs, setRuns] = useState(null)
  const [proj, setProj] = useState({ projects: [], assignments: {} })
  const [sel, setSel] = useState(ALL)
  const [expanded, setExpanded] = useState(() => new Set())
  const [renaming, setRenaming] = useState(null)   // project id being renamed
  const [dragRun, setDragRun] = useState(null)
  const [view, setView] = useState('list')         // 'list' | 'map' (semantic-zoom cross-run map)

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

  const breadcrumb = useMemo(() => {
    if (sel === ALL || sel === UNASSIGNED) return []
    const path = []; let cur = proj.projects.find(p => p.id === sel)
    while (cur) { path.unshift(cur); cur = proj.projects.find(p => p.id === cur.parent_id) }
    return path
  }, [sel, proj.projects])

  const toggle = (id) => setExpanded(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const refresh = () => { loadProjects(); loadRuns() }

  const addProject = async (parent_id) => {
    const name = prompt(parent_id ? 'New sub-project name' : 'New project name')
    if (!name) return
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
          {runs == null && <div className="notice">Loading runs…</div>}
          {runs && !runsOf(sel).length && <div className="notice">No runs here.{sel === ALL && <> Start one with
            <code> python -m looplab.cli run examples/toy_task.json --out runs/demo</code>.</>} Drag a run onto a project, or use its <b>Move</b> menu.</div>}
          {runs && runsOf(sel).map(r => (
            <div className="run-card" key={r.run_id} draggable
                 onDragStart={() => setDragRun(r.run_id)} onDragEnd={() => setDragRun(null)}>
              <span className="pill phase" onClick={() => onOpen(r.run_id)}>{r.phase}</span>
              <div onClick={() => onOpen(r.run_id)} style={{ cursor: 'pointer', flex: 1 }}>
                <div><b>{r.run_id}</b> <span className="muted">· {r.task_id}</span>
                  {r.project_id && projName[r.project_id] && <span className="pill" style={{ marginLeft: 6 }}>📁 {projName[r.project_id]}</span>}</div>
                <div className="goal">{r.goal}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div>best <b>{fmt(r.best_confirmed ?? r.best_metric)}</b></div>
                <div className="muted">{r.nodes} nodes · {r.direction}</div>
              </div>
              <select className="text move-sel" title="move to project" value={r.project_id || UNASSIGNED}
                      onChange={e => moveRun(r.run_id, e.target.value)}>
                <option value={UNASSIGNED}>— unassigned —</option>
                {proj.projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
          ))}
        </div>
      </div>}
    </div>
  )
}
