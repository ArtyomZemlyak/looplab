import React, { useEffect, useMemo, useState } from 'react'
import { useRunState, useNotifications } from './hooks.js'
import { get, fmt, fmtInt, phaseLabel, CONTROL } from './util.js'
import Dag from './Dag.jsx'
import Inspector, { GroupSummary } from './Inspector.jsx'
import Dock from './Dock.jsx'
import { computeGroups } from './grouping.js'
import { InjectModal, ChatTab } from './experiment.jsx'
import {
  TrustPanel, SensitivityPanel, FailuresPanel, ParetoPanel, DataQualityPanel,
  ConfigPanel, AuthoringPanel, MemoryPanel, RegistryPanel, ReportPanel, EventExplorer,
  ComparePanel, MetaGraphPanel, GpuPanel, PolicyPanel, StrategistPanel, HyperImportancePanel,
} from './panels.jsx'

const PANELS = [
  ['report', 'Report'], ['trust', 'Trust'], ['policy', 'Policy'], ['strategist', 'Strategist'],
  ['sensitivity', 'Sensitivity'], ['importance', 'Importance'], ['failures', 'Failures'],
  ['pareto', 'Pareto/Div'], ['data', 'Data'], ['compare', 'Compare'], ['config', 'Config'],
  ['authoring', 'Authoring'], ['memory', 'Memory'], ['registry', 'Registry'], ['meta', 'Meta-graph'],
  ['gpu', 'GPU'], ['events', 'Events'],
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
  const [notif, setNotif] = useState(false)
  const [cfg, setCfg] = useState(null)
  const [injectPrefill, setInjectPrefill] = useState(null)   // null = closed; object = inject modal open
  const [chatOpen, setChatOpen] = useState(false)
  const openInject = (prefill) => setInjectPrefill(prefill || {})
  useNotifications(notif, live)
  useEffect(() => { get(`/api/runs/${runId}/config`).then(setCfg).catch(() => {}) }, [runId])
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

  if (!live) return <div className="app"><div className="runlist"><div className="notice">Connecting to run <b>{runId}</b>…</div></div></div>
  const state = hist || live
  const replaying = hist != null
  const evalSec = state.total_eval_seconds || 0
  const maxEval = cfg?.max_eval_seconds
  const cost = state.llm_cost

  const act = async (fn, msg) => { try { await fn(); showToast(msg) } catch (e) { showToast('failed: ' + e.message) } }

  return (
    <div className="app">
      <div className="topbar">
        <span className="brand"><span className="dot">◉</span> LoopLab</span>
        <button className="btn sm ghost" onClick={onBack}>← runs</button>
        <span className="pill phase">{phaseLabel(state)}</span>
        <span className="muted" style={{ maxWidth: 280, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }} title={state.goal}>{state.goal || state.task_id}</span>
        <span className={'live ' + (connected ? 'on' : 'off')}><span className="led" />{connected ? 'live' : 'offline'}{replaying && ' · replay'}</span>
        <span className="spacer" />

        <div className="gauge">
          <div className="lab"><span>eval time</span><span>{fmt(evalSec, 3)}s{maxEval ? ` / ${maxEval}` : ''}</span></div>
          {maxEval ? <div className="bar"><div className="fill hot" style={{ width: Math.min(100, evalSec / maxEval * 100) + '%' }} /></div> : null}
        </div>
        {cost && <span className="chip"><span className="k">tokens</span> {fmtInt(cost.total_tokens)}</span>}
        {state.pending_hints?.length > 0 && <span className="chip" title={state.pending_hints.map(h => '• ' + (h.text || JSON.stringify(h))).join('\n')}>
          <span className="k">💡 hints</span> {state.pending_hints.length}</span>}
        {state.active_strategy && <span className="chip" title={state.active_strategy.rationale || 'active strategy'}
          onClick={() => setPanel('strategist')} style={{ cursor: 'pointer' }}>
          <span className="k">🧭 strategy</span> {state.active_strategy.policy || 'greedy'}{state.active_strategy.fidelity ? '/' + state.active_strategy.fidelity : ''}</span>}
        {state.novelty_events?.length > 0 && <span className="chip" title="near-duplicate proposals nudged to diversify (E1)">
          <span className="k">🔁 dedup</span> {state.novelty_events.length}</span>}
        {state.reward_hacks?.length > 0 && <span className="chip alarm" title="suspicious wins flagged (B5)" onClick={() => setPanel('trust')} style={{ cursor: 'pointer' }}>
          <span className="k">⚠ hack?</span> {state.reward_hacks.length}</span>}

        <div className="toolbar">
          {!state.finished && (state.paused
            ? <button className="btn sm primary" onClick={() => act(() => CONTROL.resume(runId), 'resumed')}>▶ Resume</button>
            : <button className="btn sm warn" onClick={() => act(() => CONTROL.pause(runId), 'paused')}>⏸ Pause</button>)}
          {!state.finished && <button className="btn sm danger" onClick={() => act(() => CONTROL.abort(runId), 'stopping run')}>■ Stop</button>}
          {!state.finished && <button className="btn sm" title="inject a directive for the researcher" onClick={() => { const t = prompt('Hint / directive for the researcher (e.g. "try higher degree")'); if (t) act(() => CONTROL.hint(runId, t), 'hint sent') }}>💡 Hint</button>}
          <button className="btn sm" title={state.finished ? 'add an experiment — reopens & continues the run' : 'hand-add an experiment node to the tree'} onClick={() => openInject(null)}>✚ Experiment</button>
          <button className="btn sm" title="chat about this run / the selected experiment" onClick={() => setChatOpen(true)}>💬 Chat</button>
          {state.paused && <button className="btn sm" onClick={() => act(() => post(`/api/runs/${runId}/resume`, {}), 'engine resume spawned')}>⟳ Spawn resume</button>}
          <button className={'btn sm' + (notif ? ' primary' : '')} title="desktop notifications" onClick={() => setNotif(n => !n)}>🔔</button>
        </div>
      </div>

      {(state.phase === 'approval' || state.phase === 'spec_approval') &&
        <div className="topbar" style={{ background: 'rgba(74,163,255,.12)', borderBottom: '1px solid var(--accent-dim)' }}>
          <b>{state.phase === 'approval' ? 'Human approval required for the final best.' : 'Eval spec needs ratification.'}</b>
          <span className="spacer" />
          {state.phase === 'approval'
            ? <button className="btn sm primary" onClick={() => act(() => CONTROL.approve(runId, state.best_node_id), 'approved')}>✓ Approve #{state.best_node_id}</button>
            : <button className="btn sm primary" onClick={() => act(() => CONTROL.ratify(runId), 'ratified')}>✓ Ratify spec</button>}
          <button className="btn sm" onClick={() => act(() => post(`/api/runs/${runId}/resume`, {}), 'resume spawned')}>⟳ Spawn resume</button>
        </div>}

      <div className="topbar" style={{ minHeight: 38, paddingTop: 4, paddingBottom: 4 }}>
        <span className="muted">panels:</span>
        <div className="menu">{PANELS.map(([k, l]) => <button key={k} className="btn sm ghost" onClick={() => setPanel(k)}>{l}</button>)}</div>
      </div>

      <div className="main">
        <div className="canvas-wrap"><Dag state={state} selectedId={selectedId} onSelect={selectNode}
          groupMode={groupMode} collapsed={collapsed} onToggleGroup={toggleGroup} onSetMode={changeMode}
          onCollapseAll={(keys) => setCollapsed(new Set(keys))} onExpandAll={() => setCollapsed(new Set())}
          selectedGroup={selectedGroup} onSelectGroup={selectGroup} /></div>
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
                      tab={inspectTab} setTab={setInspectTab} onToast={showToast} onInject={openInject} />}
              </div>
            </>}
      </div>

      {!dockC && <div className="splitter h" onMouseDown={startDrag('dock')} title="drag to resize" />}
      <Dock runId={runId} liveSeq={seq} viewSeq={viewSeq} setViewSeq={setViewSeq} onFocus={focusNode}
            collapsed={dockC} onToggleCollapse={() => setDockC(c => !c)} height={dockH} />

      {panel === 'trust' && <TrustPanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'sensitivity' && <SensitivityPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'importance' && <HyperImportancePanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'failures' && <FailuresPanel state={state} onSelect={setSelectedId} onClose={() => setPanel(null)} />}
      {panel === 'pareto' && <ParetoPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'data' && <DataQualityPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'compare' && <ComparePanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'meta' && <MetaGraphPanel onClose={() => setPanel(null)} />}
      {panel === 'config' && <ConfigPanel runId={runId} state={state} onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'authoring' && <AuthoringPanel onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'memory' && <MemoryPanel onClose={() => setPanel(null)} />}
      {panel === 'registry' && <RegistryPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'policy' && <PolicyPanel state={state} onSelect={selectNode} onClose={() => setPanel(null)} />}
      {panel === 'strategist' && <StrategistPanel state={state} runId={runId} onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'gpu' && <GpuPanel onClose={() => setPanel(null)} />}
      {panel === 'report' && <ReportPanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'events' && <EventExplorer runId={runId} onClose={() => setPanel(null)} />}

      {injectPrefill != null && <InjectModal runId={runId} state={live} initial={injectPrefill}
        onClose={() => setInjectPrefill(null)} onToast={showToast} />}
      {chatOpen && <div className="overlay" onClick={() => setChatOpen(false)}>
        <div className="panel" style={{ width: 'min(680px, 95%)' }} onClick={e => e.stopPropagation()}>
          <div className="panel-h"><span className="ttl">Research chat</span>
            <span className="pill">{selectedId != null ? `experiment #${selectedId}` : 'whole run'}</span>
            <span className="right" /><button className="btn sm ghost" onClick={() => setChatOpen(false)}>✕</button></div>
          <div className="panel-b" style={{ height: 460, display: 'flex', flexDirection: 'column' }}>
            <ChatTab runId={runId} nodeId={selectedId} state={live}
              onInject={(p) => { setChatOpen(false); openInject(p) }} onToast={showToast} />
          </div>
        </div>
      </div>}

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}

async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
