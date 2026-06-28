import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useRunState } from './hooks.js'
import { get, fmt, fmtInt, phaseLabel, workingId } from './util.js'
import Dag from './Dag.jsx'
import Inspector, { GroupSummary } from './Inspector.jsx'
import Dock from './Dock.jsx'
import { computeGroups, autoCollapseSet } from './grouping.js'
import ReportView from './Report.jsx'
import DirectionsOverview from './DirectionsOverview.jsx'
import {
  TrustPanel, SensitivityPanel, FailuresPanel, ParetoPanel, DataQualityPanel,
  ConfigPanel, AuthoringPanel, MemoryPanel, RegistryPanel, EventExplorer,
  ComparePanel, GpuPanel, HyperImportancePanel, CrossRunPanel, CollabPanel, OverviewPanel, ResearchPanel,
} from './panels.jsx'

// The panel bar, grouped by importance then process order (Report is the [Search|Report] toggle, and
// the deep-research/policy/strategist "why" cards now live in the chat — so those panels are gone).
//   rigor → analysis → data/lab → ops
const PANEL_GROUPS = [
  [['trust', 'Trust'], ['failures', 'Failures']],
  [['research', 'Research'], ['sensitivity', 'Sensitivity'], ['importance', 'Importance'], ['pareto', 'Pareto/Div'], ['compare', 'Compare']],
  [['data', 'Data'], ['crossrun', 'Cross-run'], ['registry', 'Registry']],
  [['config', 'Config'], ['gpu', 'GPU'], ['memory', 'Memory'], ['events', 'Events'], ['collab', 'Collab'], ['authoring', 'Authoring']],
]

export default function RunView({ runId, onBack }) {
  const { live, seq, connected } = useRunState(runId)
  const [viewSeq, setViewSeq] = useState(null)
  const [hist, setHist] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [inspectTab, setInspectTab] = useState('Overview')
  const [groupMode, setGroupMode] = useState('theme')
  const [collapsed, setCollapsed] = useState(() => new Set())
  const [selectedGroup, setSelectedGroup] = useState(null)
  const [panel, setPanel] = useState(null)
  const [view, setView] = useState('dag')                    // 'dag' | 'report' — primary destination
  const [themeFilter, setThemeFilter] = useState(null)       // E1: drill the tree to one direction
  const landedRef = useRef(false)                            // auto-land on Report once, on finish
  // Resizable / collapsible panes (standard multi-pane layout), persisted across sessions.
  const [sideW, setSideW] = useState(() => +localStorage.getItem('ll.sideW') || 420)
  const [dockH, setDockH] = useState(() => +localStorage.getItem('ll.dockH') || 230)
  const [sideC, setSideC] = useState(() => localStorage.getItem('ll.sideC') === '1')
  const [dockC, setDockC] = useState(() => localStorage.getItem('ll.dockC') === '1')
  useEffect(() => {
    localStorage.setItem('ll.sideW', sideW); localStorage.setItem('ll.dockH', dockH)
    localStorage.setItem('ll.sideC', sideC ? '1' : '0'); localStorage.setItem('ll.dockC', dockC ? '1' : '0')
  }, [sideW, dockH, sideC, dockC])
  // Drag a splitter: the side panel grows when dragged left, the dock when dragged up.
  const startDrag = (axis) => (e) => {
    e.preventDefault()
    const x0 = e.clientX, y0 = e.clientY, w0 = sideW, h0 = dockH
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))
    const onMove = (ev) => {
      // bounds stay sane on small windows: keep max ≥ min so clamp never degenerates
      if (axis === 'side') setSideW(clamp(w0 - (ev.clientX - x0), 280, Math.max(320, window.innerWidth - 340)))
      else setDockH(clamp(h0 - (ev.clientY - y0), 90, Math.max(140, window.innerHeight - 160)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMove); window.addEventListener('mouseup', onUp)
    document.body.style.userSelect = 'none'
  }
  const [toast, setToast] = useState(null)
  const [cfg, setCfg] = useState(null)
  useEffect(() => { get(`/api/runs/${runId}/config`).then(setCfg).catch(() => {}) }, [runId])
  // Auto-land on the Report once, when a live run finishes — the conclusion is what the user wants
  // at that moment. Guarded so a manual switch back to the DAG sticks. Live runs default to 'dag'.
  useEffect(() => {
    if (live?.finished && !landedRef.current) { landedRef.current = true; setView('report') }
  }, [live?.finished])
  useEffect(() => {
    if (viewSeq == null || viewSeq >= seq) { setHist(null); return }
    get(`/api/runs/${runId}/state?seq=${viewSeq}`).then(p => setHist(p.state)).catch(() => {})
  }, [viewSeq, seq, runId])
  const showToast = (m) => { setToast(m); setTimeout(() => setToast(null), 2200) }
  // Members of the selected group — memoized so unrelated re-renders (toast, live ticks) don't
  // re-walk all nodes; only recomputes when the node set / mode / selection actually changes.
  const groupMembers = useMemo(() => {
    const ns = (hist || live)?.nodes
    return (selectedGroup != null && ns) ? (computeGroups(ns, groupMode).get(selectedGroup) || []) : []
  }, [hist, live, groupMode, selectedGroup])
  // Node selection clears any group selection (the side panel shows one or the other).
  const selectNode = (id) => { setSelectedId(id); if (id != null) setSelectedGroup(null) }
  // Drill-down from the dock/timeline: select a node, optionally open a tab + jump the scrubber.
  const focusNode = (id, tab, seq) => {
    selectNode(id)
    if (tab) setInspectTab(tab)
    if (seq != null) setViewSeq(seq)
  }
  // --- semantic-zoom grouping controls ---
  const toggleGroup = (key) => setCollapsed(s => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n })
  const changeMode = (m) => { setGroupMode(m); setCollapsed(new Set()); setSelectedGroup(null) }
  const selectGroup = (key) => { setSelectedGroup(key); if (key != null) setSelectedId(null) }
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
    if (live?.finished && !autoCollapsedRef.current && (groupMode === 'theme' || groupMode === 'niche') && collapsed.size === 0) {
      autoCollapsedRef.current = true; autoCollapse()
    }
  }, [live?.finished, groupMode])

  if (!live) return <div className="app"><div className="runlist"><div className="notice">Connecting to run <b>{runId}</b>…</div></div></div>
  const state = hist || live
  const replaying = hist != null
  // Liveness reflects the ACTUAL run, not the viewed snapshot: green+breathing only while a
  // connected run is still going; a finished run shows a calm "finished", a dropped SSE "offline".
  const liveStatus = !connected ? 'off' : (live.finished ? 'done' : 'on')
  const liveLabel = !connected ? 'offline' : (live.finished ? 'finished' : 'live')
  const evalSec = state.total_eval_seconds || 0
  const maxEval = cfg?.max_eval_seconds
  const cost = state.llm_cost


  return (
    <div className="app">
      <div className="topbar run-head">
        <span className="brand"><span className="dot">◉</span> LoopLab</span>
        <button className="btn sm ghost" onClick={onBack}>← runs</button>
        <div className="view-toggle" role="tablist">
          <button className={'vt' + (view === 'dag' ? ' on' : '')} onClick={() => setView('dag')}>Search</button>
          <button className={'vt report' + (view === 'report' ? ' on' : '')} onClick={() => setView('report')}
            title="conclusion-first run report">★ Report</button>
          <button className={'vt' + (panel === 'overview' ? ' on' : '')} onClick={() => setPanel(panel === 'overview' ? null : 'overview')}
            title="at-a-glance run summary — best metric, budget, strategy, hints">Overview</button>
        </div>
        <span className="pill phase">{phaseLabel(state)}</span>
        <span className="muted" style={{ maxWidth: 280, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }} title={state.goal}>{state.goal || state.task_id}</span>
        <span className={'live ' + liveStatus}><span className="led" />{liveLabel}{replaying && ' · replay'}</span>
        <span className="spacer" />
        {/* round-8: keep only COMPACT at-a-glance metrics here (eval, tokens) + alerts; the fuller
            set (strategy + rationale, hints, dedup) moved to the Overview tab so this header stays a
            SINGLE non-wrapping line. Clicking a metric opens Overview for the detail. */}
        {evalSec > 0 && <span className="chip" title="eval time — open Overview for the budget bar" onClick={() => setPanel('overview')} style={{ cursor: 'pointer' }}>
          <span className="k">eval</span> {fmt(evalSec, 1)}s{maxEval ? ` / ${maxEval}` : ''}</span>}
        {cost && <span className="chip" title="tokens — open Overview" onClick={() => setPanel('overview')} style={{ cursor: 'pointer' }}>
          <span className="k">tokens</span> {fmtInt(cost.total_tokens)}</span>}
        {state.reward_hacks?.length > 0 && <span className="chip alarm" title="suspicious wins flagged (B5) — open Trust" onClick={() => setPanel('trust')} style={{ cursor: 'pointer' }}>
          <span className="k">⚠ hack?</span> {state.reward_hacks.length}</span>}
        {live.paused && <span className="chip warn">⏸ paused</span>}
      </div>

      {/* All actions now run through the chat (type a /command or just say what to do). The approval
          phase shows an inline reminder of the exact command to type. */}
      {(live.phase === 'approval' || live.phase === 'spec_approval') &&
        <div className="topbar" style={{ background: 'rgba(74,163,255,.12)', borderBottom: '1px solid var(--accent-dim)' }}>
          <b>{live.phase === 'approval'
            ? `Human approval required for the final best — type `
            : 'Eval spec needs ratification — type '}</b>
          <code className="cmd-hint">{live.phase === 'approval' ? `/approve #${live.best_node_id}` : '/ratify'}</code>
          <b>&nbsp;in the chat below.</b>
        </div>}

      <div className="topbar panel-bar" style={{ minHeight: 38, paddingTop: 4, paddingBottom: 4 }}>
        <div className="menu">{PANEL_GROUPS.map((group, gi) => <React.Fragment key={gi}>
          {gi > 0 && <span className="panel-sep" />}
          {group.map(([k, l]) => <button key={k} className={'btn sm ghost' + (panel === k ? ' on' : '')} onClick={() => setPanel(k)}>{l}</button>)}
        </React.Fragment>)}</div>
      </div>

      {view === 'report'
        ? <div className="main"><div className="report-scroll">
            <ReportView state={state} runId={runId} onToast={showToast}
              onOpenPanel={(p) => setPanel(p)} /></div></div>
        : <>
      <DirectionsOverview state={state} active={themeFilter} onPick={setThemeFilter} />
      <div className="main">
        <div className="canvas-wrap"><Dag state={state} selectedId={selectedId} onSelect={selectNode}
          groupMode={groupMode} collapsed={collapsed} onToggleGroup={toggleGroup} onSetMode={changeMode}
          onCollapseAll={(keys) => setCollapsed(new Set(keys))} onExpandAll={() => setCollapsed(new Set())}
          onAutoCollapse={autoCollapse}
          selectedGroup={selectedGroup} onSelectGroup={selectGroup} themeFilter={themeFilter} /></div>
        {sideC
          ? <div className="side-rail" title="show panel" onClick={() => setSideC(false)}>‹ {selectedGroup != null ? 'group' : 'inspector'}</div>
          : <>
              <div className="splitter v" onMouseDown={startDrag('side')} title="drag to resize" />
              <div className="side" style={{ width: sideW }}>
                <div className="pane-grip">
                  <span className="muted">{selectedGroup != null ? 'group' : 'inspector'}</span>
                  <span className="spacer" style={{ flex: 1 }} />
                  <button className="btn sm ghost" title="collapse panel" onClick={() => setSideC(true)}>⟩</button>
                </div>
                {selectedGroup != null
                  ? <GroupSummary groupKey={selectedGroup} memberIds={groupMembers}
                      state={state} onSelectNode={focusNode} onClose={() => setSelectedGroup(null)} />
                  : <Inspector runId={runId} nodeId={selectedId} state={state} live={live}
                      tab={inspectTab} setTab={setInspectTab} onToast={showToast} />}
              </div>
            </>}
      </div>

      {!dockC && <div className="splitter h" onMouseDown={startDrag('dock')} title="drag to resize" />}
      <Dock runId={runId} live={live} liveSeq={seq} viewSeq={viewSeq} setViewSeq={setViewSeq} onFocus={focusNode}
            selectedId={selectedId} onToast={showToast}
            collapsed={dockC} onToggleCollapse={() => setDockC(c => !c)} height={dockH} />
        </>}

      {panel === 'overview' && <OverviewPanel state={state} maxEval={maxEval} onClose={() => setPanel(null)} onOpenPanel={setPanel} />}
      {panel === 'research' && <ResearchPanel state={state} runId={runId} onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'trust' && <TrustPanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'sensitivity' && <SensitivityPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'importance' && <HyperImportancePanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'failures' && <FailuresPanel state={state} onSelect={setSelectedId} onClose={() => setPanel(null)} />}
      {panel === 'pareto' && <ParetoPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'data' && <DataQualityPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'compare' && <ComparePanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'crossrun' && <CrossRunPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'collab' && <CollabPanel state={state} runId={runId} onSelect={selectNode} onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'config' && <ConfigPanel runId={runId} state={state} onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'authoring' && <AuthoringPanel onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'memory' && <MemoryPanel onClose={() => setPanel(null)} />}
      {panel === 'registry' && <RegistryPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'gpu' && <GpuPanel onClose={() => setPanel(null)} />}
      {panel === 'events' && <EventExplorer runId={runId} onClose={() => setPanel(null)} />}

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
