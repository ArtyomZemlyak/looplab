import React, { lazy, useEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, fmtDate, fmtAgo, listProjects, createProject, patchProject, deleteProject, assignRun, renameRun, deleteRun,
  listSupertasks, createSupertask, renameSupertask, deleteSupertask, assignSupertask } from './util.js'
import { useMediaQuery, usePoll } from './hooks.js'
import LazyBoundary from './LazyBoundary.jsx'
import ThemeSwitcher from './ThemeSwitcher.jsx'
import EnergyToggle from './EnergyToggle.jsx'
import { OpIcon } from './icons.jsx'
import {
  ALL_RUNS as ALL, UNASSIGNED_RUNS as UNASSIGNED, filterRuns, indexProjects,
  effectiveRunStatus, metricComparable, scopeRuns, sortRuns,
} from './runIndex.js'
import { defaultCollapsedClusters } from './runMapModel.js'
import { useDialogFocus } from './useDialogFocus.js'
import { followClientRoute, nextRovingIndex } from './accessibility.jsx'

const MapView = lazy(() => import('./MapView.jsx'))
const ScopeReport = lazy(() => import('./ScopeReport.jsx'))

// Module-scope so its identity is stable across re-renders (the runs list polls every 2.5s). Defined
// inside RunList it got a fresh identity each render, so React remounted the whole project subtree and
// the uncontrolled inline-rename <input> lost its text/focus mid-edit. All render state is threaded
// through `ctx`.
function TreeNode({ p, depth, ctx }) {
  const { byParent, expanded, sel, setSel, onDrop, toggle, renaming, finishProjectRename, startProjectRename,
          count, addProject, removeProject } = ctx
  const kids = byParent[p.id] || []
  const open = expanded.has(p.id)
  return <div className="ptree-node">
    <div className={'ptree-row' + (sel === p.id ? ' sel' : '')} style={{ paddingLeft: 6 + depth * 14 }}
         onDragOver={e => { e.preventDefault() }} onDrop={() => onDrop(p.id)}>
      {kids.length
        ? <button type="button" className="ptw" aria-label={`${open ? 'Collapse' : 'Expand'} ${p.name}`}
            aria-expanded={open} onClick={() => toggle(p.id)}>{open ? '▾' : '▸'}</button>
        : <span className="ptw" aria-hidden="true">·</span>}
      {renaming === p.id
        ? <input className="text ptree-rename" autoFocus defaultValue={p.name}
                 aria-label={`Rename project ${p.name}`}
                 onBlur={e => { if (!e.currentTarget.dataset.finished) finishProjectRename(p.id, e.target.value, false) }}
                 onKeyDown={e => {
                   if (e.key === 'Enter') {
                     e.preventDefault(); e.currentTarget.dataset.finished = 'true'
                     finishProjectRename(p.id, e.currentTarget.value, true)
                   }
                   if (e.key === 'Escape') {
                     // Cancel: a blank name skips the PATCH (the guard in finishProjectRename), so
                     // Escape reverts to the current name without a redundant server write + reload.
                     e.preventDefault(); e.currentTarget.dataset.finished = 'true'
                     finishProjectRename(p.id, '', true)
                   }
                 }} />
        : <button type="button" className="pname project-choice" aria-pressed={sel === p.id}
            onClick={() => setSel(p.id)}><OpIcon name="folder" className="t-ic" /> {p.name}</button>}
      <span className="pcount">{count(p.id)}</span>
      <span className="pacts">
        <button className="ic" aria-label={`Add sub-project inside ${p.name}`}
          onClick={event => addProject(p.id, event.currentTarget)}>＋</button>
        <button className="ic" aria-label={`Rename project ${p.name}`} onClick={event => startProjectRename(p.id, event.currentTarget)}><OpIcon name="pencil" size={12} /></button>
        <button className="ic" aria-label={`Delete project ${p.name}`} onClick={() => removeProject(p.id)}>✕</button>
      </span>
    </div>
    {open && <div className="ptree-children">{kids.map(k => <TreeNode key={k.id} p={k} depth={depth + 1} ctx={ctx} />)}</div>}
  </div>
}

// Small centered popup (replaces window.prompt for project create / run rename).
function Modal({ title, onClose, children }) {
  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, onClose)
  return <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
    <div ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}>
      <div className="modal-h"><b>{title}</b><span style={{ flex: 1 }} />
        <button className="btn sm ghost" onClick={onClose} aria-label={`Close ${title}`}>✕</button></div>
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
    <input className="text" autoFocus aria-label={label || title} placeholder={placeholder} value={v} onChange={e => setV(e.target.value)}
           onKeyDown={e => { if (e.key === 'Enter') go(); if (e.key === 'Escape') onClose() }} />
    <div className="modal-actions">
      <button className="btn sm ghost" onClick={onClose}>Cancel</button>
      <button className="btn sm primary" disabled={!ok} onClick={go}>{confirm}</button>
    </div>
  </Modal>
}

// Per-run "⋮" dropdown: open / rename / move (project) / assign (super-task) / delete.
function RunMenu({ r, projects, supertasks, onOpen, onMove, onSetSuper, onManageSupers, onRename, onDelete, onClose }) {
  const menuRef = useRef(null)
  useEffect(() => { menuRef.current?.querySelector('[role="menuitem"]')?.focus() }, [])
  const close = (restore = false) => onClose(restore)
  const onKeyDown = event => {
    const items = [...(menuRef.current?.querySelectorAll('[role="menuitem"]') || [])]
    if (event.key === 'Escape') { event.preventDefault(); close(true); return }
    const current = Math.max(0, items.indexOf(document.activeElement))
    const next = nextRovingIndex(event.key, current, items.length)
    if (next == null) return
    event.preventDefault(); items[next]?.focus()
  }
  return <>
    <div className="menu-backdrop" onClick={() => close(true)} onDragStart={() => close(true)} />
    <div ref={menuRef} className="run-menu" role="menu" aria-label={`Actions for ${r.label || r.run_id}`}
      onClick={e => e.stopPropagation()} onKeyDown={onKeyDown}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(false) }}>
      <button type="button" role="menuitem" tabIndex={-1} className="mi" onClick={() => { close(false); onOpen(r.run_id) }}>↗ Open</button>
      <button type="button" role="menuitem" tabIndex={-1} className="mi" onClick={() => { close(false); onRename(r) }}><OpIcon name="pencil" size={12} /> Rename</button>
      <div className="mi-sep" />
      <div className="mi-label">Move to project</div>
      <div className="mi-scroll">
        <button type="button" role="menuitem" tabIndex={-1} className={'mi' + (!r.project_id ? ' on' : '')} onClick={() => { close(true); onMove(r.run_id, UNASSIGNED) }}>○ — unassigned —</button>
        {projects.map(p => <button type="button" role="menuitem" tabIndex={-1} key={p.id} className={'mi' + (r.project_id === p.id ? ' on' : '')}
          onClick={() => { close(true); onMove(r.run_id, p.id) }}><OpIcon name="folder" className="t-ic" /> {p.name}</button>)}
        {!projects.length && <div className="mi-empty">no projects yet</div>}
      </div>
      <div className="mi-sep" />
      <div className="mi-label">Super-task</div>
      <div className="mi-scroll">
        <button type="button" role="menuitem" tabIndex={-1} className={'mi' + (!r.supertask_id ? ' on' : '')} onClick={() => { close(true); onSetSuper(r.run_id, UNASSIGNED) }}>○ — none —</button>
        {supertasks.map(s => <button type="button" role="menuitem" tabIndex={-1} key={s.id} className={'mi' + (r.supertask_id === s.id ? ' on' : '')}
          onClick={() => { close(true); onSetSuper(r.run_id, s.id) }}><OpIcon name="target" className="t-ic" /> {s.name}</button>)}
        <button type="button" role="menuitem" tabIndex={-1} className="mi accent" onClick={() => { close(false); onManageSupers() }}>＋ New / manage…</button>
      </div>
      <div className="mi-sep" />
      <button type="button" role="menuitem" tabIndex={-1} className="mi danger" onClick={() => { close(false); onDelete(r) }}>✕ Delete run…</button>
    </div>
  </>
}

// Manage super-tasks in one popup: create, rename (inline), delete. Assignment happens per-run via
// the ⋮ menu / drag; this is just the CRUD over the buckets themselves.
function SuperTaskModal({ supertasks, onCreate, onRename, onDelete, onClose }) {
  const [name, setName] = useState('')
  const newTaskRef = useRef(null)
  const add = () => { const v = name.trim(); if (v) { onCreate(v); setName('') } }
  const remove = async (task, event) => {
    const row = event.currentTarget.closest('.st-row')
    const fallback = row?.nextElementSibling?.querySelector('.st-rename')
      || row?.previousElementSibling?.querySelector('.st-rename') || newTaskRef.current
    const removed = await onDelete(task)
    if (removed) requestAnimationFrame(() => fallback?.isConnected
      ? fallback.focus({ preventScroll: true }) : newTaskRef.current?.focus({ preventScroll: true }))
  }
  return <Modal title="Super-tasks" onClose={onClose}>
    <div className="muted" style={{ marginBottom: 8 }}>A super-task groups runs that attack the same global task (across many runs). Assign runs from a run’s ⋮ menu.</div>
    <div className="st-new">
      <input ref={newTaskRef} className="text" autoFocus aria-label="New super-task name" placeholder="New super-task name (e.g. nomad2018)" value={name}
             onChange={e => setName(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') add() }} />
      <button className="btn sm primary" disabled={!name.trim()} onClick={add}>＋ Create</button>
    </div>
    <div className="st-list">
      {supertasks.map(s => <div key={s.id} className="st-row">
        <span className="st-ic"><OpIcon name="target" className="t-ic" /></span>
        <input className="text st-rename" defaultValue={s.name} aria-label={`Rename super-task ${s.name}`}
               onBlur={e => { const v = e.target.value.trim(); if (v && v !== s.name) onRename(s.id, v) }}
               onKeyDown={e => {
                 if (e.key === 'Enter') {
                   e.preventDefault(); const v = e.currentTarget.value.trim()
                   if (v && v !== s.name) onRename(s.id, v)
                 }
               }} />
        <button className="ic" aria-label={`Delete super-task ${s.name}`} onClick={event => remove(s, event)}>✕</button>
      </div>)}
      {!supertasks.length && <div className="muted" style={{ padding: '8px 2px', fontSize: 12 }}>No super-tasks yet.</div>}
    </div>
  </Modal>
}

export default function RunList({ onOpen, onSettings, onResearchAtlas }) {
  const compactNav = useMediaQuery('(max-width: 900px)')
  const [runs, setRuns] = useState(null)
  const [runsError, setRunsError] = useState(null)
  const [proj, setProj] = useState({ projects: [], assignments: {} })
  const [projectsError, setProjectsError] = useState(null)
  const [sel, setSel] = useState(ALL)
  const [expanded, setExpanded] = useState(() => new Set())
  const [renaming, setRenaming] = useState(null)   // project id being renamed (inline)
  const projectRenameReturnRef = useRef(null)
  const runsMainRef = useRef(null)
  const projectsAllRef = useRef(null)
  const [dragRun, setDragRun] = useState(null)
  const [view, setView] = useState('list')         // 'list' | 'map' (semantic-zoom cross-run map)
  const [mapCollapseOverrides, setMapCollapseOverrides] = useState(() => new Map())
  const [projModal, setProjModal] = useState(null) // {parent_id} → show create-project popup
  const projectModalReturnRef = useRef(null)
  const [runMenu, setRunMenu] = useState(null)     // run_id whose ⋮ menu is open
  const runMenuTriggerRef = useRef(null)
  const runModalReturnFocusRef = useRef(null)
  const [runRename, setRunRename] = useState(null) // run object being renamed (popup)
  // Sort + filter of the run list (client-side over the loaded summaries).
  const [sortKey, setSortKey] = useState('time')   // time | name | metric | task | nodes | phase
  const [sortDir, setSortDir] = useState('desc')   // asc | desc
  const [query, setQuery] = useState('')           // free-text over label/id/task/goal
  const [taskFilter, setTaskFilter] = useState(ALL)
  const [statusFilter, setStatusFilter] = useState('all')   // effective status vocabulary from runIndex
  const [stFilter, setStFilter] = useState(ALL)             // super-task filter (ALL | UNASSIGNED | id)
  const [superdata, setSuperdata] = useState({ supertasks: [], assignments: {} })
  const [stModal, setStModal] = useState(false)             // manage-super-tasks popup open?
  const [showReport, setShowReport] = useState(false)       // cross-run scope-report panel open?
  const [projectsOpen, setProjectsOpen] = useState(false)   // compact-screen Projects drawer
  const projectsToggleRef = useRef(null)
  const projectsCloseRef = useRef(null)
  const projectsDialogRef = useRef(null)
  const closeRunMenu = (restore = false) => {
    setRunMenu(null)
    if (restore) requestAnimationFrame(() => runMenuTriggerRef.current?.isConnected
      && runMenuTriggerRef.current.focus({ preventScroll: true }))
  }
  const restoreRunModalFocus = () => requestAnimationFrame(() => {
    const target = runModalReturnFocusRef.current
    if (target?.isConnected) target.focus({ preventScroll: true })
    runModalReturnFocusRef.current = null
  })
  const openRunRename = run => {
    runModalReturnFocusRef.current = runMenuTriggerRef.current
    setRunRename(run)
  }
  const openSuperTasks = returnFocus => {
    runModalReturnFocusRef.current = returnFocus || runMenuTriggerRef.current
    setStModal(true)
  }
  const closeRunRename = () => { setRunRename(null); restoreRunModalFocus() }
  const closeSuperTasks = () => { setStModal(false); restoreRunModalFocus() }
  useDialogFocus(projectsDialogRef, () => setProjectsOpen(false), compactNav && projectsOpen)
  // Run creation is no longer a modal here — it happens INSIDE the assistant (the bottom command bar's
  // "/new" asks the assistant to propose a run, which renders as an inline launch card in the chat).

  const loadRuns = () => get('/api/runs')
    .then(data => { setRuns(data); setRunsError(null) })
    .catch(e => setRunsError(e?.message || 'Could not load runs.'))
  const loadProjects = () => listProjects()
    .then(data => { setProj(data); setProjectsError(null) })
    .catch(e => setProjectsError(e?.message || 'Could not load projects.'))
  const loadSupers = () => listSupertasks().then(setSuperdata).catch(() => {})
  // Poll the runs list every 2.5s, but skip the request while the tab is hidden (no point refreshing a
  // list nobody's looking at) — and refresh once immediately when it becomes visible again.
  usePoll(loadRuns, 2500, [], { pauseHidden: true })
  useEffect(() => { loadProjects(); loadSupers() }, [])

  const stName = useMemo(() => Object.fromEntries(superdata.supertasks.map(s => [s.id, s.name])), [superdata])
  const assignToSuper = async (runId, sid) => { await assignSupertask(runId, sid === UNASSIGNED ? null : sid); loadRuns() }

  const { byParent, subtree } = useMemo(() => indexProjects(proj.projects), [proj.projects])
  const projName = useMemo(() => Object.fromEntries(proj.projects.map(p => [p.id, p.name])), [proj.projects])

  const runsOf = (id) => {
    return scopeRuns(runs || [], id, proj.projects)
  }
  const count = (id) => runsOf(id).length

  // Distinct task ids across all loaded runs — populates the task filter dropdown.
  const tasks = useMemo(
    () => Array.from(new Set((runs || []).map(r => r.task_id).filter(Boolean))).sort(),
    [runs])

  // List and Map consume this exact same derived result set.  Map no longer performs an independent
  // fetch, so switching representation cannot silently reset scope or show stale assignments.
  const filtered = useMemo(() => filterRuns(runs || [], {
    project: sel, projects: proj.projects, query, task: taskFilter,
    supertask: stFilter, status: statusFilter,
  }), [runs, sel, proj.projects, query, taskFilter, stFilter, statusFilter])
  const visible = useMemo(() => sortRuns(filtered, sortKey, sortDir), [filtered, sortKey, sortDir])
  const metricSortAvailable = taskFilter !== ALL && metricComparable(filtered)
  useEffect(() => {
    if (sortKey === 'metric' && !metricSortAvailable) setSortKey('time')
  }, [sortKey, metricSortAvailable])

  const autoMapCollapsed = useMemo(
    () => defaultCollapsedClusters(proj.projects, visible, subtree),
    [proj.projects, visible, subtree])
  const mapCollapsed = useMemo(() => {
    const next = new Set(autoMapCollapsed)
    mapCollapseOverrides.forEach((collapsed, id) => collapsed ? next.add(id) : next.delete(id))
    return next
  }, [autoMapCollapsed, mapCollapseOverrides])
  const toggleMapCluster = (id) => setMapCollapseOverrides(current => {
    const next = new Map(current); next.set(id, !mapCollapsed.has(id)); return next
  })

  const breadcrumb = useMemo(() => {
    if (sel === ALL || sel === UNASSIGNED) return []
    const path = []; let cur = proj.projects.find(p => p.id === sel)
    while (cur) { path.unshift(cur); cur = proj.projects.find(p => p.id === cur.parent_id) }
    return path
  }, [sel, proj.projects])

  // The scope a cross-run report would cover, by the CURRENT view: an explicit super-task / task filter
  // is the user's intent to scope by it; otherwise the open folder. null = nothing reportable (All runs,
  // no filter) → hide the Report button.
  const scope = useMemo(() => {
    if (stFilter !== ALL && stFilter !== UNASSIGNED) return { type: 'supertask', id: stFilter, label: (stName[stFilter] || stFilter) }
    if (taskFilter !== ALL) return { type: 'task', id: taskFilter, label: 'task ' + taskFilter }
    if (sel !== ALL && sel !== UNASSIGNED) return { type: 'project', id: sel, label: (projName[sel] || sel) }
    return null
  }, [stFilter, taskFilter, sel, stName, projName])

  const toggle = (id) => setExpanded(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const refresh = () => Promise.all([loadProjects(), loadRuns()])

  const restoreProjectModalFocus = () => requestAnimationFrame(() => {
    const target = projectModalReturnRef.current
    if (target?.isConnected) target.focus({ preventScroll: true })
    projectModalReturnRef.current = null
  })
  const addProject = (parent_id, returnFocus) => {
    projectModalReturnRef.current = returnFocus || document.activeElement
    setProjModal({ parent_id })
  }
  const closeProjectModal = () => { setProjModal(null); restoreProjectModalFocus() }
  const submitProject = async (name) => {
    const parent_id = projModal?.parent_id; setProjModal(null); restoreProjectModalFocus()
    const p = await createProject(name, parent_id)
    if (parent_id) setExpanded(s => new Set(s).add(parent_id))
    await loadProjects(); setSel(p.id)
  }
  const startProjectRename = (id, returnFocus) => {
    projectRenameReturnRef.current = returnFocus
    setRenaming(id)
  }
  const finishProjectRename = async (id, name, restoreFocus = false) => {
    setRenaming(null)
    if (restoreFocus) requestAnimationFrame(() => projectRenameReturnRef.current?.isConnected
      && projectRenameReturnRef.current.focus({ preventScroll: true }))
    if (name?.trim()) { await patchProject(id, { name: name.trim() }); loadProjects() }
  }
  const removeProject = async (id) => {
    if (!confirm(`Delete project "${projName[id]}"? Sub-projects and runs move up to its parent.`)) return
    await deleteProject(id); if (sel === id) setSel(ALL); await refresh()
    requestAnimationFrame(() => projectsAllRef.current?.focus({ preventScroll: true }))
  }
  const moveRun = async (runId, project_id) => { await assignRun(runId, project_id === UNASSIGNED ? null : project_id); refresh() }
  const onDrop = async (project_id) => { if (dragRun) { await moveRun(dragRun, project_id); setDragRun(null) } }
  const submitRunRename = async (label) => {
    const id = runRename.run_id; setRunRename(null); restoreRunModalFocus()
    await renameRun(id, label); loadRuns()
  }
  const removeRun = async (r) => {
    const returnFocus = runMenuTriggerRef.current
    const card = returnFocus?.closest('.run-card')
    const fallbackFocus = card?.nextElementSibling?.querySelector('.run-card-main')
      || card?.previousElementSibling?.querySelector('.run-card-main')
    setRunMenu(null)
    if (!confirm(`Delete run "${r.label || r.run_id}" permanently? This removes its files on disk and cannot be undone.`)) {
      requestAnimationFrame(() => returnFocus?.isConnected && returnFocus.focus({ preventScroll: true })); return
    }
    try { await deleteRun(r.run_id); await refresh() }
    catch (e) {
      // A live engine still holds the run → backend 409 (the detail text has no "409" in it, so branch
      // on the status code _throw now attaches, not a regex on the message). Anything else: surface it.
      alert(e.status === 409 || /409/.test(e.message)
        ? 'This run is still live — pause or stop it before deleting.'
        : 'Delete failed: ' + e.message)
    }
    finally { requestAnimationFrame(() => {
      const target = returnFocus?.isConnected ? returnFocus : fallbackFocus?.isConnected ? fallbackFocus : runsMainRef.current
      target?.focus({ preventScroll: true })
    }) }
  }
  // super-task CRUD (the buckets themselves; per-run assignment is assignToSuper above).
  const createSuper = async (name) => { await createSupertask(name); loadSupers() }
  const renameSuper = async (id, name) => { await renameSupertask(id, name); loadSupers() }
  const removeSuper = async (s) => {
    if (!confirm(`Delete super-task "${s.name}"? Runs in it become unassigned (the runs themselves are kept).`)) return false
    if (stFilter === s.id) setStFilter(ALL)
    await deleteSupertask(s.id); await Promise.all([loadSupers(), loadRuns()]); return true
  }

  // TreeNode lives at MODULE scope (below) so its component identity is stable across the 2.5s runs
  // poll; defined inline it remounted the whole subtree every poll, wiping the inline-rename input.
  const chooseProject = (id) => { setSel(id); setProjectsOpen(false) }
  const treeCtx = { byParent, expanded, sel, setSel: chooseProject, onDrop, toggle, renaming,
                    finishProjectRename, startProjectRename,
                    count, addProject, removeProject }

  return (
    <main ref={runsMainRef} className="app" data-route-main tabIndex={-1}>
      <h1 className="sr-only">Runs</h1>
      <div className="topbar home-head"><span className="brand"><span className="dot">◉</span> LoopLab</span>
        <span className="muted home-subtitle">autonomous R&D — live runs</span>
        <button ref={projectsToggleRef} className="btn sm ghost projects-toggle" onClick={() => setProjectsOpen(true)}
                aria-expanded={projectsOpen} aria-controls="projects-drawer">
          <OpIcon name="folder" className="t-ic" /> Projects
        </button>
        <button className="btn sm primary new-run-cta"
                onClick={() => window.dispatchEvent(new CustomEvent('ll:new-run'))}>
          ＋ New run
        </button>
        <span className="spacer" style={{ flex: 1 }} />
        <div className="seg">
          <button aria-pressed={view === 'list'} className={view === 'list' ? 'on' : ''} onClick={() => setView('list')}><OpIcon name="list" className="t-ic" /> List</button>
          <button aria-pressed={view === 'map'} className={view === 'map' ? 'on' : ''} onClick={() => setView('map')}><OpIcon name="map" className="t-ic" /> Map</button>
        </div>
        {/* Slash remains a power-user shortcut; New run above is the first-use primary action. */}
        <span className="muted home-new-hint" style={{ fontSize: 11 }}>type <code className="cmd-hint">/new</code> in the bar below to start a run</span>
        <span className="spacer" style={{ flex: 1 }} />
        <div className="home-actions">
          <ThemeSwitcher />
          <EnergyToggle />
          <button type="button" className="btn sm ghost" title="Read the experimental bounded portfolio preview"
                  aria-label="Open Research Atlas preview" onClick={() => onResearchAtlas?.()}>
            <OpIcon name="compass" className="t-ic" /> Atlas preview
          </button>
          <button className="btn sm ghost" title="settings" onClick={() => onSettings && onSettings()}><OpIcon name="gear" className="t-ic" /> Settings</button>
        </div>
      </div>

      <div className={'runlayout' + (projectsOpen ? ' projects-open' : '')}>
        {projectsOpen && <button className="project-backdrop" onClick={() => setProjectsOpen(false)}
                                 aria-label="Close projects" />}
        <aside ref={projectsDialogRef} className="psidebar" id="projects-drawer" aria-label="Projects"
               role={compactNav && projectsOpen && !projModal ? 'dialog' : undefined}
               aria-modal={compactNav && projectsOpen && !projModal ? 'true' : undefined}
               tabIndex={compactNav ? -1 : undefined}
               aria-hidden={compactNav && (!projectsOpen || !!projModal) ? 'true' : undefined}
               inert={compactNav && (!projectsOpen || !!projModal) ? '' : undefined}>
          <div className="psidebar-h">
            <b>Projects</b>
            <button ref={projectsCloseRef} className="btn sm ghost projects-close" onClick={() => setProjectsOpen(false)}
                    aria-label="Close projects">×</button>
            <button className="btn sm" onClick={event => addProject(null, event.currentTarget)}>＋ New</button>
          </div>
          {projectsError && <div className="notice compact-error" role="alert">Projects unavailable. <button className="btn sm" onClick={loadProjects}>Retry</button></div>}
          <button ref={projectsAllRef} type="button" className={'ptree-row pseudo' + (sel === ALL ? ' sel' : '')}
               onClick={() => chooseProject(ALL)} aria-pressed={sel === ALL}>
            <span className="ptw">▦</span><span className="pname">All runs</span><span className="pcount">{count(ALL)}</span>
          </button>
          <button type="button" className={'ptree-row pseudo' + (sel === UNASSIGNED ? ' sel' : '')}
               onClick={() => chooseProject(UNASSIGNED)} aria-pressed={sel === UNASSIGNED}
               onDragOver={e => e.preventDefault()} onDrop={() => onDrop(UNASSIGNED)}>
            <span className="ptw">○</span><span className="pname">Unassigned</span><span className="pcount">{count(UNASSIGNED)}</span>
          </button>
          <nav className="ptree" aria-label="Project folders">
            {(byParent[null] || []).map(p => <TreeNode key={p.id} p={p} depth={0} ctx={treeCtx} />)}
            {!proj.projects.length && <div className="muted" style={{ padding: 10, fontSize: 12 }}>No projects yet. Create one to organize runs.</div>}
          </nav>
        </aside>

        <div className={'runlist' + (view === 'map' ? ' map-list-shell' : '')}>
          <div className="crumbs">
            <button type="button" className="crumb" onClick={() => chooseProject(ALL)}>All runs</button>
            {breadcrumb.map(p => <React.Fragment key={p.id}><span className="sep">/</span>
              <button type="button" className="crumb" onClick={() => chooseProject(p.id)}>{p.name}</button></React.Fragment>)}
            {sel === UNASSIGNED && <><span className="sep">/</span><span className="crumb">Unassigned</span></>}
            <span style={{ flex: 1 }} />
            {scope && <div className="view-toggle crumb-report">
              <button className={'vt report' + (showReport ? ' on' : '')} title={`cross-run report for ${scope.label}`}
                onClick={() => setShowReport(true)}><OpIcon name="doc" size={12} /> Report<span className="vt-scope"> · {scope.label}</span></button>
            </div>}
          </div>
          {runs && !!runsOf(sel).length && <div className="runbar">
            <OpIcon name="search" className="t-ic" />
            <input className="text runbar-q" aria-label="Filter runs" placeholder="filter runs…" value={query}
                   onChange={e => setQuery(e.target.value)} />
            <select className="sel" aria-label="Filter by status" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
              <option value="all">all status</option>
               <option value="running">running</option>
               <option value="finalizing">finalizing</option>
               <option value="paused">paused</option>
               <option value="approval">approval needed</option>
               <option value="stalled">stalled</option>
               <option value="unknown">ownership unknown</option>
              <option value="finished">finished</option>
            </select>
            <select className="sel" aria-label="Filter by task" value={taskFilter} onChange={e => setTaskFilter(e.target.value)}>
              <option value={ALL}>all tasks</option>
              {tasks.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
            <select className="sel" aria-label="Filter by super-task" value={stFilter} onChange={e => setStFilter(e.target.value)}>
              <option value={ALL}>all super-tasks</option>
              <option value={UNASSIGNED}>— no super-task —</option>
              {superdata.supertasks.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <button className="btn sm ghost" aria-label="Create or manage super-tasks"
              title="create / manage super-tasks" onClick={event => openSuperTasks(event.currentTarget)}><OpIcon name="target" className="t-ic" /> ＋</button>
            <span style={{ flex: 1 }} />
            <span className="muted runbar-count">{visible.length}/{runsOf(sel).length}</span>
            <select className="sel" aria-label="Sort runs by" value={sortKey} onChange={e => {
              setSortKey(e.target.value)
              if (e.target.value === 'metric') setSortDir('asc')
            }}>
              <option value="time">time</option>
              <option value="name">name</option>
              <option value="metric" disabled={!metricSortAvailable}>best metric{metricSortAvailable ? '' : ' (select one task)'}</option>
              <option value="task">task</option>
              <option value="nodes">nodes</option>
              <option value="phase">phase</option>
            </select>
            <button className="btn sm ghost"
                    title={sortKey === 'metric' ? (sortDir === 'asc' ? 'best first' : 'worst first') : (sortDir === 'asc' ? 'ascending' : 'descending')}
                    onClick={() => setSortDir(d => d === 'asc' ? 'desc' : 'asc')}>
              {sortKey === 'metric' ? (sortDir === 'asc' ? 'best' : 'worst') : (sortDir === 'asc' ? '↑' : '↓')}
            </button>
          </div>}
          {runs == null && !runsError && <div className="notice" role="status">Loading runs…</div>}
          {runs == null && runsError && <div className="notice resource-error" role="alert"><b>Could not load runs.</b><span>{runsError}</span><button className="btn sm primary" onClick={loadRuns}>Retry</button></div>}
          {runs != null && runsError && <div className="notice resource-warning" role="status">Could not refresh runs; showing the last loaded data. <button className="btn sm" onClick={loadRuns}>Retry</button></div>}
          {runs && !runsOf(sel).length && <div className="notice resource-empty">No runs here.
            {sel === ALL
              ? <button className="btn sm primary" onClick={() => window.dispatchEvent(new CustomEvent('ll:new-run'))}>Start a new run</button>
              : <span>Drag a run onto this project, or use its <b>Move</b> menu.</span>}</div>}
          {runs && !!runsOf(sel).length && !visible.length && <div className="notice">No runs match the filter.</div>}
          {view === 'map' && projectsError && <div className="notice resource-error" role="alert"><b>Map unavailable.</b><span>Project metadata is required to place runs correctly.</span><button className="btn sm primary" onClick={loadProjects}>Retry</button></div>}
          {view === 'map' && !projectsError && runs && visible.length > 0 && <div className="map-stage">
            <LazyBoundary label="run map" resetKey={`map:${sel}`}>
              <MapView onOpen={onOpen} runs={visible} projects={proj.projects}
                collapsed={mapCollapsed} onToggle={toggleMapCluster}
                scopeLabel={scope?.label || (sel === ALL ? 'All runs' : sel === UNASSIGNED ? 'Unassigned' : (projName[sel] || sel))} />
            </LazyBoundary>
          </div>}
          {view === 'list' && runs && visible.map(r => (
            <div className="run-card" key={r.run_id} draggable
                 onDragStart={() => setDragRun(r.run_id)} onDragEnd={() => setDragRun(null)}>
              {(() => {
                // A zombie (not finished, but no engine holds the lock) reads as "search" from phase
                // alone — surface it as "stalled" so the list matches the run header's badge.
                const status = effectiveRunStatus(r)
                const stalled = status === 'stalled'
                return <span className={'pill phase ' + status}
                             title={stalled ? 'engine stopped unexpectedly — open the run to resume'
                                : status === 'unknown' ? 'engine ownership could not be verified; inspect before acting'
                                : status === 'finalizing' ? 'wrapping up report, lessons, and cost'
                                : status === 'paused' ? 'paused intentionally' : undefined}>{status}</span>
              })()}
              <a className="run-card-main" href={`#/run/${encodeURIComponent(r.run_id)}`}
                   draggable={false}
                   onClick={event => followClientRoute(event, () => onOpen(r.run_id))}
                   aria-label={`Open run ${r.label || r.run_id}`}>
                <div><b>{r.label || r.run_id}</b> <span className="muted">· {r.label ? r.run_id + ' · ' : ''}{r.task_id}</span>
                  {r.project_id && projName[r.project_id] && <span className="pill" style={{ marginLeft: 6 }}><OpIcon name="folder" className="t-ic" /> {projName[r.project_id]}</span>}
                  {r.supertask_id && stName[r.supertask_id] && <span className="pill st-pill" style={{ marginLeft: 6 }}><OpIcon name="target" className="t-ic" /> {stName[r.supertask_id]}</span>}</div>
                <div className="goal">{r.goal}</div>
              </a>
              <div className="run-card-metrics" style={{ textAlign: 'right' }}>
                <div>best <b>{fmt(r.best_confirmed ?? r.best_metric)}</b></div>
                <div className="muted">{r.nodes} nodes · {r.direction}</div>
                {r.mtime && <div className="muted run-when"
                  title={`started ${fmtDate(r.created)} · updated ${fmtDate(r.mtime)}`}>
                  {fmtAgo(r.mtime)}</div>}
              </div>
              <div className="run-actions">
                <button className="ic dots" aria-label={`Actions for run ${r.label || r.run_id}`}
                        aria-haspopup="menu" aria-expanded={runMenu === r.run_id}
                        onClick={e => {
                          e.stopPropagation()
                          if (runMenu === r.run_id) closeRunMenu(false)
                          else { runMenuTriggerRef.current = e.currentTarget; setRunMenu(r.run_id) }
                        }}>⋮</button>
                {runMenu === r.run_id && <RunMenu r={r} projects={proj.projects} supertasks={superdata.supertasks}
                  onOpen={onOpen} onMove={moveRun} onSetSuper={assignToSuper} onManageSupers={() => openSuperTasks(runMenuTriggerRef.current)}
                  onRename={openRunRename} onDelete={removeRun} onClose={closeRunMenu} />}
              </div>
            </div>
          ))}
        </div>
      </div>

      {projModal && <PromptModal
        title={projModal.parent_id ? 'New sub-project' : 'New project'}
        label={projModal.parent_id ? `Inside “${projName[projModal.parent_id]}”` : 'Group runs into a project folder.'}
        placeholder="e.g. baseline sweep" confirm="Create"
        onSubmit={submitProject} onClose={closeProjectModal} />}

      {runRename && <PromptModal
        title="Rename run" label={`Display name for ${runRename.run_id} (clear it to fall back to the id).`}
        placeholder={runRename.run_id} initial={runRename.label || ''} confirm="Save" allowEmpty
        onSubmit={submitRunRename} onClose={closeRunRename} />}

      {stModal && <SuperTaskModal supertasks={superdata.supertasks}
        onCreate={createSuper} onRename={renameSuper} onDelete={removeSuper}
        onClose={closeSuperTasks} />}

      {showReport && scope && <LazyBoundary label="scope report" mode="overlay" resetKey={scope.label}>
        <ScopeReport scope={scope}
          onOpen={(id) => { setShowReport(false); onOpen(id) }} onClose={() => setShowReport(false)} />
      </LazyBoundary>}
    </main>
  )
}
