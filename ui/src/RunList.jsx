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

export function useResource(read, initial) {
  const [data, setData] = useState(initial)
  const [state, setState] = useState('loading')
  const version = useRef(0)
  useEffect(() => () => { version.current += 1 }, [])
  const load = () => {
    const owner = ++version.current
    // # CODEX AGENT: Poll, visibility and post-mutation reads can overlap. Only the newest request
    // owns the resource; cleanup invalidates every late settlement after this component unmounts.
    return Promise.resolve().then(read)
      .then(value => {
        if (version.current !== owner) return
        setData(value); setState('ready')
      })
      .catch(() => {
        if (version.current !== owner) return
        setState(current => ['ready', 'stale'].includes(current) ? 'stale' : 'error')
      })
  }
  return [data, state, load]
}

function ResourceNotice({ state, label, retry }) {
  if (state === 'ready') return null
  if (state === 'loading') return <div className="notice" role="status">{label} loading…</div>
  const stale = state === 'stale'
  return <div className={'notice ' + (stale ? 'resource-warning' : 'resource-error')}
    role={stale ? 'status' : 'alert'}>
    {label}: {stale ? 'Last loaded data; refresh failed.' : 'Unavailable.'} <button className="btn sm" onClick={retry}>Retry</button>
  </div>
}

const mutationMessage = (error, timedOut = false) => timedOut
  ? 'Save timed out; its outcome is unknown. Refresh before retrying.'
  : error?.status === 409
    ? 'Conflict; current input or selection kept.'
    : error?.status === 503
      ? 'Unavailable; current input or selection kept.'
      : 'Save failed; current input or selection kept.'

// Prompt-local input and menu selection survive a failed mutation until the operator retries.

function useMutation() {
  const lock = useRef(false)
  const [state, setState] = useState(null)
  const run = async action => {
    if (lock.current) return false
    lock.current = true; setState(true)
    try {
      // The transport is intentionally not replayed: after the local deadline its write outcome is
      // ambiguous. Releasing the page-wide interaction lock with explicit unknown-outcome copy is
      // safer than freezing every navigation surface until a hung proxy eventually settles.
      const settlement = await settleWithin(action, LIST_WRITE_TIMEOUT_MS)
      if (!settlement.ok) {
        setState(mutationMessage(settlement.error, settlement.timeout)); return false
      }
      const outcome = settlement.value
      // A caller may own a stronger reconciliation contract (for example useListMutation below).
      // Keep its explicit unknown/failure result authoritative instead of closing the menu as success.
      if (outcome === false) { setState(null); return false }
      setState(null); return true
    }
    catch (error) { setState(mutationMessage(error)); return false }
    finally { lock.current = false }
  }
  return [state === true, typeof state === 'string' ? state : '', run, setState]
}

const focusSoon = target => requestAnimationFrame(() => target?.isConnected && target.focus({ preventScroll: true }))

const listMutationMessage = (kind, error) => {
  if (kind === 'delete-run') return error?.status === 409
    ? 'This run is still live. Pause or stop it before deleting.'
    : 'Run deletion was not confirmed. Check the refreshed list before retrying.'
  if (kind === 'delete-project') return error?.status === 409
    ? 'This project changed elsewhere and was not deleted. Refresh before retrying.'
    : 'Project deletion was not confirmed. Check the refreshed list before retrying.'
  return error?.status === 409
    ? 'This assignment changed elsewhere and the move was not applied. Refresh before retrying.'
    : 'The move was not confirmed. Check the refreshed list before retrying.'
}

const LIST_WRITE_TIMEOUT_MS = 12_000
const LIST_RECONCILE_TIMEOUT_MS = 8_000

// Resolve exactly once even when unabortable transport settles after the local deadline. Attaching
// both handlers up front also prevents a late rejection from becoming an unhandled promise.
const settleWithin = (work, timeout) => new Promise(resolve => {
  let settled = false
  const finish = result => {
    if (settled) return
    settled = true; clearTimeout(timer); resolve(result)
  }
  const timer = setTimeout(() => finish({ timeout: true }), timeout)
  Promise.resolve().then(work).then(
    value => finish({ ok: true, value }),
    error => finish({ error }),
  )
})

// Destructive list actions and drag/drop share one lock. A failed transport can have an ambiguous
// outcome, so this guard never replays a write; callers reconcile with a read before the operator can
// explicitly try again.
export function useListMutation({ actionTimeout = LIST_WRITE_TIMEOUT_MS, reconcileTimeout = LIST_RECONCILE_TIMEOUT_MS } = {}) {
  const lock = useRef(false)
  const version = useRef(0)
  const [state, setState] = useState(null)
  useEffect(() => () => { version.current += 1 }, [])
  const run = async (kind, label, action, reconcile) => {
    if (lock.current) return false
    const token = ++version.current
    const update = value => { if (version.current === token) setState(value) }
    lock.current = true; update({ busy: true, label })
    try {
      const outcome = await settleWithin(action, actionTimeout)
      if (outcome.ok) { update(null); return true }
      let message = listMutationMessage(kind, outcome.error)
      if (reconcile) {
        update({ busy: true, label: 'Checking the current list before retry…' })
        const check = await settleWithin(reconcile, reconcileTimeout)
        if (check.timeout) message += ' The follow-up list check timed out.'
        else if (!check.ok) message += ' The follow-up list check failed.'
      }
      update({ busy: false, error: message }); return false
    }
    finally { lock.current = false }
  }
  const clear = () => { version.current += 1; setState(null) }
  return [state, run, clear]
}

// Module-scope so its identity is stable across re-renders (the runs list polls every 2.5s). Defined
// inside RunList it got a fresh identity each render, so React remounted the whole project subtree and
// the uncontrolled inline-rename <input> lost its text/focus mid-edit. All render state is threaded
// through `ctx`.
export function TreeNode({ p, depth, ctx }) {
  const { byParent, expanded, sel, setSel, onDrop, toggle, renaming, finishProjectRename, startProjectRename,
          projectBusy, projectError, count, addProject, removeProject } = ctx
  const kids = byParent[p.id] || []
  const open = expanded.has(p.id)
  const commitRename = async (input, value, restoreFocus) => {
    if (projectBusy || input.dataset.pending) return
    input.dataset.pending = 'true'
    const finished = await finishProjectRename(p.id, value, restoreFocus)
    if (!finished && input.isConnected) delete input.dataset.pending
  }
  return <div className="ptree-node">
    <div className={'ptree-row' + (sel === p.id ? ' sel' : '')} style={{ paddingLeft: 6 + depth * 14 }}
         aria-busy={renaming === p.id && projectBusy}
         onDragOver={e => { if (!projectBusy) e.preventDefault() }} onDrop={() => { if (!projectBusy) onDrop(p.id) }}>
      {kids.length
        ? <button type="button" className="ptw" disabled={projectBusy} aria-label={`${open ? 'Collapse' : 'Expand'} ${p.name}`}
            aria-expanded={open} onClick={() => toggle(p.id)}>{open ? '▾' : '▸'}</button>
        : <span className="ptw" aria-hidden="true">·</span>}
      {renaming === p.id
        ? <input className="text ptree-rename" autoFocus readOnly={projectBusy} defaultValue={p.name}
                 aria-label={`Rename project ${p.name}`}
                 onBlur={e => { if (!e.currentTarget.dataset.pending) commitRename(e.currentTarget, e.currentTarget.value, false) }}
                 onKeyDown={e => {
                   if (e.key === 'Enter') {
                     e.preventDefault(); commitRename(e.currentTarget, e.currentTarget.value, true)
                   }
                   if (e.key === 'Escape') {
                     // Cancel: a blank name skips the PATCH (the guard in finishProjectRename), so
                     // Escape reverts to the current name without a redundant server write + reload.
                     e.preventDefault(); commitRename(e.currentTarget, '', true)
                   }
                 }} />
        : <button type="button" className="pname project-choice" disabled={projectBusy} aria-pressed={sel === p.id}
            onClick={() => setSel(p.id)}><OpIcon name="folder" className="t-ic" /> {p.name}</button>}
      <span className="pcount">{count(p.id)}</span>
      <span className="pacts">
        <button className="ic" disabled={projectBusy} aria-label={`Add sub-project inside ${p.name}`}
          onClick={event => addProject(p.id, event.currentTarget)}>＋</button>
        <button className="ic" disabled={projectBusy} aria-label={`Rename project ${p.name}`} onClick={event => startProjectRename(p.id, event.currentTarget)}><OpIcon name="pencil" size={12} /></button>
        <button className="ic" disabled={projectBusy} aria-label={`Delete project ${p.name}`} onClick={event => removeProject(p.id, event.currentTarget)}>✕</button>
      </span>
    </div>
    {renaming === p.id && projectBusy && <div className="muted" role="status">Saving project name…</div>}
    {renaming === p.id && projectError && <div className="flag" role="alert">{projectError}</div>}
    {open && <div className="ptree-children">{kids.map(k => <TreeNode key={k.id} p={k} depth={depth + 1} ctx={ctx} />)}</div>}
  </div>
}

// Small centered popup (replaces window.prompt for project create / run rename).
function Modal({ title, onClose, children, busy = false }) {
  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, busy ? null : onClose)
  return <div className="overlay" onMouseDown={event => { if (!busy && event.target === event.currentTarget) onClose?.() }}>
    <div ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-label={title} aria-busy={busy} tabIndex={-1}>
      <div className="modal-h"><b>{title}</b><span style={{ flex: 1 }} />
        <button className="btn sm ghost" disabled={busy} onClick={onClose} aria-label={`Close ${title}`}>✕</button></div>
      <div className="modal-b">{children}</div>
    </div>
  </div>
}

function PromptModal({ title, label, placeholder, initial = '', confirm = 'Create', allowEmpty = false, onSubmit, onClose }) {
  const [v, setV] = useState(initial)
  const [busy, error, mutate] = useMutation()
  const ok = allowEmpty || !!v.trim()
  const go = async () => { if (ok && await mutate(() => onSubmit(v.trim()))) onClose() }
  return <Modal title={title} onClose={onClose} busy={busy}>
    {label && <div className="muted" style={{ marginBottom: 8 }}>{label}</div>}
    <input className="text" autoFocus readOnly={busy} aria-label={label || title} placeholder={placeholder} value={v} onChange={e => setV(e.target.value)}
           onKeyDown={e => { if (e.key === 'Enter') go(); if (e.key === 'Escape' && !busy) onClose() }} />
    {error && <div className="flag" role="alert">{error}</div>}
    <div className="modal-actions">
      <button className="btn sm ghost" disabled={busy} onClick={onClose}>Cancel</button>
      <button className="btn sm primary" disabled={!ok || busy} onClick={go}>{busy ? 'Saving…' : confirm}</button>
    </div>
  </Modal>
}

// Per-run "⋮" dropdown: open / rename / move (project) / assign (super-task) / delete.
function RunMenu({ r, projects, supertasks, onOpen, onMove, onSetSuper, onManageSupers, onRename, onDelete, onClose, onBusyChange }) {
  const menuRef = useRef(null)
  const [busy, error, mutate] = useMutation()
  useEffect(() => { menuRef.current?.querySelector('[role="menuitem"]')?.focus() }, [])
  useEffect(() => {
    onBusyChange?.(busy)
    return () => onBusyChange?.(false)
  }, [busy, onBusyChange])
  const close = (restore = false) => { if (!busy) onClose(restore) }
  const act = async action => { if (await mutate(action)) onClose(true) }
  const onKeyDown = event => {
    if (busy && event.key === 'Tab') { event.preventDefault(); return }
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
      aria-busy={busy} aria-disabled={busy}
      onClick={e => e.stopPropagation()} onClickCapture={e => { if (busy) { e.preventDefault(); e.stopPropagation() } }} onKeyDown={onKeyDown}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(false) }}>
      <button type="button" role="menuitem" tabIndex={-1} className="mi" onClick={() => { close(false); onOpen(r.run_id) }}>↗ Open</button>
      <button type="button" role="menuitem" tabIndex={-1} className="mi" onClick={() => { close(false); onRename(r) }}><OpIcon name="pencil" size={12} /> Rename</button>
      <div className="mi-sep" />
      <div className="mi-label">Move to project</div>
      <div className="mi-scroll">
        <button type="button" role="menuitem" tabIndex={-1} className={'mi' + (!r.project_id ? ' on' : '')} onClick={() => act(() => onMove(r.run_id, UNASSIGNED))}>○ — unassigned —</button>
        {projects.map(p => <button type="button" role="menuitem" tabIndex={-1} key={p.id} className={'mi' + (r.project_id === p.id ? ' on' : '')}
          onClick={() => act(() => onMove(r.run_id, p.id))}><OpIcon name="folder" className="t-ic" /> {p.name}</button>)}
        {!projects.length && <div className="mi-empty">no projects yet</div>}
      </div>
      <div className="mi-sep" />
      <div className="mi-label">Super-task</div>
      <div className="mi-scroll">
        <button type="button" role="menuitem" tabIndex={-1} className={'mi' + (!r.supertask_id ? ' on' : '')} onClick={() => act(() => onSetSuper(r.run_id, UNASSIGNED))}>○ — none —</button>
        {supertasks.map(s => <button type="button" role="menuitem" tabIndex={-1} key={s.id} className={'mi' + (r.supertask_id === s.id ? ' on' : '')}
          onClick={() => act(() => onSetSuper(r.run_id, s.id))}><OpIcon name="target" className="t-ic" /> {s.name}</button>)}
        <button type="button" role="menuitem" tabIndex={-1} className="mi accent" onClick={() => { close(false); onManageSupers() }}>＋ New / manage…</button>
      </div>
      {busy && <div className="muted" role="status">Saving…</div>}
      {error && <div className="flag" role="alert">{error}</div>}
      <div className="mi-sep" />
      <button type="button" role="menuitem" tabIndex={-1} className="mi danger" onClick={() => onDelete(r)}>✕ Delete run…</button>
    </div>
  </>
}

// Manage super-tasks in one popup: create, rename (inline), delete. Assignment happens per-run via
// the ⋮ menu / drag; this is just the CRUD over the buckets themselves.
function SuperTaskModal({ supertasks, state, onRetry, onCreate, onRename, onDelete, onClose }) {
  const [name, setName] = useState('')
  const newTaskRef = useRef(null)
  const [busy, error, mutate] = useMutation()
  const add = async () => { const v = name.trim(); if (v && await mutate(() => onCreate(v))) setName('') }
  const edit = (task, input) => { const v = input.value.trim(); if (v && v !== task.name) mutate(() => onRename(task.id, v)) }
  const remove = async (task, event) => {
    const row = event.currentTarget.closest('.st-row')
    const fallback = row?.nextElementSibling?.querySelector('.st-rename')
      || row?.previousElementSibling?.querySelector('.st-rename') || newTaskRef.current
    let removed
    const saved = await mutate(async () => { removed = await onDelete(task) })
    if (saved && removed) requestAnimationFrame(() => fallback?.isConnected
      ? fallback.focus({ preventScroll: true }) : newTaskRef.current?.focus({ preventScroll: true }))
  }
  return <Modal title="Super-tasks" onClose={onClose} busy={busy}>
    <div className="muted" style={{ marginBottom: 8 }}>A super-task groups runs that attack the same global task (across many runs). Assign runs from a run’s ⋮ menu.</div>
    <ResourceNotice state={state} label="Super-tasks" retry={onRetry} />
    {busy && <div className="muted" role="status">Saving super-task changes…</div>}
    {error && <div className="flag" role="alert">{error}</div>}
    <div className="st-new">
      <input ref={newTaskRef} className="text" autoFocus readOnly={busy} aria-label="New super-task name" placeholder="New super-task name (e.g. nomad2018)" value={name}
             onChange={e => setName(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') add() }} />
      <button className="btn sm primary" disabled={busy || !name.trim()} onClick={add}>＋ Create</button>
    </div>
    <div className="st-list">
      {supertasks.map(s => <div key={s.id} className="st-row">
        <span className="st-ic"><OpIcon name="target" className="t-ic" /></span>
        <input className="text st-rename" readOnly={busy} defaultValue={s.name} aria-label={`Rename super-task ${s.name}`}
               onBlur={e => edit(s, e.currentTarget)}
               onKeyDown={e => {
                 if (e.key === 'Enter') {
                   e.preventDefault(); edit(s, e.currentTarget)
                 }
               }} />
        <button className="ic" disabled={busy} aria-label={`Delete super-task ${s.name}`} onClick={event => remove(s, event)}>✕</button>
      </div>)}
      {state === 'ready' && !supertasks.length && <div className="muted" style={{ padding: '8px 2px', fontSize: 12 }}>No super-tasks yet.</div>}
    </div>
  </Modal>
}

export default function RunList({ onOpen, onSettings, onResearchAtlas }) {
  const compactNav = useMediaQuery('(max-width: 900px)')
  const [runs, runsState, loadRuns] = useResource(() => get('/api/runs'), null)
  const [proj, projectsState, loadProjects] = useResource(listProjects, { projects: [], assignments: {} })
  const [sel, setSel] = useState(ALL)
  const [expanded, setExpanded] = useState(() => new Set())
  const [renaming, setRenaming] = useState(null)   // project id being renamed (inline)
  const [projectBusy, projectError, saveProjectRename, clearProjectError] = useMutation()
  const [listMutation, mutateList, clearListMutation] = useListMutation()
  const [menuBusy, setMenuBusy] = useState(false)
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
  const [superdata, superState, loadSupers] = useResource(listSupertasks, { supertasks: [], assignments: {} })
  const [stModal, setStModal] = useState(false)             // manage-super-tasks popup open?
  const [showReport, setShowReport] = useState(false)       // cross-run scope-report panel open?
  const [projectsOpen, setProjectsOpen] = useState(false)   // compact-screen Projects drawer
  const projectsToggleRef = useRef(null)
  const projectsCloseRef = useRef(null)
  const projectsDialogRef = useRef(null)
  const listBusy = !!listMutation?.busy
  const navigationBusy = listBusy || projectBusy || menuBusy
  const closeRunMenu = (restore = false) => {
    setRunMenu(null)
    if (restore) focusSoon(runMenuTriggerRef.current)
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
  // Keep the compact drawer active while any list or menu mutation is pending.
  useDialogFocus(projectsDialogRef, navigationBusy ? null : () => setProjectsOpen(false), compactNav && projectsOpen)
  // Run creation is no longer a modal here — it happens INSIDE the assistant (the bottom command bar's
  // "/new" asks the assistant to propose a run, which renders as an inline launch card in the chat).

  // Poll the runs list every 2.5s, but skip the request while the tab is hidden (no point refreshing a
  // list nobody's looking at) — and refresh once immediately when it becomes visible again.
  usePoll(loadRuns, 2500, [], { pauseHidden: true })
  useEffect(() => { loadProjects(); loadSupers() }, [])

  const stName = useMemo(() => Object.fromEntries(superdata.supertasks.map(s => [s.id, s.name])), [superdata])
  const assignToSuper = async (runId, sid) => { await assignSupertask(runId, sid === UNASSIGNED ? null : sid); await loadRuns() }

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
  const hasActiveFilters = !!query.trim() || taskFilter !== ALL || statusFilter !== 'all' || stFilter !== ALL
  const clearFilters = () => {
    setQuery(''); setTaskFilter(ALL); setStatusFilter('all'); setStFilter(ALL)
  }
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

  const restoreProjectModalFocus = () => {
    const target = projectModalReturnRef.current
    projectModalReturnRef.current = null
    focusSoon(target)
  }
  const addProject = (parent_id, returnFocus) => {
    projectModalReturnRef.current = returnFocus || document.activeElement
    setProjModal({ parent_id })
  }
  const closeProjectModal = () => { setProjModal(null); restoreProjectModalFocus() }
  const submitProject = async (name) => {
    const parent_id = projModal?.parent_id
    const p = await createProject(name, parent_id)
    if (parent_id) setExpanded(s => new Set(s).add(parent_id))
    await loadProjects(); setSel(p.id)
  }
  const startProjectRename = (id, returnFocus) => {
    clearProjectError(null)
    projectRenameReturnRef.current = returnFocus
    setRenaming(id)
  }
  const finishProjectRename = async (id, name, restoreFocus = false) => {
    const value = name?.trim()
    if (value && !await saveProjectRename(() => patchProject(id, { name: value }))) return false
    if (value) await loadProjects()
    else clearProjectError(null)
    setRenaming(null)
    if (restoreFocus) requestAnimationFrame(() => projectRenameReturnRef.current?.isConnected
      && projectRenameReturnRef.current.focus({ preventScroll: true }))
    return true
  }
  const removeProject = async (id, returnFocus) => {
    if (!confirm(`Delete project "${projName[id]}"? Sub-projects and runs move up to its parent.`)) return
    const removed = await mutateList('delete-project', `Deleting project “${projName[id]}”…`,
      async () => { await deleteProject(id); await refresh() }, refresh)
    if (removed && sel === id) setSel(ALL)
    if (removed) requestAnimationFrame(() => projectsAllRef.current?.focus({ preventScroll: true }))
    else focusSoon(returnFocus)
  }
  const moveRun = async (runId, project_id) => {
    const target = project_id === UNASSIGNED ? 'Unassigned' : (projName[project_id] || 'project')
    return mutateList('move-run', `Moving run to “${target}”…`,
      async () => { await assignRun(runId, project_id === UNASSIGNED ? null : project_id); await refresh() }, refresh)
  }
  const onDrop = async (project_id) => {
    const runId = dragRun
    if (!runId || listBusy) return
    setDragRun(null)
    await moveRun(runId, project_id)
  }
  const submitRunRename = async (label) => {
    await renameRun(runRename.run_id, label); await loadRuns()
  }
  const removeRun = async (r) => {
    const menuFocus = document.activeElement
    const returnFocus = runMenuTriggerRef.current
    const card = returnFocus?.closest('.run-card')
    const fallbackFocus = card?.nextElementSibling?.querySelector('.run-card-main')
      || card?.previousElementSibling?.querySelector('.run-card-main')
    if (!confirm(`Delete run "${r.label || r.run_id}" permanently? This removes its files on disk and cannot be undone.`)) {
      focusSoon(menuFocus); return
    }
    setRunMenu(null)
    await mutateList('delete-run', `Deleting run “${r.label || r.run_id}”…`,
      async () => { await deleteRun(r.run_id); await refresh() }, refresh)
    requestAnimationFrame(() => {
      const target = returnFocus?.isConnected ? returnFocus : fallbackFocus?.isConnected ? fallbackFocus : runsMainRef.current
      target?.focus({ preventScroll: true })
    })
  }
  // super-task CRUD (the buckets themselves; per-run assignment is assignToSuper above).
  const createSuper = async (name) => { await createSupertask(name); await loadSupers() }
  const renameSuper = async (id, name) => { await renameSupertask(id, name); await loadSupers() }
  const removeSuper = async (s) => {
    if (!confirm(`Delete super-task "${s.name}"? Runs in it become unassigned (the runs themselves are kept).`)) return false
    if (stFilter === s.id) setStFilter(ALL)
    await deleteSupertask(s.id); await Promise.all([loadSupers(), loadRuns()]); return true
  }

  // TreeNode lives at MODULE scope (below) so its component identity is stable across the 2.5s runs
  // poll; defined inline it remounted the whole subtree every poll, wiping the inline-rename input.
  const chooseProject = (id) => { setSel(id); setProjectsOpen(false) }
  const treeCtx = { byParent, expanded, sel, setSel: chooseProject, onDrop, toggle, renaming,
                    finishProjectRename, startProjectRename, projectBusy, projectError,
                    count, addProject, removeProject }
  const mutationNotice = listMutation?.busy
    ? <div className="notice" role="status">{listMutation.label}</div>
    : listMutation?.error
      ? <div className="notice resource-error" role="alert">{listMutation.error}{' '}
          <button type="button" className="btn sm" onClick={clearListMutation}>Dismiss</button>
        </div>
      : null

  return (
    <main ref={runsMainRef} className="app" data-route-main tabIndex={-1} aria-busy={navigationBusy}>
      <h1 className="sr-only">Runs</h1>
      <div className="topbar home-head"><span className="brand"><span className="dot">◉</span> LoopLab</span>
        <span className="muted home-subtitle">autonomous R&D — live runs</span>
        <button ref={projectsToggleRef} className="btn sm ghost projects-toggle" disabled={navigationBusy} onClick={() => setProjectsOpen(true)}
                aria-expanded={projectsOpen} aria-controls="projects-drawer">
          <OpIcon name="folder" className="t-ic" /> Projects
        </button>
        <button className="btn sm primary new-run-cta" disabled={navigationBusy}
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
          <button type="button" className="btn sm ghost" disabled={navigationBusy} title="Read the experimental bounded portfolio preview"
                  aria-label="Open Research Atlas preview" onClick={() => onResearchAtlas?.()}>
            <OpIcon name="compass" className="t-ic" /> Atlas preview
          </button>
          <button className="btn sm ghost" disabled={navigationBusy} title="settings" onClick={() => onSettings && onSettings()}><OpIcon name="gear" className="t-ic" /> Settings</button>
        </div>
      </div>

      <div className={'runlayout' + (projectsOpen ? ' projects-open' : '')}>
        {projectsOpen && <button className="project-backdrop" disabled={projectBusy} aria-disabled={navigationBusy || undefined}
                                 onClick={() => { if (!navigationBusy) setProjectsOpen(false) }}
                                 aria-label="Close projects" />}
        <aside ref={projectsDialogRef} className="psidebar" id="projects-drawer" aria-label="Projects"
               role={compactNav && projectsOpen && !projModal ? 'dialog' : undefined}
               aria-modal={compactNav && projectsOpen && !projModal ? 'true' : undefined}
               tabIndex={compactNav ? -1 : undefined}
               aria-hidden={compactNav && (!projectsOpen || !!projModal) ? 'true' : undefined}
               inert={compactNav && (!projectsOpen || !!projModal) ? '' : undefined}>
          {compactNav && projectsOpen && mutationNotice}
          <div inert={listBusy ? '' : undefined}>
            <div className="psidebar-h">
              <b>Projects</b>
              <button ref={projectsCloseRef} className="btn sm ghost projects-close" disabled={projectBusy} onClick={() => setProjectsOpen(false)}
                      aria-label="Close projects">×</button>
              <button className="btn sm" disabled={projectBusy} onClick={event => addProject(null, event.currentTarget)}>＋ New</button>
            </div>
            <button ref={projectsAllRef} type="button" className={'ptree-row pseudo' + (sel === ALL ? ' sel' : '')}
                 disabled={projectBusy} onClick={() => chooseProject(ALL)} aria-pressed={sel === ALL}>
              <span className="ptw">▦</span><span className="pname">All runs</span><span className="pcount">{count(ALL)}</span>
            </button>
            <button type="button" className={'ptree-row pseudo' + (sel === UNASSIGNED ? ' sel' : '')}
                 disabled={projectBusy} onClick={() => chooseProject(UNASSIGNED)} aria-pressed={sel === UNASSIGNED}
                 onDragOver={e => { if (!projectBusy) e.preventDefault() }} onDrop={() => { if (!projectBusy) onDrop(UNASSIGNED) }}>
              <span className="ptw">○</span><span className="pname">Unassigned</span><span className="pcount">{count(UNASSIGNED)}</span>
            </button>
            <nav className="ptree" aria-label="Project folders">
              {(byParent[null] || []).map(p => <TreeNode key={p.id} p={p} depth={0} ctx={treeCtx} />)}
              {projectsState === 'ready' && !proj.projects.length && <div className="muted" style={{ padding: 10, fontSize: 12 }}>No projects yet. Create one to organize runs.</div>}
            </nav>
          </div>
        </aside>

        <div className={'runlist' + (view === 'map' ? ' map-list-shell' : '')}>
          <div className="crumbs">
            <button type="button" className="crumb" disabled={navigationBusy} onClick={() => chooseProject(ALL)}>All runs</button>
            {breadcrumb.map(p => <React.Fragment key={p.id}><span className="sep">/</span>
              <button type="button" className="crumb" disabled={navigationBusy} onClick={() => chooseProject(p.id)}>{p.name}</button></React.Fragment>)}
            {sel === UNASSIGNED && <><span className="sep">/</span><span className="crumb">Unassigned</span></>}
            <span style={{ flex: 1 }} />
            {scope && <div className="view-toggle crumb-report">
              <button className={'vt report' + (showReport ? ' on' : '')} disabled={navigationBusy} title={`cross-run report for ${scope.label}`}
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
            <button className="btn sm ghost" disabled={navigationBusy} aria-label="Create or manage super-tasks"
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
                    aria-label={`Sort ${sortKey === 'metric' ? (sortDir === 'asc' ? 'best first' : 'worst first') : (sortDir === 'asc' ? 'ascending' : 'descending')}`}
                    title={sortKey === 'metric' ? (sortDir === 'asc' ? 'best first' : 'worst first') : (sortDir === 'asc' ? 'ascending' : 'descending')}
                    onClick={() => setSortDir(d => d === 'asc' ? 'desc' : 'asc')}>
              {sortKey === 'metric' ? (sortDir === 'asc' ? 'best' : 'worst') : (sortDir === 'asc' ? '↑' : '↓')}
            </button>
          </div>}
          <ResourceNotice state={runsState} label="Runs" retry={loadRuns} />
          <ResourceNotice state={projectsState} label="Projects" retry={loadProjects} />
          {!stModal && <ResourceNotice state={superState} label="Super-tasks" retry={loadSupers} />}
          {(!compactNav || !projectsOpen) && mutationNotice}
          {runsState === 'ready' && runs && !runsOf(sel).length && <div className="notice resource-empty">No runs here.
            {sel === ALL
              ? <button className="btn sm primary" disabled={navigationBusy} onClick={() => window.dispatchEvent(new CustomEvent('ll:new-run'))}>Start a new run</button>
              : <span>Drag a run onto this project, or use its <b>Move</b> menu.</span>}</div>}
          {runs && !!runsOf(sel).length && !visible.length && <div className="notice" role="status">
            {runsState === 'stale' ? 'No runs in the last loaded data match the filters.' : 'No runs match the filters.'}
            {hasActiveFilters && <button type="button" className="btn sm" disabled={navigationBusy} onClick={clearFilters}>Clear filters</button>}
          </div>}
          {view === 'map' && ['ready', 'stale'].includes(projectsState) && runs && visible.length > 0 && <div className="map-stage">
            <LazyBoundary label="run map" resetKey={`map:${sel}`}>
              <MapView onOpen={id => { if (!navigationBusy) onOpen(id) }} runs={visible} projects={proj.projects}
                collapsed={mapCollapsed} onToggle={toggleMapCluster}
                scopeLabel={scope?.label || (sel === ALL ? 'All runs' : sel === UNASSIGNED ? 'Unassigned' : (projName[sel] || sel))} />
            </LazyBoundary>
          </div>}
          {view === 'list' && runs && visible.map(r => (
            <div className="run-card" key={r.run_id} draggable={!navigationBusy}
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
                   aria-disabled={navigationBusy || undefined}
                   onClick={event => {
                     if (navigationBusy) { event.preventDefault(); return }
                     followClientRoute(event, () => onOpen(r.run_id))
                   }}
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
                <button className="ic dots" disabled={navigationBusy} aria-label={`Actions for run ${r.label || r.run_id}`}
                        aria-haspopup="menu" aria-expanded={runMenu === r.run_id}
                        onClick={e => {
                          e.stopPropagation()
                          if (runMenu === r.run_id) closeRunMenu(false)
                          else { runMenuTriggerRef.current = e.currentTarget; setRunMenu(r.run_id) }
                        }}>⋮</button>
                {runMenu === r.run_id && <RunMenu r={r} projects={proj.projects} supertasks={superdata.supertasks}
                  onOpen={onOpen} onMove={moveRun} onSetSuper={assignToSuper} onManageSupers={() => openSuperTasks(runMenuTriggerRef.current)}
                  onRename={openRunRename} onDelete={removeRun} onClose={closeRunMenu} onBusyChange={setMenuBusy} />}
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

      {stModal && <SuperTaskModal supertasks={superdata.supertasks} state={superState} onRetry={loadSupers}
        onCreate={createSuper} onRename={renameSuper} onDelete={removeSuper}
        onClose={closeSuperTasks} />}

      {showReport && scope && <LazyBoundary label="scope report" mode="overlay" resetKey={scope.label}>
        <ScopeReport scope={scope}
          onOpen={(id) => { setShowReport(false); onOpen(id) }} onClose={() => setShowReport(false)} />
      </LazyBoundary>}
    </main>
  )
}
