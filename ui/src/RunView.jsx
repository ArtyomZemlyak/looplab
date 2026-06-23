import React, { useEffect, useState } from 'react'
import { useRunState, useNotifications } from './hooks.js'
import { get, fmt, fmtInt, phaseLabel, CONTROL } from './util.js'
import Dag from './Dag.jsx'
import Inspector from './Inspector.jsx'
import Dock from './Dock.jsx'
import {
  TrustPanel, SensitivityPanel, FailuresPanel, ParetoPanel, DataQualityPanel,
  ConfigPanel, AuthoringPanel, MemoryPanel, RegistryPanel, ReportPanel, EventExplorer,
  ComparePanel, MetaGraphPanel,
} from './panels.jsx'

const PANELS = [
  ['trust', 'Trust'], ['sensitivity', 'Sensitivity'], ['failures', 'Failures'],
  ['pareto', 'Pareto/Div'], ['data', 'Data'], ['compare', 'Compare'], ['config', 'Config'],
  ['authoring', 'Authoring'], ['memory', 'Memory'], ['registry', 'Registry'],
  ['meta', 'Meta-graph'], ['report', 'Report'], ['events', 'Events'],
]

export default function RunView({ runId, onBack }) {
  const { live, seq, connected } = useRunState(runId)
  const [viewSeq, setViewSeq] = useState(null)
  const [hist, setHist] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [panel, setPanel] = useState(null)
  const [toast, setToast] = useState(null)
  const [notif, setNotif] = useState(false)
  const [cfg, setCfg] = useState(null)
  useNotifications(notif, live)
  useEffect(() => { get(`/api/runs/${runId}/config`).then(setCfg).catch(() => {}) }, [runId])
  useEffect(() => {
    if (viewSeq == null || viewSeq >= seq) { setHist(null); return }
    get(`/api/runs/${runId}/state?seq=${viewSeq}`).then(p => setHist(p.state)).catch(() => {})
  }, [viewSeq, seq, runId])
  const showToast = (m) => { setToast(m); setTimeout(() => setToast(null), 2200) }

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

        <div className="toolbar">
          {!state.finished && (state.paused
            ? <button className="btn sm primary" onClick={() => act(() => CONTROL.resume(runId), 'resumed')}>▶ Resume</button>
            : <button className="btn sm warn" onClick={() => act(() => CONTROL.pause(runId), 'paused')}>⏸ Pause</button>)}
          {!state.finished && <button className="btn sm danger" onClick={() => act(() => CONTROL.abort(runId), 'stopping run')}>■ Stop</button>}
          {!state.finished && <button className="btn sm" title="inject a directive for the researcher" onClick={() => { const t = prompt('Hint / directive for the researcher (e.g. "try higher degree")'); if (t) act(() => CONTROL.hint(runId, t), 'hint sent') }}>💡 Hint</button>}
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
        <div className="canvas-wrap"><Dag state={state} selectedId={selectedId} onSelect={setSelectedId} /></div>
        <div className="side"><Inspector runId={runId} nodeId={selectedId} state={state} live={live} onToast={showToast} /></div>
      </div>

      <Dock runId={runId} liveSeq={seq} viewSeq={viewSeq} setViewSeq={setViewSeq} />

      {panel === 'trust' && <TrustPanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'sensitivity' && <SensitivityPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'failures' && <FailuresPanel state={state} onSelect={setSelectedId} onClose={() => setPanel(null)} />}
      {panel === 'pareto' && <ParetoPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'data' && <DataQualityPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'compare' && <ComparePanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'meta' && <MetaGraphPanel onClose={() => setPanel(null)} />}
      {panel === 'config' && <ConfigPanel runId={runId} state={state} onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'authoring' && <AuthoringPanel onToast={showToast} onClose={() => setPanel(null)} />}
      {panel === 'memory' && <MemoryPanel onClose={() => setPanel(null)} />}
      {panel === 'registry' && <RegistryPanel state={state} onClose={() => setPanel(null)} />}
      {panel === 'report' && <ReportPanel state={state} runId={runId} onClose={() => setPanel(null)} />}
      {panel === 'events' && <EventExplorer runId={runId} onClose={() => setPanel(null)} />}

      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}

async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
