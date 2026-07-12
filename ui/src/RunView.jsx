import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useMediaQuery, useRunState } from './hooks.js'
import { get, fmt, fmtInt, phaseLabel, workingId, CONTROL, resumeRun } from './util.js'
import Dag from './Dag.jsx'
import Inspector, { GroupSummary } from './Inspector.jsx'
import Dock from './Dock.jsx'
import { computeGroups, autoCollapseSet } from './grouping.js'
import ReportView from './Report.jsx'
import DirectionsOverview from './DirectionsOverview.jsx'
import EnergyToggle from './EnergyToggle.jsx'
import { OpIcon } from './icons.jsx'
import { useDialogFocus } from './useDialogFocus.js'
import {
  clearRunAccess, liveHistory, reconcileHistoricalSelection, rejectHistory,
  requestHistory, resolveHistory, setRunAccess,
} from './runMode.js'
import {
  TrustPanel, SensitivityPanel, FailuresPanel, ParetoPanel, DataQualityPanel,
  ConfigPanel, AuthoringPanel, MemoryPanel, RegistryPanel, EventExplorer,
  ComparePanel, GpuPanel, HyperImportancePanel, CrossRunPanel, CollabPanel, OverviewPanel, ResearchPanel,
  ArtifactsPanel, HypothesisBoard, QueuePanel, WhyStrip,
} from './panels.jsx'

// The panel bar, grouped by importance then process order (Report is the [Search|Report] toggle, and
// the deep-research/policy/strategist "why" cards now live in the chat — so those panels are gone).
//   rigor → analysis → data/lab → ops
// Run-view panel IA (design audit 2026-06-28): keep the few most-used panels inline; fold the long
// tail into a grouped "More ▾" overflow so the bar never wraps to a second row (was 17 buttons).
// Panel IA consolidated into 4 hubs (was 7 inline + a 12-item "More" overflow). Each hub is a
// dropdown of related panels; the hub button lights when its open panel is active. Report + Overview
// stay in the view-toggle above; Settings stays a dedicated button — both are one-click essentials.
const HUBS = [
  ['Progress', [['queue', 'Queue'], ['hypotheses', 'Hypotheses'], ['research', 'Research'], ['failures', 'Failures']]],
  ['Trust', [['trust', 'Trust'], ['pareto', 'Pareto / diversity'], ['data', 'Data quality']]],
  ['Analysis', [['compare', 'Compare'], ['sensitivity', 'Sensitivity'], ['importance', 'Importance'], ['crossrun', 'Cross-run']]],
  ['Lab', [['artifacts', 'Artifacts'], ['registry', 'Registry'], ['memory', 'Memory'], ['collab', 'Collab'], ['authoring', 'Authoring'], ['events', 'Events'], ['gpu', 'GPU']]],
]
const HUB_OF = Object.fromEntries(HUBS.flatMap(([label, items]) => items.map(([k]) => [k, label])))

export default function RunView({ runId, onBack }) {
  const { live, seq, connected, status: runStatus, error: runError, retry: retryRun } = useRunState(runId)
  const compactWorkspace = useMediaQuery('(max-width: 900px)')
  const [viewSeq, setViewSeq] = useState(null)
  const [history, setHistory] = useState(liveHistory)
  const [historyRetry, setHistoryRetry] = useState(0)
  const historyActive = viewSeq != null
  const changeViewSeq = (next) => {
    const n = next == null || Number(next) >= seq ? null : Number(next)
    setRunAccess(runId, { readOnly: n != null, seq: n })
    setViewSeq(n)
  }
  useEffect(() => {
    // A fresh route always starts live. This also heals any stale client registry entry left by an
    // interrupted historical scrub before the previous workspace could finish unmounting.
    setRunAccess(runId, { readOnly: false, seq: null })
    return () => clearRunAccess(runId)
  }, [runId])
  const [selectedId, setSelectedId] = useState(null)
  const [inspectTab, setInspectTab] = useState('Overview')
  const [groupMode, setGroupMode] = useState('theme')
  const [collapsed, setCollapsed] = useState(() => new Set())
  const [selectedGroup, setSelectedGroup] = useState(null)
  const [panel, setPanel] = useState(null)
  const panelReturnFocusRef = useRef(null)
  const hubTriggerRef = useRef(null)
  const closePanel = () => {
    setPanel(null)
    requestAnimationFrame(() => {
      const target = panelReturnFocusRef.current
      if (target && document.contains(target)) target.focus()
      panelReturnFocusRef.current = null
    })
  }
  const [openHub, setOpenHub] = useState(null)               // which panel-hub dropdown is open
  const [view, setView] = useState('dag')                    // 'dag' | 'report' — primary destination
  const [themeFilter, setThemeFilter] = useState(null)       // E1: drill the tree to one direction
  const landedRef = useRef(false)                            // auto-land on Report once, on finish
  // Resizable / collapsible panes (standard multi-pane layout), persisted across sessions.
  const [sideW, setSideW] = useState(() => +localStorage.getItem('ll.sideW') || 420)
  const [dockH, setDockH] = useState(() => +localStorage.getItem('ll.dockH') || 230)
  const [sideC, setSideC] = useState(() => localStorage.getItem('ll.sideC') === '1')
  const [dockC, setDockC] = useState(() => localStorage.getItem('ll.dockC') === '1')
  // Desktop panes keep their persisted layout. On tablet/mobile they become temporary surfaces so
  // neither Inspector nor Timeline permanently taxes the graph. Compact state is intentionally not
  // persisted: every narrow-screen visit starts with the canvas unobstructed.
  const [compactInspectorOpen, setCompactInspectorOpen] = useState(false)
  const [compactTimelineOpen, setCompactTimelineOpen] = useState(false)
  const compactInspectorCloseRef = useRef(null)
  const compactInspectorRef = useRef(null)
  useDialogFocus(compactInspectorRef, () => setCompactInspectorOpen(false), compactWorkspace && compactInspectorOpen)
  useEffect(() => {
    localStorage.setItem('ll.sideW', sideW); localStorage.setItem('ll.dockH', dockH)
    localStorage.setItem('ll.sideC', sideC ? '1' : '0'); localStorage.setItem('ll.dockC', dockC ? '1' : '0')
  }, [sideW, dockH, sideC, dockC])
  const clampSide = (value) => Math.max(280, Math.min(Math.max(280, window.innerWidth - 486), value))
  const clampDock = (value) => Math.max(90, Math.min(Math.max(140, window.innerHeight - 470), value))
  useEffect(() => {
    const clampPanes = () => { setSideW(w => clampSide(w)); setDockH(h => clampDock(h)) }
    clampPanes()
    window.addEventListener('resize', clampPanes)
    return () => window.removeEventListener('resize', clampPanes)
  }, [])
  // Drag a splitter: the side panel grows when dragged left, the dock when dragged up.
  const startDrag = (axis) => (e) => {
    e.preventDefault()
    const x0 = e.clientX, y0 = e.clientY, w0 = sideW, h0 = dockH
    const onMove = (ev) => {
      if (axis === 'side') setSideW(clampSide(w0 - (ev.clientX - x0)))
      else setDockH(clampDock(h0 - (ev.clientY - y0)))
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp)
      document.body.style.userSelect = ''; dragCleanupRef.current = null
    }
    dragCleanupRef.current = onUp   // unmount mid-drag must also detach the window listeners
    window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', onUp)
    document.body.style.userSelect = 'none'
  }
  const resizeWithKeys = (axis) => (e) => {
    const sideDelta = e.key === 'ArrowLeft' ? 20 : e.key === 'ArrowRight' ? -20 : 0
    const dockDelta = e.key === 'ArrowUp' ? 20 : e.key === 'ArrowDown' ? -20 : 0
    const delta = axis === 'side' ? sideDelta : dockDelta
    if (!delta) return
    e.preventDefault()
    if (axis === 'side') setSideW(w => clampSide(w + delta))
    else setDockH(h => clampDock(h + delta))
  }
  const dragCleanupRef = useRef(null)
  useEffect(() => () => dragCleanupRef.current?.(), [])
  const [toast, setToast] = useState(null)
  const [cfg, setCfg] = useState(null)
  useEffect(() => { get(`/api/runs/${runId}/config`).then(setCfg).catch(() => {}) }, [runId])
  // Auto-land on the Report once, when a live run finishes — the conclusion is what the user wants
  // at that moment. Guarded so a manual switch back to the DAG sticks. Live runs default to 'dag'.
  useEffect(() => {
    if (!historyActive && live?.finished && !landedRef.current) { landedRef.current = true; setView('report') }
  }, [live?.finished, historyActive])
  useEffect(() => {
    if (viewSeq == null) { setHistory(liveHistory()); return }
    let alive = true
    const want = Number(viewSeq)
    setHistory(requestHistory(want))
    get(`/api/runs/${runId}/state?seq=${want}`)
      .then(p => { if (alive) setHistory(current => resolveHistory(current, want, p)) })
      .catch(e => { if (alive) setHistory(current => rejectHistory(current, want, e)) })
    return () => { alive = false }
  }, [viewSeq, runId, historyRetry])
  const hist = history.status === 'ready' && history.requestedSeq === viewSeq ? history.data : null
  const toastTimer = useRef(null)
  const showToast = (m) => {
    // Clear the previous timer so a second toast doesn't get hidden early by the first one's timeout.
    if (toastTimer.current) clearTimeout(toastTimer.current)
    setToast(m)
    toastTimer.current = setTimeout(() => setToast(null), 2200)
  }
  // Members of the selected group — memoized so unrelated re-renders (toast, live ticks) don't
  // re-walk all nodes; only recomputes when the node set / mode / selection actually changes.
  const groupMembers = useMemo(() => {
    const ns = (hist || live)?.nodes
    return (selectedGroup != null && ns) ? (computeGroups(ns, groupMode).get(selectedGroup) || []) : []
  }, [hist, live, groupMode, selectedGroup])
  // Node selection clears any group selection (the side panel shows one or the other).
  const selectNode = (id) => { setSelectedId(id); if (id != null) setSelectedGroup(null) }
  // U3 · canvas as a control surface: right-click actions + drag-to-merge + a "merge with…" arm mode.
  const [mergeFrom, setMergeFrom] = useState(null)   // arm: next node click merges with this one
  const [comparePair, setComparePair] = useState(null)   // seed ComparePanel for "diff vs champion"
  useEffect(() => {
    if (!historyActive) return
    setMergeFrom(null)
    setComparePair(null)
    setPanel(null)
    setOpenHub(null)
    setSelectedGroup(null)
    setInspectTab('Overview')
    if (history.status === 'ready') {
      setSelectedId(id => {
        const next = reconcileHistoricalSelection(id, history.data)
        if (id != null && next == null) setCompactInspectorOpen(false)
        return next
      })
    }
  }, [historyActive, history.status, history.resolvedSeq])
  const live2 = hist || live
  const pendingDescendants = (rootId) => {   // for "kill branch": abort the node + its PENDING subtree
    const ns = live2?.nodes || {}
    const kids = {}; Object.values(ns).forEach(n => (n.parent_ids || []).forEach(p => { (kids[p] ||= []).push(n.id) }))
    const out = []; const stack = [rootId]; const seen = new Set()
    while (stack.length) { const x = stack.pop(); if (seen.has(x)) continue; seen.add(x)
      if (ns[x] && ns[x].status === 'pending') out.push(x)
      ;(kids[x] || []).forEach(k => stack.push(k)) }
    return out
  }
  // fork/ablate/merge only append an INTENT to the event log — a dead engine (finished/idle run)
  // never reads it, so the button silently "does nothing". Mirror the chat's apply path: reopen the
  // run + re-enter the loop so the engine actually processes the action (same NEEDS_RESUME logic).
  const kickEngine = async (label) => {
    const engineDead = !!live2?.finished || live?.engine_running === false
    if (!engineDead) return
    await CONTROL.reopen(runId)
    await resumeRun(runId)
    showToast(`${label} — run reopened, engine resuming`)
  }
  const onNodeAction = async (action, arg) => {
    if (historyActive) { showToast(`Historical snapshot seq ${viewSeq} is read-only`); return }
    try {
      if (action === 'merge' && arg && typeof arg === 'object') {   // drag drop A->B
        await CONTROL.merge(runId, [arg.from, arg.to]); await kickEngine(`merging #${arg.from} + #${arg.to}`)
        showToast(`merging #${arg.from} + #${arg.to}`); return
      }
      const id = arg
      if (action === 'explore') { await CONTROL.fork(runId, id); await kickEngine(`exploring from #${id}`); showToast(`exploring from #${id}`) }
      else if (action === 'ablate') { await CONTROL.forceAblate(runId, id); await kickEngine(`ablating #${id}`); showToast(`ablating #${id}`) }
      else if (action === 'inspect') {
        selectNode(id)
        if (compactWorkspace) setCompactInspectorOpen(true)
        else setSideC(false)
      }   // un-collapse: with the side
      // panel folded (sideC persists in localStorage!) selecting alone changes nothing visible
      else if (action === 'diff') { setComparePair([live2?.best_node_id ?? id, id]); setPanel('compare') }
      else if (action === 'merge') { setMergeFrom(id); showToast(`click a node to merge with #${id}`) }
      else if (action.startsWith('reset:')) {   // re-run this node IN PLACE from a stage (no new node)
        const stage = action.split(':')[1]
        await CONTROL.resetNode(runId, id, stage)
        await kickEngine(`re-running #${id} from ${stage}`)
        showToast(`#${id}: re-running from ${stage} — applied on resume`)
      }
      else if (action === 'kill') {
        const ids = pendingDescendants(id)
        if (!ids.length) { showToast(`#${id}: nothing pending to kill (already evaluated)`); return }
        for (const k of ids) await CONTROL.nodeAbort(runId, k)
        showToast(`killed ${ids.length} pending experiment(s) under #${id}`)
      }
    } catch (e) { showToast(`action failed: ${e.message || 'run not live?'}`) }
  }
  // When armed for merge, a node click completes the merge instead of selecting.
  const onCanvasSelect = (id) => {
    if (historyActive && mergeFrom != null) { setMergeFrom(null); return }
    if (mergeFrom != null && id != null && id !== mergeFrom) {
      CONTROL.merge(runId, [mergeFrom, id])
        .then(() => kickEngine(`merging #${mergeFrom} + #${id}`))
        .then(() => showToast(`merging #${mergeFrom} + #${id}`))
        .catch(() => showToast('merge failed'))
      setMergeFrom(null); return
    }
    if (mergeFrom != null) setMergeFrom(null)
    selectNode(id)
    if (compactWorkspace && id != null) setCompactInspectorOpen(true)
  }
  useEffect(() => {   // Esc cancels the merge-arm
    if (mergeFrom == null) return
    const h = (e) => { if (e.key === 'Escape') setMergeFrom(null) }
    window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h)
  }, [mergeFrom])
  // Drill-down from the dock/timeline: select a node, optionally open a tab + jump the scrubber.
  const focusNode = (id, tab, eventSeq) => {
    selectNode(id)
    if (compactWorkspace) setCompactInspectorOpen(true)
    else setSideC(false)     // drill-down means "show me the inspector" — un-fold a collapsed panel
    if (tab) setInspectTab(tab)
    if (eventSeq != null) changeViewSeq(eventSeq)
  }
  // --- semantic-zoom grouping controls ---
  const toggleGroup = (key) => setCollapsed(s => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n })
  const changeMode = (m) => { setGroupMode(m); setCollapsed(new Set()); setSelectedGroup(null) }
  const selectGroup = (key) => {
    setSelectedGroup(key)
    if (key != null) {
      setSelectedId(null)
      if (compactWorkspace) setCompactInspectorOpen(true)
    }
  }
  // Phase 0: auto-collapse settled groups into the existing `collapsed` Set (one-shot fill, not a live
  // policy — so it never fights the user's manual toggles). Keeps the champion/selected/working groups
  // open. Wired to the "⊟ settled" button and fired once when a run finishes (only if nothing's been
  // collapsed yet, so it never clobbers a layout the user arranged by hand).
  const autoCollapse = () => {
    const st = hist || live; const ns = st?.nodes; if (!ns) return
    setCollapsed(autoCollapseSet(ns, computeGroups(ns, groupMode),
      { mode: groupMode, bestId: st.best_node_id, selectedId, workId: workingId(st) }))
  }
  const autoCollapsedRef = useRef(false)
  // groupMode is in the deps so that if the run finishes in a non-banded mode (operator/metric/none),
  // a later switch to theme/niche still triggers the one-shot fold (the ref keeps it firing only once).
  useEffect(() => {
    if (!historyActive && live?.finished && !autoCollapsedRef.current && (groupMode === 'theme' || groupMode === 'niche') && collapsed.size === 0) {
      autoCollapsedRef.current = true; autoCollapse()
    }
  }, [live?.finished, groupMode, historyActive])

  if (!live) return <div className="app">
    <div className="topbar run-head">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button className="btn sm ghost" onClick={onBack}>← runs</button>
    </div>
    <main className="run-resource-state" aria-live="polite">
      {runStatus === 'not_found' ? <>
        <div className="resource-state-icon" aria-hidden="true">404</div>
        <h1>Run not found</h1>
        <p><code>{runId}</code> does not exist or may have been removed.</p>
        <div className="resource-state-actions"><button className="btn primary" onClick={onBack}>Back to runs</button><button className="btn" onClick={retryRun}>Retry</button></div>
      </> : runStatus === 'error' ? <>
        <div className="resource-state-icon" aria-hidden="true">!</div>
        <h1>Could not load run</h1>
        <p>{runError || 'Check that the LoopLab server is reachable.'}</p>
        <div className="resource-state-actions"><button className="btn primary" onClick={retryRun}>Retry</button><button className="btn" onClick={onBack}>Back to runs</button></div>
      </> : <>
        <div className="history-spinner" aria-hidden="true" />
        <h1>Opening run…</h1><p>Loading the latest search state.</p>
      </>}
    </main>
  </div>
  // Liveness reflects the ACTUAL run, not the viewed snapshot: green+breathing only while a
  // connected run is still going; a finished run shows a calm "finished", a dropped SSE "offline".
  // A ZOMBIE (not finished, but no engine holds the lock) gets its own "stalled" badge — otherwise it
  // would falsely breathe green "live" while nothing actually runs.
  const intentionallyPaused = !!live.paused || live.phase === 'paused'
  const awaitingApproval = live.phase === 'approval' || live.phase === 'spec_approval'
  const stalled = live.engine_running === false && !live.finished && !intentionallyPaused && !awaitingApproval
  const liveStatus = !connected ? 'off' : (live.finished ? 'done' : stalled ? 'stalled' : (intentionallyPaused || awaitingApproval) ? 'paused' : 'on')
  const liveLabel = !connected ? 'offline' : (live.finished ? 'finished' : stalled ? 'stalled' : intentionallyPaused ? 'paused' : awaitingApproval ? 'approval needed' : 'live')
  if (historyActive && !hist) return <div className="app">
    <div className="topbar run-head">
      <span className="brand"><span className="dot">◉</span> LoopLab</span>
      <button className="btn sm ghost" onClick={onBack}>← runs</button>
      <span className="spacer" />
      <span className={'live ' + liveStatus}><span className="led" />current run: {liveLabel}</span>
    </div>
    <div className="history-banner" role="status">
      <span className="history-lock" aria-hidden="true">◷</span>
      <b>Historical snapshot · seq {viewSeq} of {seq}</b>
      <span>read-only</span>
      <button className="btn sm primary" onClick={() => changeViewSeq(null)}>Return to live</button>
    </div>
    <main className="history-resource">
      {history.status === 'error'
        ? <><h2>Snapshot unavailable</h2><p>{history.error}</p>
            <button className="btn" onClick={() => setHistoryRetry(n => n + 1)}>Retry</button></>
        : <><div className="history-spinner" aria-hidden="true" /><h2>Loading snapshot seq {viewSeq}…</h2>
            <p>The live workspace is hidden until this exact historical state resolves.</p></>}
    </main>
  </div>
  const state = historyActive ? hist : live
  const evalSec = state.total_eval_seconds || 0
  const maxEval = cfg?.max_eval_seconds
  const cost = state.llm_cost
  const hasInspectorContext = selectedId != null || selectedGroup != null
  const showInspector = compactWorkspace ? (compactInspectorOpen && hasInspectorContext) : (!sideC && hasInspectorContext)
  const timelineCollapsed = compactWorkspace ? !compactTimelineOpen : dockC


  return (
    <div className="app">
      <div className="topbar run-head">
        <span className="brand"><span className="dot">◉</span> LoopLab</span>
        <button className="btn sm ghost" onClick={onBack}>← runs</button>
        <div className="view-toggle" role="tablist">
          <button className={'vt' + (view === 'dag' ? ' on' : '')} onClick={() => setView('dag')}>Search</button>
          <button className={'vt report' + (view === 'report' ? ' on' : '')} onClick={() => setView('report')}
            title="conclusion-first run report"><OpIcon name="doc" size={12} /> Report</button>
          <button className={'vt' + (panel === 'overview' ? ' on' : '')} disabled={historyActive}
            onClick={event => {
              if (panel === 'overview') closePanel()
              else { panelReturnFocusRef.current = event.currentTarget; setPanel('overview') }
            }}
            title="at-a-glance run summary — best metric, budget, strategy, hints">Overview</button>
        </div>
        <EnergyToggle />
        <span className="pill phase">{phaseLabel(state)}</span>
        <span className="muted" style={{ maxWidth: 280, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }} title={state.goal}>{state.goal || state.task_id}</span>
        <span className={'live ' + liveStatus}><span className="led" />{liveLabel}{historyActive && ' · history'}</span>
        <span className="spacer" />
        {/* round-8: keep only COMPACT at-a-glance metrics here (eval, tokens) + alerts; the fuller
            set (strategy + rationale, hints, dedup) moved to the Overview tab so this header stays a
            SINGLE non-wrapping line. Clicking a metric opens Overview for the detail. */}
        {evalSec > 0 && <span className="chip" title={historyActive ? 'Historical mode — return live to open Overview' : 'eval time — open Overview for the budget bar'}
          onClick={event => { if (!historyActive) { panelReturnFocusRef.current = event.currentTarget; setPanel('overview') } }} style={{ cursor: historyActive ? 'default' : 'pointer' }}>
          <span className="k">eval</span> {fmt(evalSec, 1)}s{maxEval ? ` / ${maxEval}` : ''}</span>}
        {cost && <span className="chip" title={historyActive ? 'Historical mode — return live to open Overview' : 'tokens — open Overview'}
          onClick={event => { if (!historyActive) { panelReturnFocusRef.current = event.currentTarget; setPanel('overview') } }} style={{ cursor: historyActive ? 'default' : 'pointer' }}>
          <span className="k">tokens</span> {fmtInt(cost.total_tokens)}</span>}
        {state.reward_hacks?.length > 0 && <span className="chip alarm" title={historyActive ? 'Historical mode — return live to open Trust' : 'suspicious wins flagged (B5) — open Trust'}
          onClick={event => { if (!historyActive) { panelReturnFocusRef.current = event.currentTarget; setPanel('trust') } }} style={{ cursor: historyActive ? 'default' : 'pointer' }}>
          <span className="k"><OpIcon name="alert" size={11} /> hack?</span> {state.reward_hacks.length}</span>}
        {live.paused && <span className="chip warn"><OpIcon name="pause" size={11} /> paused</span>}
      </div>

      {historyActive && <div className="history-banner" role="status">
        <span className="history-lock" aria-hidden="true">◷</span>
        <b>Historical snapshot · seq {history.resolvedSeq} of {seq}</b>
        <span>read-only · actions target live and are disabled</span>
        <button className="btn sm primary" onClick={() => changeViewSeq(null)}>Return to live</button>
      </div>}

      {/* All actions now run through the chat (type a /command or just say what to do). The approval
          phase shows an inline reminder of the exact command to type. */}
      {!historyActive && (live.phase === 'approval' || live.phase === 'spec_approval') &&
        <div className="topbar" style={{ background: 'rgba(74,163,255,.12)', borderBottom: '1px solid var(--accent-dim)' }}>
          <b>{live.phase === 'approval'
            ? `Human approval required for the final best — type `
            : 'Eval spec needs ratification — type '}</b>
          <code className="cmd-hint">{live.phase === 'approval' ? `/approve #${live.best_node_id}` : '/ratify'}</code>
          <b>&nbsp;in the chat below.</b>
        </div>}

      <div className="topbar panel-bar" style={{ minHeight: 38, paddingTop: 4, paddingBottom: 4 }}>
        <div className="menu">
          {/* 4 consolidated hubs — each a dropdown of related panels; the hub lights when its open
              panel is active. Report/Overview live in the view-toggle above; Settings is dedicated. */}
          {HUBS.map(([label, items]) => <div className="more-wrap" key={label}>
            <button className={'btn sm ghost' + (HUB_OF[panel] === label ? ' on' : '')}
                    disabled={historyActive}
                    onClick={event => { hubTriggerRef.current = event.currentTarget; setOpenHub(o => o === label ? null : label) }}>{label} ▾</button>
            {openHub === label && <>
              <div className="menu-backdrop" onClick={() => setOpenHub(null)} />
              <div className="run-menu more-menu" onClick={e => e.stopPropagation()}>
                {items.map(([k, l]) => <button key={k} className={'mi' + (panel === k ? ' on' : '')}
                  onClick={() => { panelReturnFocusRef.current = hubTriggerRef.current; setOpenHub(null); setPanel(k) }}>{l}</button>)}
              </div>
            </>}
          </div>)}
          <span className="panel-sep" />
          <button className={'btn sm ghost' + (panel === 'config' ? ' on' : '')}
                  disabled={historyActive}
                  onClick={event => { setOpenHub(null); panelReturnFocusRef.current = event.currentTarget; setPanel('config') }}>Settings</button>
        </div>
      </div>

      {view === 'report'
        ? <div className="main"><div className="report-scroll">
            <ReportView state={state} runId={runId} onToast={showToast}
              readOnly={historyActive} historySeq={history.resolvedSeq}
              onOpenPanel={historyActive ? null : (p) => setPanel(p)}
              onPickNode={(id) => { setView('dag'); selectNode(id) }} /></div></div>
        : <>
      <DirectionsOverview state={state} active={themeFilter} onPick={setThemeFilter} />
      <WhyStrip state={state} onSelect={selectNode} />
      <div className={'main run-workspace' + (compactWorkspace ? ' compact' : '')}>
        <div className="canvas-wrap"><Dag state={state} selectedId={selectedId} onSelect={onCanvasSelect}
          groupMode={groupMode} collapsed={collapsed} onToggleGroup={toggleGroup} onSetMode={changeMode}
          onCollapseAll={(keys) => setCollapsed(new Set(keys))} onExpandAll={() => setCollapsed(new Set())}
          onAutoCollapse={autoCollapse} onNodeAction={historyActive ? null : onNodeAction}
          mergeArm={historyActive ? null : mergeFrom}
          selectedGroup={selectedGroup} onSelectGroup={selectGroup} themeFilter={themeFilter} /></div>
        {compactWorkspace && !showInspector && hasInspectorContext &&
          <button className="workspace-pane-toggle" onClick={() => setCompactInspectorOpen(true)}
                  aria-label={`Open ${selectedGroup != null ? 'group' : 'inspector'} panel`}>
            {selectedGroup != null ? 'Group' : `Inspector · #${selectedId}`}
          </button>}
        {compactWorkspace && showInspector &&
          <button className="workspace-scrim" onClick={() => setCompactInspectorOpen(false)}
                  aria-label="Close inspector panel" />}
        {!compactWorkspace && hasInspectorContext && !showInspector
          ? <button className="side-rail" title="show panel" onClick={() => setSideC(false)}>‹ {selectedGroup != null ? 'group' : 'inspector'}</button>
          : showInspector && <>
              {!compactWorkspace && <div className="splitter v" onPointerDown={startDrag('side')} onKeyDown={resizeWithKeys('side')}
                role="separator" tabIndex={0} aria-orientation="vertical" aria-label="Resize inspector"
                aria-valuemin={280} aria-valuemax={Math.max(280, window.innerWidth - 486)} aria-valuenow={Math.round(sideW)} title="Drag or use arrow keys to resize" />}
              <aside className={'side' + (compactWorkspace ? ' compact-drawer' : '')} style={{ width: sideW }}
                     ref={compactInspectorRef} tabIndex={compactWorkspace ? -1 : undefined}
                     aria-label={selectedGroup != null ? 'Group details' : 'Experiment inspector'}
                     role={compactWorkspace ? 'dialog' : 'complementary'}
                     aria-modal={compactWorkspace ? 'true' : undefined}>
                <div className="pane-grip">
                  <span className="muted">{selectedGroup != null ? 'group' : 'inspector'}</span>
                  <span className="spacer" style={{ flex: 1 }} />
                  <button ref={compactInspectorCloseRef} className="btn sm ghost" title={compactWorkspace ? 'close panel' : 'collapse panel'}
                          onClick={() => compactWorkspace ? setCompactInspectorOpen(false) : setSideC(true)}>⟩</button>
                </div>
                {selectedGroup != null
                  ? <GroupSummary groupKey={selectedGroup} memberIds={groupMembers}
                      state={state} onSelectNode={focusNode} onClose={() => { setSelectedGroup(null); setCompactInspectorOpen(false) }} />
                  : <Inspector runId={runId} nodeId={selectedId} state={state} live={live}
                      tab={inspectTab} setTab={setInspectTab} onToast={showToast}
                      readOnly={historyActive} historySeq={history.resolvedSeq} />}
              </aside>
            </>}
      </div>

      {!compactWorkspace && !dockC && <div className="splitter h" onPointerDown={startDrag('dock')} onKeyDown={resizeWithKeys('dock')}
        role="separator" tabIndex={0} aria-orientation="horizontal" aria-label="Resize timeline"
        aria-valuemin={90} aria-valuemax={Math.max(140, window.innerHeight - 470)} aria-valuenow={Math.round(dockH)} title="Drag or use arrow keys to resize" />}
      <Dock runId={runId} live={live} liveSeq={seq} viewSeq={viewSeq} setViewSeq={changeViewSeq} onFocus={focusNode}
            onToast={showToast}
            readOnly={historyActive}
            collapsed={timelineCollapsed}
            onToggleCollapse={() => compactWorkspace ? setCompactTimelineOpen(v => !v) : setDockC(c => !c)}
            height={dockH} />
        </>}

      {panel === 'overview' && <OverviewPanel state={state} maxEval={maxEval} onClose={closePanel} onOpenPanel={setPanel} />}
      {panel === 'research' && <ResearchPanel state={state} runId={runId} onToast={showToast} onClose={closePanel} />}
      {panel === 'trust' && <TrustPanel state={state} runId={runId} onSelect={selectNode} onToast={showToast} onClose={closePanel} />}
      {panel === 'queue' && <QueuePanel state={state} runId={runId} onSelect={selectNode} onToast={showToast} onClose={closePanel} />}
      {panel === 'hypotheses' && <HypothesisBoard state={state} runId={runId} onSelect={selectNode} onToast={showToast} onClose={closePanel} />}
      {panel === 'sensitivity' && <SensitivityPanel state={state} onSelect={selectNode} onClose={closePanel} />}
      {panel === 'importance' && <HyperImportancePanel state={state} onClose={closePanel} />}
      {panel === 'failures' && <FailuresPanel state={state} onSelect={selectNode} onClose={closePanel} />}
      {panel === 'pareto' && <ParetoPanel state={state} onSelect={selectNode} onClose={closePanel} />}
      {panel === 'data' && <DataQualityPanel state={state} onClose={closePanel} />}
      {panel === 'compare' && <ComparePanel state={state} runId={runId} initialPair={comparePair}
        onClose={() => { closePanel(); setComparePair(null) }} />}
      {panel === 'crossrun' && <CrossRunPanel state={state} onClose={closePanel} />}
      {panel === 'collab' && <CollabPanel state={state} runId={runId} onSelect={selectNode} onToast={showToast} onClose={closePanel} />}
      {panel === 'config' && <ConfigPanel runId={runId} state={state} live={live} onToast={showToast} onClose={closePanel} />}
      {panel === 'authoring' && <AuthoringPanel onToast={showToast} onClose={closePanel} />}
      {panel === 'memory' && <MemoryPanel onClose={closePanel} />}
      {panel === 'registry' && <RegistryPanel state={state} onClose={closePanel} />}
      {panel === 'gpu' && <GpuPanel onClose={closePanel} />}
      {panel === 'events' && <EventExplorer runId={runId} onClose={closePanel} />}
      {panel === 'artifacts' && <ArtifactsPanel runId={runId} onToast={showToast} onClose={closePanel} />}

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
