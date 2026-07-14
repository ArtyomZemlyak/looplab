import React, { useEffect, useMemo, useState, useRef } from 'react'
import { get, fmt, fmtInt, isSweep, spanDetail, nodeConversation, CONTROL, clearNodeTrace, commandFeedback } from './util.js'
import { usePoll } from './hooks.js'
import { Trajectory, ParallelCoords, Scatter, MetricLines } from './charts.jsx'
import { groupAggregate } from './grouping.js'
import { mergeSummary, nodeChip } from './report.js'
import { OpIcon } from './icons.jsx'
import Markdown from './markdown.jsx'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import { nodeFeasibilityStatus } from './trustSemantics.js'
import { reviewInspectorTabs } from './runRouteState.js'
import { DataTable, nextRovingIndex } from './accessibility.jsx'
import CommentsThread from './CommentsThread.jsx'

// One lifecycle "Trace" tab replaces the old Reasoning / LLM / Agent split: a node is worked on by
// several parts in sequence (Researcher proposes, Developer implements/repairs, then it's evaluated
// and confirmed), so we show that whole story in one place — each stage with its sub-steps, inline
// LLM I/O, and the coding-agent's validation — instead of three disconnected panes. The Inspector is
// READ-ONLY (Workstream C): every node action — confirm/ablate/fork/promote/note — is done from the
// chat (add the node via its ＋#id chip, or use a /command), so there's no per-node button toolbar.
// Tab order keeps durable review context closest to the summary: Overview → Comments →
// Trials (sweeps) → Trace → Code → Metrics → Trust → Cost.
const TABS = ['Overview', 'Comments', 'Trace', 'Code', 'Metrics', 'Trust', 'Cost']

// The ONE per-node write action (Workstream-C exception): re-run THIS node in place — no new node —
// from a chosen stage. It's a recovery/fix control (natural to trigger from the failed node itself),
// unlike the exploratory confirm/ablate/fork which stay in the chat. Appends a node_reset control
// event; the engine applies it on the next resume.
function ResetBtn({ runId, id, generation, onToast }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const menuRef = useRef(null)
  const STAGES = [
    ['eval', 're-score', 'keep the idea + code, just re-run the evaluation (an infra / API-key blip)'],
    ['implement', 're-run the Developer', "keep the Researcher's idea, re-write the code (its code crashed)"],
    ['propose', 'full redo', 're-propose the idea, re-develop, then re-evaluate'],
  ]
  const doReset = async (stage) => {
    if (busy) return
    setOpen(false)
    requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }))
    setBusy(true)
    try {
      const feedback = commandFeedback(await CONTROL.resetNode(runId, id, stage, generation), {
        success: `Reset #${id} from ${stage} applied — the engine is processing it`, noop: `#${id} already reflects that reset`,
        executing: `Reset #${id} from ${stage} requested — waiting for the engine`, failure: `Reset #${id} failed`,
      })
      onToast?.(feedback.message)
    } catch (error) { onToast?.(`Reset #${id} failed: ${error.message || error}`) }
    finally { setBusy(false) }
  }
  useEffect(() => {
    if (!open) return
    requestAnimationFrame(() => menuRef.current?.querySelector('[role="menuitem"]')?.focus())
  }, [open])
  useEffect(() => {
    if (!open) return
    const dismiss = event => { if (!rootRef.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', dismiss, true)
    return () => document.removeEventListener('pointerdown', dismiss, true)
  }, [open])
  const onMenuKeyDown = event => {
    const items = [...(menuRef.current?.querySelectorAll('[role="menuitem"]') || [])]
    const index = items.indexOf(document.activeElement)
    if (event.key === 'Tab') { setOpen(false); return }
    if (event.key === 'Escape') {
      event.preventDefault(); setOpen(false); requestAnimationFrame(() => triggerRef.current?.focus()); return
    }
    const next = nextRovingIndex(event.key, Math.max(0, index), items.length)
    if (next == null) return
    event.preventDefault(); items[next]?.focus()
  }
  return <span ref={rootRef} style={{ position: 'relative', marginLeft: 8 }}>
    <button ref={triggerRef} className="ctx-chip" style={{ padding: '0 6px', cursor: 'pointer' }}
            title="re-run THIS node in place (no new node) from a chosen stage"
            aria-haspopup="menu" aria-expanded={open} aria-disabled={busy} aria-busy={busy}
            onClick={() => { if (!busy) setOpen(!open) }}>{busy ? '↻ Resetting…' : '↻ Reset ▾'}</button>
    {open && <div ref={menuRef} role="menu" aria-label={`Reset experiment ${id} from stage`}
      onKeyDown={onMenuKeyDown}
      onBlur={event => {
        if (event.relatedTarget !== triggerRef.current && !event.currentTarget.contains(event.relatedTarget)) setOpen(false)
      }}
      style={{ position: 'absolute', zIndex: 30, top: '110%', left: 0, background: 'var(--bg-1)', border: '1px solid var(--border)', borderRadius: 6, padding: 4, minWidth: 240, boxShadow: '0 4px 16px rgba(0,0,0,.4)' }}>
      {STAGES.map(([stage, label, desc]) =>
        <button type="button" role="menuitem" key={stage} className="reset-stage-option"
             tabIndex={-1} title={desc} onClick={() => doReset(stage)}>
          <span style={{ display: 'block' }}><b style={{ fontSize: 12 }}>{label}</b> <span className="muted" style={{ fontSize: 10 }}>from {stage}</span></span>
          <span className="muted" style={{ display: 'block', fontSize: 10 }}>{desc}</span>
        </button>)}
    </div>}
  </span>
}

export default function Inspector({ runId, nodeId, state, live, tab, setTab, onToast, readOnly = false,
  historySeq = null, expectedGeneration = null, readOnlyReason = 'history', evidenceAvailable = true,
  commentsRevision = null, focusCommentId = null }) {
  const [detail, setDetail] = useState(null)
  const [detailStatus, setDetailStatus] = useState('idle')
  const [detailError, setDetailError] = useState('')
  const [detailNonce, setDetailNonce] = useState(0)
  useEffect(() => {
    setDetail(null)               // clear stale detail immediately so we never render node A's
    setDetailError('')
    if (nodeId == null) { setDetailStatus('idle'); return } // payload under node B while B's fetch is in flight (or failed)
    if (readOnlyReason === 'review' && !evidenceAvailable) { setDetailStatus('restricted'); return }
    setDetailStatus('loading')
    let on = true
    const at = readOnly && historySeq != null
      ? `?seq=${encodeURIComponent(historySeq)}&expected_generation=${encodeURIComponent(expectedGeneration || '')}`
      : ''
    get(`/api/runs/${encodeURIComponent(runId)}/nodes/${nodeId}${at}`)
      .then(d => { if (on) { setDetail(d); setDetailStatus('ready') } })
      .catch(() => { if (on) { setDetailStatus('error'); setDetailError('Full node details could not be loaded.') } })
    return () => { on = false }
  }, [runId, nodeId, state?.nodes?.[nodeId]?.status, readOnly, historySeq, expectedGeneration,
    readOnlyReason, evidenceAvailable, detailNonce])
  // Live-refresh the node detail (it carries n.trace spans + the agent report) while the run is ACTIVELY
  // working this node — so the Trace tab fills in WITHOUT the user toggling tabs. Two windows, both
  // engine-alive & not-finished (stops at terminal / engine death):
  //   • building  — an LLM is authoring the node (propose + implement, or a repair).
  //   • pending   — the sandbox is EVALUATING it (data_prep → train → score). Training used to show
  //     nothing live (no child LLM spans, and the stage op flushes only on close); command_eval now
  //     emits a `stage_started` anchor per stage so the Train/Evaluate band fills in DURING the run.
  //     A pending node's status doesn't change until it's scored, so without polling here the Trace
  //     tab froze after "Developer implement" for the whole training run.
  const nodeStatus = state?.nodes?.[nodeId]?.status
  const engineActive = !readOnly && !!live && live.engine_running !== false && !live.finished && nodeId != null
  // The node being EVALUATED right now is the LATEST pending one — the loop creates a node then scores
  // it before creating the next. Gate the pending pulse on that (+ not paused), so an older queued or
  // injected pending node doesn't poll-spin every 4s or mislabel itself as "training".
  const latestId = Math.max(-1, ...Object.keys(state?.nodes || {}).map(Number))
  const evaluatingThis = nodeStatus === 'pending' && !live?.paused && Number(nodeId) === latestId
  const nodeWorking = engineActive && (live.building?.node_id === nodeId || evaluatingThis)
  usePoll((alive) => {
    // alive() gates the async resolution: if the user selects a different node (or the poll is
    // disabled) while this /nodes/{nodeId} request is in flight, its late response must NOT overwrite
    // the newly-selected node's detail — otherwise node A's Code/Trace/Metrics render (stuck) under B.
    get(`/api/runs/${encodeURIComponent(runId)}/nodes/${nodeId}`).then(d => { if (alive() && d) { setDetail(d); setDetailStatus('ready'); setDetailError('') } }).catch(() => {})
  }, 4000, [runId, nodeId, nodeWorking], { enabled: !readOnly && nodeWorking, immediate: false })

  if (nodeId == null) return <div className="insp-empty">Select a node to inspect its idea, code, metrics, trust, and agent trace.</div>
  const n = detail || (state.nodes[nodeId])
  if (!n) return <div className="insp-empty">…</div>
  // Metric-drift is run-level state (state.drifts), each entry tagged with its node_id — the
  // per-node detail payload has no `drifts` key, so filter the run state down to this node.
  const nodeDrifts = (state?.drifts || []).filter(d => d.node_id === n.id)
  // Sweep nodes get a Trials tab (right after Overview). `activeTab` guards against a stale tab
  // (e.g. 'Trials' left selected after switching to a non-sweep node) falling through to nothing.
  const sweep = isSweep(n)
  const liveTabs = sweep ? ['Overview', 'Comments', 'Trials', ...TABS.slice(2)] : TABS
  const tabs = readOnly
    ? readOnlyReason === 'review' ? reviewInspectorTabs(evidenceAvailable) : ['Overview', 'Code', 'Trust', 'Cost']
    : liveTabs
  const activeTab = tabs.includes(tab) ? tab : 'Overview'
  const tabSlug = value => value.toLowerCase().replace(/[^a-z0-9]+/g, '-')
  const tabId = value => `inspector-${nodeId}-tab-${tabSlug(value)}`
  const panelId = value => `inspector-${nodeId}-panel-${tabSlug(value)}`
  const onTabKeyDown = (event, index) => {
    const next = nextRovingIndex(event.key, index, tabs.length)
    if (next == null) return
    event.preventDefault()
    const nextTab = tabs[next]
    setTab(nextTab)
    requestAnimationFrame(() => document.getElementById(tabId(nextTab))?.focus())
  }

  return (
    <>
      <div className="tabs" role="tablist" aria-label="Inspector sections">
        {tabs.map((t, index) => <button key={t} id={tabId(t)} type="button" role="tab"
          aria-selected={t === activeTab} aria-controls={t === activeTab ? panelId(t) : undefined}
          tabIndex={t === activeTab ? 0 : -1}
          className={'tab' + (t === activeTab ? ' active' : '') + (t === 'Trust' && (n.violations?.length || nodeDrifts.length) ? ' alarm' : '')}
          onClick={() => setTab(t)} onKeyDown={event => onTabKeyDown(event, index)}>{t}</button>)}
      </div>
      <div className="insp-body" id={panelId(activeTab)} role="tabpanel"
        aria-labelledby={tabId(activeTab)} tabIndex={0}>
        {detailStatus === 'loading' && <div className="notice" role="status">Loading full node details…</div>}
        {detailStatus === 'error' && <div className="notice resource-error" role="alert"><span>{detailError} The summary below may be incomplete.</span><button className="btn sm" onClick={() => setDetailNonce(n => n + 1)}>Retry</button></div>}
        {readOnly
          ? <div className="insp-hint history-inline">{readOnlyReason === 'review'
              ? evidenceAvailable
                ? 'Read-only review with redacted source evidence. Live traces and actions stay hidden.'
                : 'Summary-only review. Source, live traces, and actions are not included.'
              : `Snapshot seq ${historySeq} · read-only. Live traces, metrics sidecars and actions are hidden.`}</div>
          : <div className="insp-hint muted">Run actions (confirm · ablate · fork · promote) live in the chat. Use the Comments tab for durable review notes, or attach <button className="ctx-chip" style={{ padding: '0 6px', cursor: 'pointer' }} title="attach this experiment to the assistant context" onClick={() => window.dispatchEvent(new CustomEvent('ll:attach-node', { detail: { id: n.id } }))}>＋ #{n.id}</button> as context.<ResetBtn runId={runId} id={n.id} generation={n.attempt} onToast={onToast} /></div>}

        {activeTab === 'Overview' && <Overview n={n} state={state} runId={readOnly ? null : runId} onToast={onToast} />}
        {activeTab === 'Comments' && <CommentsThread runId={runId} nodeId={n.id}
          nodeGeneration={n.attempt} expectedGeneration={expectedGeneration} refreshKey={commentsRevision}
          readOnly={readOnly} reviewMode={readOnlyReason === 'review'} focusCommentId={focusCommentId} />}
        {activeTab === 'Trials' && <Trials n={n} detail={detail} state={state} />}
        {activeTab === 'Trace' && <Trace n={n} runId={runId} live={live} working={nodeWorking} />}
        {activeTab === 'Code' && (detailStatus === 'ready'
          ? <Code n={n} />
          : detailStatus === 'error'
            ? <div className="insp-empty">Code is unavailable because full node details failed to load.</div>
            : <div className="insp-empty">Loading code…</div>)}
        {activeTab === 'Metrics' && <Metrics n={n} detail={detail} state={state} runId={runId} />}
        {activeTab === 'Trust' && <Trust n={n} drifts={nodeDrifts} />}
        {activeTab === 'Cost' && <Cost state={state} />}
      </div>
    </>
  )
}

function KV({ k, v }) { return <><div className="k">{k}</div><div className="v">{v}</div></> }

// Summary for a COLLAPSED group's super-node (semantic zoom): aggregate + drill back to members.
export function GroupSummary({ groupKey, memberIds, state, onSelectNode, onClose }) {
  const members = (memberIds || []).map(id => state.nodes[id]).filter(Boolean).sort((a, b) => a.id - b.id)
  const dir = state.direction
  const best = groupAggregate(memberIds || [], state.nodes, dir).best   // same aggregate as the super-node card
  const themes = [...new Set(members.map(n => n.idea?.theme).filter(Boolean))]
  return <>
    <div className="tabs">
      <div className="tab active">Group · {groupKey}</div>
      <span style={{ flex: 1 }} />
      <button className="btn sm ghost" onClick={onClose} title="close group view" aria-label="Close group details">✕</button>
    </div>
    <div className="insp-body">
      <div className="kv">
        <KV k="experiments" v={members.length} />
        <KV k="best" v={fmt(best)} />
        {themes.length > 0 && <KV k="themes" v={themes.join(', ')} />}
      </div>
      <div className="section-h">Best over members</div>
      <Trajectory nodes={members} direction={dir} height={150} onPick={onSelectNode} />
      <div className="section-h">Members <span className="pill">{members.length}</span></div>
      <DataTable caption="Group member results" card={false}><table className="tbl"><thead><tr><th>node</th><th>operator</th><th>metric</th><th>status</th></tr></thead>
        <tbody>{members.map(n => <tr key={n.id}>
          <td><button type="button" className="btn xs ghost" onClick={() => onSelectNode(n.id)}>#{n.id}</button></td>
          <td>{n.operator}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td><td>{n.status}</td></tr>)}</tbody></table></DataTable>
    </div>
  </>
}

// Phase 1: the node's declared eval pipeline as a coloured strip (data_prep ✓ → train ✓ → eval ✗), so a
// crash is pinpointed to its stage instead of hiding behind one opaque "evaluate". Empty on single-command
// evals. The failed stage is tinted red; a still-pending tail (not yet reached) shows muted.
function StagePipeline({ stages, failed, runId, id, generation, onToast }) {
  const [pendingStage, setPendingStage] = useState(null)
  if (!stages || !stages.length) return null
  const tone = (s) => s.status === 'ok' ? 'var(--ok)' : s.status === 'timeout' ? 'var(--working)'
    : s.status === 'reused' ? 'var(--fg-mut)' : 'var(--fail)'
  const ic = (s) => s.status === 'ok' ? '✓' : s.status === 'timeout' ? '⧗' : s.status === 'reused' ? '↺' : '✗'
  const rerun = async (name) => {
    if (!runId || pendingStage) return
    setPendingStage(name)
    try {
      const feedback = commandFeedback(await CONTROL.resetNode(runId, id, name, generation), {
        success: `Reset #${id} from '${name}' applied — the engine is processing it`, noop: `#${id} already reflects that reset`,
        executing: `Re-run of #${id} from '${name}' requested — waiting for the engine`, failure: 'Re-run failed',
      })
      onToast?.(feedback.message)
    } catch (error) { onToast?.(`Re-run failed: ${error.message || error}`) }
    finally { setPendingStage(null) }
  }
  return <div style={{ margin: '8px 0' }}>
    <div className="muted" style={{ fontSize: 10, marginBottom: 3 }}>
      eval pipeline{failed ? ` — failed at ${failed}` : ''}{runId ? ' · click a stage to re-run from there' : ' · historical result (read-only)'}</div>
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
      {stages.map((s, i) => <React.Fragment key={i}>
        {runId ? <button type="button" disabled={pendingStage != null} onClick={() => rerun(s.name)}
          style={{ padding: '2px 7px', borderRadius: 4, fontSize: 11, cursor: 'pointer', background: 'transparent',
                   border: `1px solid ${tone(s)}`, color: tone(s) }}
          title={`${s.name}: ${s.status}${s.seconds != null ? ` · ${s.seconds}s` : ''}${s.exit_code != null ? ` · exit ${s.exit_code}` : ''} — click to re-run the pipeline FROM here (reuse earlier stages)`}>
          {ic(s)} {s.name}</button> : <span
          style={{ padding: '2px 7px', borderRadius: 4, fontSize: 11, border: `1px solid ${tone(s)}`, color: tone(s) }}
          title={`${s.name}: ${s.status}${s.seconds != null ? ` · ${s.seconds}s` : ''}${s.exit_code != null ? ` · exit ${s.exit_code}` : ''} · historical result`}>
          {ic(s)} {s.name}</span>}
        {i < stages.length - 1 && <span className="muted" style={{ fontSize: 10 }}>→</span>}
      </React.Fragment>)}
    </div>
  </div>
}

function Overview({ n, state, runId, onToast }) {
  const p = n.idea?.params || {}
  const uses = mergeSummary(n, state.nodes || {})   // E3: for merges, which technique each parent fused
  const chg = nodeChip(n, state.nodes || {})        // same chip as the card (sweep-aware; '' for merges)
  return <>
    <div className="kv">
      <KV k="node" v={`#${n.id}`} />
      <KV k="operator" v={n.operator} />
      <KV k="parents" v={(n.parent_ids || []).join(', ') || '—'} />
      <KV k="status" v={n.status + (n.id === state.best_node_id ? ' — champion' : '')} />
      <KV k="metric" v={fmt(n.metric)} />
      {n.confirmed_mean != null && <KV k="robust mean" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} (${n.confirmed_seeds}×)`} />}
      <KV k="feasible" v={String(n.feasible)} />
      <KV k="eval seconds" v={fmt(n.eval_seconds)} />
    </div>
    <StagePipeline stages={n.stages} failed={n.failed_stage} runId={runId} id={n.id} generation={n.attempt} onToast={onToast} />
    {chg && <><div className="section-h">What this node did</div><div className="v">{chg}</div></>}
    {uses.length > 0 && <><div className="section-h">Merge — techniques fused</div>
      <ul className="bul">{uses.map(u => <li key={u.parentId}>
        <b>#{u.parentId}</b>{u.theme ? ` · ${u.theme}` : ''}{u.change && u.change !== '—' ? ` — ${u.change}` : ''}</li>)}</ul></>}
    <div className="section-h">Idea params</div>
    {Object.keys(p).length ? <div className="kv">{Object.entries(p).map(([k, v]) => <KV key={k} k={k} v={fmt(v)} />)}</div> : <div className="muted">none</div>}
    {n.idea?.rationale && !(chg && chg.includes(n.idea.rationale)) && <><div className="section-h">Rationale</div><div className="v">{n.idea.rationale}</div></>}
    {n.deleted?.length > 0 && <><div className="section-h">Deleted files</div><div className="v">{n.deleted.join(', ')}</div></>}
  </>
}

// Trace timeline bounds: earliest start + total wall-span across the forest, so every span bar can be
// positioned by its OFFSET from t0 (a langfuse-style waterfall) rather than just sized by duration.
function traceBounds(spans) {
  let lo = Infinity, hi = 0
  const walk = (arr) => (arr || []).forEach(s => {
    const st = (typeof s.start === 'number') ? s.start : null
    const en = st != null ? st + (s.duration_s || 0) : (s.duration_s || 0)
    if (st != null && st < lo) lo = st
    if (en > hi) hi = en
    walk(s.children)
  })
  walk(spans)
  if (!isFinite(lo)) lo = 0
  return { t0: lo, total: Math.max(1e-9, hi - lo) }
}

// Friendly identity for each span kind — turns raw span names into "who did what" so the trace
// reads as the node's life story rather than instrumentation. `tone` colours the waterfall bar so
// phases are distinguishable at a glance. (Span names come from orchestrator.py.)
// icon = an OpIcon glyph name (monochrome, inherits the stage tone via currentColor — no color emoji).
const STAGE = {
  onboard:      { icon: 'flag', role: 'Onboarding', desc: 'task setup & eval spec', tone: '#8a7bb0' },
  create_node:  { icon: 'trending', role: 'Author node', desc: 'propose an idea, then build the solution', tone: '#6f8bb0' },
  propose:      { icon: 'search', role: 'Researcher · propose', desc: 'propose the next idea', tone: '#6fa3b0' },
  // the Developer's own sub-phases (repo tasks): STAGES declares the eval pipeline, PLAN decomposes
  // the change into atomic steps — both read-only, before the write-capable implement session(s).
  stages:       { icon: 'sliders', role: 'Developer · stages', desc: 'declare the eval pipeline (prep → train → …)', tone: '#5f9e8f' },
  plan:         { icon: 'doc', role: 'Developer · plan', desc: 'decompose into atomic steps', tone: '#7fae8f' },
  'handoff-summary': { icon: 'doc', role: 'Handoff summary', desc: 'distill this phase for the next (fewer re-reads downstream)', tone: '#8fa8b8' },
  implement:    { icon: 'gear', role: 'Developer · implement', desc: 'write / edit the solution code', tone: '#6fae97' },
  repair:       { icon: 'bug', role: 'Developer · repair', desc: 'fix a failed parent', tone: '#b0936f' },
  inline_repair: { icon: 'bug', role: 'Developer · inline repair', desc: 'quick in-eval fix attempts', tone: '#b08a6f' },
  seed_workspace: { icon: 'gear', role: 'Workspace', desc: 'materialize node files into the eval workdir', tone: '#8b96a5' },
  evaluate:     { icon: 'target', role: 'Evaluate', desc: 'run the solution & score it', tone: '#a87da8' },
  triage:       { icon: 'bug', role: 'Triage', desc: 'a failed node — decide repair / abandon / reject-idea', tone: '#b07a7a' },
  // declared eval-pipeline stages (looplab_stages.json): each runs as its own block in the node story
  train:        { icon: 'replay', role: 'Train', desc: 'declared pipeline stage: train a fresh model', tone: '#4e8f5d' },
  data_prep:    { icon: 'sliders', role: 'Data prep', desc: 'declared pipeline stage: prepare data/features', tone: '#7a9e5f' },
  score:        { icon: 'target', role: 'Evaluate · score', desc: "operator's protected scoring stage", tone: '#a87da8' },
  confirm_seed: { icon: 'replay', role: 'Confirmation', desc: 'multi-seed robustness check', tone: '#9aa06f' },
  ablate:       { icon: 'sliders', role: 'Ablation', desc: 'sensitivity probe', tone: '#6f8bb0' },
  // sub-operation traces the engine wraps in their own named span — give each a distinct hue so the
  // conversation reads as coloured bands (foresight vs strategy vs research vs merge) at a glance.
  // Two DISTINCT Researcher ranking steps — kept apart so the first doesn't read as a duplicate of
  // the second: `hyp_prioritize` runs BEFORE propose (pick which open hypothesis to pursue),
  // `foresight_rank` runs AFTER propose (predict the chosen proposal's payoff, best-of-N pick).
  hyp_prioritize: { icon: 'bulb', role: 'Researcher · prioritize', desc: 'rank the open-hypothesis board', tone: '#c2a24e' },
  foresight_rank: { icon: 'bulb', role: 'Researcher · foresight', desc: 'predict payoff of the chosen idea', tone: '#c2a24e' },
  foresight:      { icon: 'bulb', role: 'Researcher · foresight', desc: 'predict payoff of the chosen idea', tone: '#c2a24e' },
  strategy_consult: { icon: 'trending', role: 'Strategist', desc: 'pick policy / operators / fidelity', tone: '#b0729e' },
  strategy_decision: { icon: 'trending', role: 'Strategist', desc: 'pick policy / operators / fidelity', tone: '#b0729e' },
  hypothesis_merge: { icon: 'confluence', role: 'Hypothesis merge', desc: 'fold paraphrase hypotheses', tone: '#5fa0a8' },
  deep_research:  { icon: 'search', role: 'Deep research', desc: 'read the literature first', tone: '#6fb0a3' },
  lessons:        { icon: 'doc', role: 'Lessons', desc: 'reflect / distil cross-run lessons', tone: '#9a8fb0' },
  lessons_distill: { icon: 'doc', role: 'Lessons', desc: 'reflect / distil cross-run lessons', tone: '#9a8fb0' },
  lessons_refresh: { icon: 'doc', role: 'Lessons', desc: 'reflect / distil cross-run lessons', tone: '#9a8fb0' },
  novelty:        { icon: 'gitbranch', role: 'Novelty gate', desc: 'dedup near-duplicate proposals', tone: '#a89a6f' },
}
const stageMeta = (name) => STAGE[name] || { icon: 'dot', role: name, desc: '', tone: 'var(--accent)' }

function llmEvents(s) { return (s.events || []).filter(e => e.name === 'llm_call') }

// Compact info helpers so each trace row carries the data that DIFFERENTIATES it (langfuse/Phoenix
// convention: model · input→output tokens · a content preview), instead of a bare op name repeated.
const ktok = (n) => (n == null ? '' : (n >= 1000 ? +(n / 1000).toFixed(n >= 9950 ? 0 : 1) + 'k' : String(n)))
const shortModel = (m) => (m || '').split('/').pop()
function callTok(c) { const t = c.tokens || {}; return { in: t.prompt, out: t.completion, total: t.total || ((t.prompt || 0) + (t.completion || 0)) } }
// First meaningful line of the completion (what the call PRODUCED) — falls back to the last user
// message (what it was ASKED) so even an empty/streaming completion still reads as something.
function callPreview(c) {
  const firstLine = (s) => (s || '').trim().split('\n').map(l => l.trim()).find(Boolean) || ''
  const compl = firstLine(c.completion)
  if (compl) return compl
  const lastUser = [...(c.prompt || [])].reverse().find(m => m.role === 'user')
  return firstLine(lastUser && lastUser.content)
}
// Roll the whole subtree of a span up to "how many model calls and how many tokens it cost" — shown on
// the stage/span header so you see the expensive steps without expanding anything. Counts first-class
// GENERATION spans (kind), and legacy `llm_call` events (older runs) so both render.
function spanRollup(s) {
  // tok = SUM of every call's total (billed — a tool loop re-sends the growing context each turn, O(n²)).
  // ctx = the PEAK single prompt = the real context-window size. out = generated tokens. The UI shows
  // ctx + out (billed tok in the tooltip) so the number reads as "context", not the re-send sum.
  let calls = 0, tok = 0, ctx = 0, out = 0
  const walk = (x) => {
    if (x.kind === 'generation') { calls++; const u = (x.attributes || {}).usage || {}; const p = u.prompt || 0; tok += (u.total != null ? u.total : p + (u.completion || 0)); ctx = Math.max(ctx, p); out += u.completion || 0 }
    ;(x.events || []).forEach(e => { if (e.name === 'llm_call') { calls++; const t = callTok(e); tok += t.total || 0; ctx = Math.max(ctx, t.in || 0); out += t.out || 0 } })
    ;(x.children || []).forEach(walk)
  }
  walk(s)
  return { calls, tok, ctx, out }
}

// Adapt a first-class GENERATION span (kind='generation', I/O held in attributes) to the same
// {op,model,prompt,completion,tokens,thinking,tool_calls} shape the legacy llm_call renderer uses —
// so a generation span and an old llm_call event display identically.
function genToCall(s) {
  const a = s.attributes || {}, u = a.usage || {}
  return {
    op: a.op, model: a.model, prompt: a.input || [],
    completion: typeof a.output === 'string' ? a.output : (a.output != null ? JSON.stringify(a.output, null, 2) : ''),
    thinking: a.thinking, tool_calls: a.tool_calls, model_parameters: a.model_parameters, cost: a.cost,
    tokens: { prompt: u.prompt, completion: u.completion, total: u.total },
  }
}
const asText = (v) => v == null ? '' : (typeof v === 'string' ? v : JSON.stringify(v, null, 2))

// The expandable body of a generation: the INPUT (prompt messages) and the OUTPUT (the model's text),
// plus a collapsed reasoning disclosure. Tool CALLS are NOT shown here — they render as their own
// indented tool observations directly beneath this chat (no duplication); when a turn produced only
// tool calls, its output is empty and we say so, pointing at the tools below.
function GenBody({ c }) {
  const [think, setThink] = useState(false)
  const nTools = (c.tool_calls || []).length
  return <div className="llm-io">
    {(c.model || c.model_parameters || c.cost != null) && <div className="kv">
      {c.model && <KV k="model" v={c.model} />}
      {c.model_parameters && <KV k="params" v={JSON.stringify(c.model_parameters)} />}
      {c.cost ? <KV k="cost" v={'$' + c.cost} /> : null}</div>}
    <div className="gen-sec-h">input</div>
    {(c.prompt || []).map((m, i) => <div key={i} className="msg">
      <div className={'msg-role role-' + (m.role || 'user')}>{m.role}</div>
      <pre className="code">{m.content}</pre></div>)}
    <div className="gen-sec-h">output</div>
    {c.completion
      ? <div className="msg"><pre className="code">{c.completion}</pre></div>
      : <div className="muted" style={{ fontSize: 12, padding: '2px 2px 4px' }}>
          {nTools ? `→ called ${nTools} tool${nTools > 1 ? 's' : ''} (shown below)` : '(no text output)'}</div>}
    {c.thinking && <div className="msg think-debug">
      <button type="button" className="msg-role role-think disclosure-button" aria-expanded={think}
        onClick={() => setThink(v => !v)}>{think ? '▾' : '▸'} reasoning (debug)</button>
      {think && <Markdown className="think-body" text={c.thinking} />}</div>}
  </div>
}

// Render a list of sibling spans. Two behaviours:
//  • INDENT each tool observation one level under the generation before it — in the tool-loop the
//    sequence is (chat → tool → tool → chat → …), so a tool belongs to the last chat, making "which
//    chat called this tool" obvious without re-parenting the trace.
//  • CAP how many are rendered at once (a heavily-repaired node can have 800+ spans — rendering them
//    all freezes the browser / black screen). Show the first SPAN_CAP, then a "show N more" button;
//    the rest (and every span's full I/O) are always one click away — nothing is lost.
const SPAN_CAP = 60
function SpanList({ items, depth, t0, total, runId, parentOp = null }) {
  const [all, setAll] = useState(false)
  const rows = []
  let genDepth = null
  ;(items || []).forEach((c, i) => {
    const kind = c.kind || 'operation'
    if (kind === 'tool' && genDepth != null) { rows.push({ c, d: genDepth + 1, i }) }
    else { rows.push({ c, d: depth, i }); genDepth = (kind === 'generation') ? depth : null }
  })
  const shown = all ? rows : rows.slice(0, SPAN_CAP)
  return <>
    {shown.map(({ c, d, i }) => <SpanRow key={i} s={c} depth={d} t0={t0} total={total} runId={runId} parentOp={parentOp} />)}
    {!all && rows.length > SPAN_CAP && <button className="span-more" style={{ marginLeft: depth * 14 + 4 }}
      onClick={() => setAll(true)}>… show {rows.length - SPAN_CAP} more observations</button>}
  </>
}

// One span and its subtree, drawn as a langfuse-style waterfall row: the bar is positioned by the
// span's OFFSET from the trace start (t0) and sized by its duration, so sequence reads at a glance.
// Renders three observation kinds distinctly — GENERATION (an LLM call: op·model·in→out·preview, its
// prompt/output on expand), TOOL (name·arg, its input/output on expand), and OPERATION (a phase of
// work) — so the tree shows exactly what called what and what each produced. Nothing is truncated.
function SpanRow({ s, depth, t0, total, runId, parentOp = null }) {
  const [open, setOpen] = useState(false)
  const [io, setIo] = useState(null)   // lazily-fetched FULL i/o for a generation/tool (Langfuse-style)
  const kind = s.kind || 'operation'
  const err = s.status === 'ERROR'
  const off = (typeof s.start === 'number') ? Math.max(0, (s.start - t0) / total * 100) : 0
  const wid = Math.max(1.5, (s.duration_s || 0) / total * 100)
  const barTone = err ? 'var(--fail)' : kind === 'generation' ? 'var(--accent)' : kind === 'tool' ? 'var(--working)' : stageMeta(s.name).tone
  const bar = <span className="span-bar"><span className="span-fill" style={{ marginLeft: Math.min(98, off) + '%', width: wid + '%', background: barTone }} /></span>
  const kids = <SpanList items={s.children} depth={depth + 1} t0={t0} total={total} runId={runId} parentOp={s.name} />
  // On first expand of a generation/tool, pull the full (uncapped) input/output on demand — the tree
  // is served light so a long run stays fast, and NO information is lost (full text fetched here).
  useEffect(() => {
    if (open && io === null && runId && s.span_id && (kind === 'generation' || kind === 'tool')) {
      let on = true
      spanDetail(runId, s.span_id).then(d => on && setIo((d && d.attributes) || {})).catch(() => on && setIo({}))
      return () => { on = false }
    }
  }, [open])

  if (kind === 'generation') {
    // Row header from the LIGHT span (op·model·tokens); the prompt/output come from the fetched `io`.
    const a = { ...(s.attributes || {}), ...(io || {}) }
    const c = genToCall({ ...s, attributes: a }), t = callTok(c)
    return <>
      <button type="button" aria-expanded={open} className={'span-row gen disclosure-button' + (err ? ' err' : '')}
        style={{ paddingLeft: depth * 14 }} onClick={() => setOpen(o => !o)} title="expand for prompt & output">
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        {(() => {   // name the call by ROLE so "who writes code" is unmistakable: the Developer's LLM
          // call (under implement/repair) is "writing code"; the Researcher's (under propose) is "reasoning".
          const dev = parentOp === 'implement' || parentOp === 'repair'
          const label = dev ? 'writing code' : (parentOp === 'propose' && a.op === 'chat' ? 'reasoning' : (a.op || 'llm'))
          return <span className="span-name gen"><OpIcon name={dev ? 'pencil' : 'bulb'} className="t-ic" /> <span className={'llm-op' + (dev ? ' dev-code' : '')}>{label}</span>{a.model && <span className="llm-model" title={a.model}>{shortModel(a.model)}</span>}</span>
        })()}
        {bar}
        <span className="t">{fmt(s.duration_s, 3)}s</span>
        {(t.in != null || t.out != null) && <span className="badge" title={`${t.in || 0} prompt → ${t.out || 0} completion tokens`}>{ktok(t.in)}→{ktok(t.out)}</span>}
        {err && <span className="badge reason">ERROR</span>}
      </button>
      {open && <div className="span-detail" style={{ marginLeft: depth * 14 + 16 }}>
        {io === null ? <div className="muted" style={{ fontSize: 12 }}>loading…</div> : <GenBody c={c} />}</div>}
      {kids}
    </>
  }
  if (kind === 'tool') {
    const a = { ...(s.attributes || {}), ...(io || {}) }
    const inp = asText(a.input), outp = asText(a.output), name = (s.attributes || {}).tool || a.tool || 'tool'
    return <>
      <button type="button" aria-expanded={open} className={'span-row tool disclosure-button' + (err ? ' err' : '')}
        style={{ paddingLeft: depth * 14 }} onClick={() => setOpen(o => !o)} title="expand for input & output">
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        <span className="span-name tool"><OpIcon name="gear" className="t-ic" /> <b className="tool-name">{name}</b></span>
        {bar}
        <span className="t">{fmt(s.duration_s, 3)}s</span>
        {err && <span className="badge reason">ERROR</span>}
      </button>
      {open && <div className="span-detail" style={{ marginLeft: depth * 14 + 16 }}>
        {io === null ? <div className="muted" style={{ fontSize: 12 }}>loading…</div> : <>
          {inp && <div className="msg"><div className="msg-role role-user">input</div><pre className="code">{inp}</pre></div>}
          {outp && <div className="msg"><div className="msg-role role-completion">output</div><pre className="code">{outp}</pre></div>}
          {!inp && !outp && <div className="muted" style={{ fontSize: 12 }}>(no input/output recorded)</div>}</>}
      </div>}
      {kids}
    </>
  }
  // OPERATION span (a phase of work): its attributes, non-llm events, + legacy llm_call events (old runs).
  const attrs = Object.entries(s.attributes || {}).filter(([k]) => k !== 'node_id')
  const events = (s.events || []).filter(e => e.name !== 'llm_call')
  const calls = llmEvents(s)
  const m = stageMeta(s.name)
  const detail = attrs.length || events.length || calls.length
  const OperationHeader = detail ? 'button' : 'div'
  return <>
    <OperationHeader type={detail ? 'button' : undefined} aria-expanded={detail ? open : undefined}
         className={'span-row' + (detail ? ' disclosure-button' : '') + (err ? ' err' : '')}
         style={{ paddingLeft: depth * 14 }} onClick={detail ? () => setOpen(o => !o) : undefined}
         title={detail ? 'click for step detail' : ''}>
      <span className="span-tw">{detail ? (open ? '▾' : '▸') : '·'}</span>
      <span className="span-name" title={m.desc}><OpIcon name={m.icon} className="t-ic" /> {m.role !== s.name ? m.role : s.name}</span>
      {bar}
      <span className="t">{fmt(s.duration_s, 3)}s</span>
      {calls.length > 0 && (() => { const tok = calls.reduce((a, c) => a + (callTok(c).total || 0), 0)
        return <span className="badge" title="model calls in this step — expand to read prompt & completion">{calls.length}×LLM{tok ? ` · ${ktok(tok)}` : ''}</span> })()}
      {err && <span className="badge reason">ERROR</span>}
    </OperationHeader>
    {open && detail && <div className="span-detail" style={{ marginLeft: depth * 14 + 16 }}>
      {attrs.length > 0 && <div className="kv">{attrs.map(([k, v]) =>
        <KV key={k} k={k} v={typeof v === 'object' ? JSON.stringify(v) : String(v)} />)}</div>}
      {events.map((e, i) => <div key={i} className="span-ev">
        <span className="ty">{e.name}</span>{e.error ? <span className="flag"> {e.error}</span> :
          <span className="muted"> {Object.entries(e).filter(([k]) => k !== 'name').map(([k, v]) => `${k}=${v}`).join(' ')}</span>}
      </div>)}
      {calls.map((c, i) => <LlmCall key={i} call={{ ...c, span: s.name }} idx={i} />)}
    </div>}
    {kids}
  </>
}

// One LLM call as a COMPACT, information-dense row (the langfuse "generation" line): op · model ·
// in→out tokens · #prompt-msgs · 🧠 · a one-line content preview — so repeated calls in a loop read
// as distinct steps, not "chat / chat / chat". Click to expand the full prompt / completion / reasoning.
export function LlmCall({ call, idx }) {
  const [open, setOpen] = useState(idx === 0)   // first call expanded by default
  const [think, setThink] = useState(false)     // raw reasoning is debug-only — collapsed by default
  const t = callTok(call)
  const msgs = (call.prompt || []).length
  const preview = callPreview(call)
  return <div className={'llm-row' + (open ? ' open' : '')}>
    <button type="button" className="llm-line disclosure-button" aria-expanded={open}
      onClick={() => setOpen(o => !o)} title={preview || 'expand for prompt & completion'}>
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      {typeof idx === 'number' && <span className="llm-i">{idx + 1}</span>}
      <span className="llm-op">{call.op || 'llm'}</span>
      {call.model && <span className="llm-model" title={call.model}>{shortModel(call.model)}</span>}
      {(t.in != null || t.out != null) && <span className="llm-tok" title={`${t.in || 0} prompt → ${t.out || 0} completion tokens`}>{ktok(t.in)}→{ktok(t.out)}</span>}
      {msgs > 2 && <span className="llm-msgs" title={`${msgs} messages in the prompt (context size)`}>{msgs}m</span>}
      {call.thinking && <span className="llm-think" title="model reasoning captured"><OpIcon name="bulb" /></span>}
      {preview && <span className="llm-prev">{preview}</span>}
    </button>
    {open && <div className="llm-io">
      {(call.prompt || []).map((m, i) => <div key={i} className="msg">
        <div className={'msg-role role-' + (m.role || 'user')}>{m.role}</div>
        <pre className="code">{m.content}</pre>
      </div>)}
      <div className="msg">
        <div className="msg-role role-completion">completion</div>
        <pre className="code">{call.completion || '(empty)'}</pre>
      </div>
      {/* Raw <think> chain-of-thought: a debug aid only, kept collapsed so the clean answer above
          stays the primary view. The conclusion is what matters; this is how it got there. */}
      {call.thinking && <div className="msg think-debug">
        <button type="button" className="msg-role role-think disclosure-button" aria-expanded={think}
          onClick={() => setThink(v => !v)}>
          {think ? '▾' : '▸'} reasoning (debug)
        </button>
        {think && <Markdown className="think-body" text={call.thinking} />}
      </div>}
    </div>}
  </div>
}

// A top-level lifecycle stage (one root span = one phase of work on this node), with its sub-steps.
// The header rolls up the stage's model-call count + token cost so the expensive phases stand out.
function StageBlock({ s, t0, total, runId }) {
  const m = stageMeta(s.name)
  const roll = spanRollup(s)
  return <div className={'stage' + (s.status === 'ERROR' ? ' err' : '')}>
    <div className="stage-h" title={m.desc}>
      <span className="stage-ic"><OpIcon name={m.icon} /></span>
      <b>{m.role}</b>
      {roll.calls > 0 && <span className="stage-roll" title={`${roll.calls} model call(s) · context peaked at ~${roll.ctx} tokens, generated ~${roll.out} · ${roll.tok} billed (context re-sent each turn)`}>{roll.calls} call{roll.calls > 1 ? 's' : ''}{roll.ctx ? ` · ${ktok(roll.ctx)} ctx` : ''}{roll.out ? ` · ${ktok(roll.out)} out` : ''}</span>}
      <span className="spacer" style={{ flex: 1 }} />
      <span className="t">{fmt(s.duration_s, 3)}s</span>
    </div>
    <div className="spans">
      {(s.children || []).length
        ? <SpanList items={s.children} depth={0} t0={t0} total={total} runId={runId} />
        : <SpanRow s={s} depth={0} t0={t0} total={total} runId={runId} />}
    </div>
  </div>
}

// Reusable langfuse-style trace for ONE node's span forest — the lifecycle stages on a shared
// timeline. Exported so the chat feed can show the same waterfall inline (Dock.jsx) as the Inspector.
export function NodeTrace({ spans, runId }) {
  const roots = spans || []
  if (!roots.length) return <div className="muted" style={{ fontSize: 12 }}>No LLM/execution spans captured for this node yet.</div>
  const { t0, total } = traceBounds(roots)
  return <div className="trace">{roots.map((s, i) => <StageBlock key={i} s={s} t0={t0} total={total} runId={runId} />)}</div>
}

// The coding-agent's own validation report (was its own tab) — folded into the lifecycle as the
// Developer stage's verification footnote, only when an external agent actually wrote the node.
function AgentReport({ r }) {
  return <div className="stage">
    <div className="stage-h">
      <span className="stage-ic" style={{ color: r.ok && !r.fell_back ? 'var(--ok)' : r.fell_back ? 'var(--working)' : 'var(--fail)' }}>
        <OpIcon name={r.ok && !r.fell_back ? 'check' : r.fell_back ? 'replay' : 'cross'} /></span>
      <b>Developer · agent validation</b>
      <span className="muted">{r.fell_back ? 'fell back to template' : r.ok ? 'shipped clean' : 'failed checks'}</span>
      <span className="spacer" style={{ flex: 1 }} />
      <span className="muted">{r.attempts} attempt{r.attempts === 1 ? '' : 's'}</span>
    </div>
    <DataTable caption="Agent attempt validation checks" card={false}><table className="tbl"><thead><tr><th>check</th><th>ok</th><th>detail</th></tr></thead>
      <tbody>{(r.checks || []).map((c, i) => <tr key={i}>
        <td>{c.name}</td><td style={{ color: c.ok ? 'var(--ok)' : 'var(--fail)' }}>{c.ok ? '✓' : '✗'}</td>
        <td className="muted">{c.detail || c.severity || ''}</td></tr>)}</tbody></table></DataTable>
  </div>
}

// ── linear conversation view ─────────────────────────────────────────────────────────────────────
// The raw span tree re-shows the WHOLE re-sent message list on every generation (a tool-loop re-sends
// the growing history each turn → the system+user prompt and every prior turn duplicate N times). The
// conversation view reconstructs the loop as a readable thread: the request once per sub-loop, then
// each generation's DELTA (reasoning + text + tool calls) interleaved with the tool executions.
function ConvRequest({ t }) {
  const [open, setOpen] = useState(false)   // system prompt is big — collapsed by default
  const roles = (t.messages || []).map(m => m.role).join(' + ')
  return <div className="conv-req">
    <button type="button" className="conv-req-h disclosure-button" aria-expanded={open}
      onClick={() => setOpen(o => !o)} title="the system + user prompt for this sub-loop (shown once)">
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      <OpIcon name="chat" className="t-ic" /> <b>request</b>
      {t.label && <span className="llm-op">{t.label}</span>}
      <span className="muted conv-req-roles"> {roles}</span>
    </button>
    {open && <div className="conv-req-body">
      {(t.messages || []).map((m, i) => <div key={i} className="msg">
        <div className={'msg-role role-' + (m.role || 'user')}>{m.role}</div>
        <pre className="code">{m.content}</pre></div>)}
    </div>}
  </div>
}

function ConvGen({ t }) {
  const [think, setThink] = useState(false)
  const calls = t.tool_calls || []
  const u = t.usage || {}
  const tok = u.total || (u.prompt || 0) + (u.completion || 0)
  // strip the trailing "[tool_calls: …]" marker — the calls are their own chip + the tool rows below
  const text = (t.output || '').replace(/\n*\[tool_calls:[^\]]*\]\s*$/, '').trim()
  return <div className={'conv-gen' + (t.status === 'ERROR' ? ' err' : '')}>
    <div className="conv-gen-h">
      <OpIcon name="bulb" className="t-ic" />
      {t.model && <span className="llm-model" title={t.model}>{shortModel(t.model)}</span>}
      {tok ? <span className="badge" title={`${u.prompt || 0} prompt → ${u.completion || 0} completion tokens`}>{ktok(tok)} tok</span> : null}
      {t.seconds != null && <span className="t">{fmt(t.seconds, 2)}s</span>}
      {t.status === 'ERROR' && <span className="badge reason">ERROR</span>}
    </div>
    {t.think && <div className="msg think-debug">
      <button type="button" className="msg-role role-think disclosure-button" aria-expanded={think}
        onClick={() => setThink(v => !v)}>{think ? '▾' : '▸'} thinking</button>
      {think && <Markdown className="think-body" text={t.think} />}</div>}
    {text && <div className="conv-out"><Markdown text={text} /></div>}
    {calls.length > 0 && <div className="conv-calls muted">→ called {calls.join(', ')}</div>}
    {!text && !t.think && calls.length === 0 && <div className="muted" style={{ fontSize: 12 }}>(no output)</div>}
  </div>
}

function ConvTool({ t }) {
  const [open, setOpen] = useState(false)
  const err = t.status === 'ERROR'
  return <div className={'conv-tool' + (err ? ' err' : '')}>
    <button type="button" className="conv-tool-h disclosure-button" aria-expanded={open}
      onClick={() => setOpen(o => !o)} title="tool call — expand for input & output">
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      <OpIcon name="gear" className="t-ic" /> <b className="tool-name">{t.name}</b>
      {!open && t.input && <span className="muted conv-tool-prev"> {t.input.slice(0, 60)}</span>}
      {err && <span className="badge reason">ERROR</span>}
      {t.seconds != null && <span className="t">{fmt(t.seconds, 2)}s</span>}
    </button>
    {open && <div className="conv-tool-body">
      {t.input && <div className="msg"><div className="msg-role role-user">input</div><pre className="code">{t.input}</pre></div>}
      {t.output && <div className="msg"><div className="msg-role role-completion">output</div><pre className="code">{t.output}</pre></div>}
      {!t.input && !t.output && <div className="muted" style={{ fontSize: 12 }}>(no input/output recorded)</div>}
    </div>}
  </div>
}

// The live stdout/stderr of a stage's subprocess (training epochs, eval scoring), rendered INSIDE its
// trace band. Auto-scrolls to the newest line while the stage is live so a running train tails itself.
function StageLog({ text, live }) {
  const ref = useRef(null)
  const shown = text.length > 40000 ? text.slice(-40000) : text
  // Auto-tail while live, but ONLY if the user is already parked near the bottom — otherwise scrolling
  // up to read an earlier epoch would be yanked back down on every 4s poll (no follow-toggle here).
  useEffect(() => {
    const el = ref.current
    if (live && el && el.scrollHeight - el.scrollTop - el.clientHeight < 40) el.scrollTop = el.scrollHeight
  }, [text, live])
  return <div style={{ margin: '4px 0 2px' }}>
    <div className="muted" style={{ fontSize: 10, marginBottom: 2 }}>📄 stage log{live ? ' · live' : ''}</div>
    <pre ref={ref} className="training-log" style={{
      maxHeight: 320, overflow: 'auto', background: '#0b0e14', border: '1px solid #20252f', borderRadius: 6,
      padding: 8, fontSize: 11, lineHeight: 1.4, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{shown}</pre>
  </div>
}

function ConvStage({ st, defaultOpen = true, log = '', live = false }) {
  const m = stageMeta(st.label)
  const [open, setOpen] = useState(defaultOpen)
  const roll = st.rollup || {}
  const tk = roll.tokens || {}
  const nTurns = (st.turns || []).length
  const err = st.status === 'ERROR'
  // Colour-band the stage by its tone: a left rail + a tinted header, so foresight/strategy/researcher/
  // developer/eval read as distinct bands. Click the header to collapse the whole band.
  return <div className={'stage' + (err ? ' err' : '')}
              style={{ borderLeft: `3px solid ${err ? 'var(--fail)' : m.tone}` }}>
    <button type="button" className="stage-h disclosure-button" aria-expanded={open}
         title={m.desc + ' — click to collapse'} onClick={() => setOpen(o => !o)}
         style={{ cursor: 'pointer', background: err ? undefined : `color-mix(in srgb, ${m.tone} 12%, transparent)` }}>
      <span className="stage-caret" style={{ opacity: 0.6, fontSize: 10, width: 10, display: 'inline-block' }}>{open ? '▾' : '▸'}</span>
      <span className="stage-ic" style={{ color: err ? 'var(--fail)' : m.tone }}><OpIcon name={m.icon} /></span>
      <b style={{ color: err ? 'var(--fail)' : m.tone }}>{m.role}</b>
      {(roll.generations || roll.tools) ? <span className="stage-roll"
          title={tk.total ? `context window peaked at ${tk.context || 0} tokens; the model generated ${tk.completion || 0}. Billed ${tk.total} total — a tool loop RE-SENDS the growing context every turn, so billed ≫ context.` : undefined}>
        {roll.generations || 0} turn{roll.generations === 1 ? '' : 's'}
        {roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? '' : 's'}` : ''}
        {tk.context ? ` · ${ktok(tk.context)} ctx` : ''}
        {tk.completion ? ` · ${ktok(tk.completion)} out` : ''}</span> : null}
      {!open && nTurns ? <span className="muted" style={{ marginLeft: 6, fontSize: 10 }}>· {nTurns} step{nTurns === 1 ? '' : 's'} hidden</span> : null}
    </button>
    {open && <div className="conv-turns">
      {(st.turns || []).map((t, j) => t.type === 'request' ? <ConvRequest key={j} t={t} />
        : t.type === 'tool' ? <ConvTool key={j} t={t} /> : <ConvGen key={j} t={t} />)}
      {log ? <StageLog text={log} live={live} /> : null}
    </div>}
  </div>
}

function Conversation({ n, runId, working, allOpen = true, reloadNonce = 0 }) {
  const [conv, setConv] = useState(null)
  const [logs, setLogs] = useState({})   // {eval, stages:{train,score,…}} — the live stage/eval logs
  useEffect(() => {
    setConv(null)   // node changed → clear before the first load (poll ticks below don't clear, so no flash)
    setLogs({})     // …likewise the logs, else B's stage bands briefly render A's log text
  }, [runId, n.id, working, reloadNonce])
  usePoll((alive) => {
    nodeConversation(runId, n.id).then(d => alive() && setConv(d || { stages: [] })).catch(() => alive() && setConv({ stages: [] }))
    // Stage/eval logs ride ALONGSIDE the trace now (moved out of the old Training tab): each stage
    // band renders its own live log inside it, so opening "Train" shows the training output in place.
    get(`/api/runs/${runId}/nodes/${n.id}/logs`).then(d => alive() && setLogs(d || {})).catch(() => {})
  }, working ? 4000 : null,   // interval only while the agent works this node (live-refresh); null = load once
  [runId, n.id, working, reloadNonce])   // reloadNonce bumps after a "clear trace" so the band list refreshes
  if (conv === null) return <div className="muted" style={{ fontSize: 12 }}>loading…</div>
  const stages = conv.stages || []
  if (!stages.length) return <div className="muted">No conversation captured for this node yet.</div>
  // The live log for a stage band: a multi-stage eval logs per stage (stages[label]); a single-command
  // eval logs to eval.log ("evaluate"/"command"); the dep-install step to setup.log. Anything else
  // (propose/implement/…) has no subprocess log.
  const logFor = (label) => (logs.stages && logs.stages[label])
    || ({ setup: logs.setup, evaluate: logs.eval, command: logs.eval }[label]) || ''
  // `allOpen` is owned by the sticky Trace header (so collapse-all lives in the pinned bar). It's folded
  // into each band's key so a collapse/expand-all click remounts them at the new default; a live poll
  // (allOpen unchanged) keeps the key stable, so per-band toggles survive the 4s refresh.
  return <div className="conv">
    {stages.map((st, i) => <ConvStage key={`${st.trace_id || ''}:${st.label || ''}:${st.start || i}:${allOpen}`}
                                      st={st} defaultOpen={allOpen} log={logFor(st.label)} live={working} />)}
    {logs.run_setup ? <RunSetupLog text={logs.run_setup} /> : null}
  </div>
}

// The run-level, one-time dependency install (shared by every node) — moved out of the old Training
// tab; a collapsed footnote under the trace so a setup failure is still inspectable without its own tab.
function RunSetupLog({ text }) {
  const [open, setOpen] = useState(false)
  return <div className="stage" style={{ borderLeft: '3px solid var(--fg-mut)', marginTop: 4 }}>
    <button type="button" className="stage-h disclosure-button" aria-expanded={open}
      onClick={() => setOpen(o => !o)}>
      <span className="stage-caret" style={{ opacity: 0.6, fontSize: 10, width: 10, display: 'inline-block' }}>{open ? '▾' : '▸'}</span>
      <b className="muted">Run setup <span style={{ fontWeight: 400 }}>· deps install (run-level, once)</span></b>
    </button>
    {open && <div className="conv-turns"><StageLog text={text} live={false} /></div>}
  </div>
}

function Trace({ n, runId, live, working }) {
  const [view, setView] = useState('conversation')   // linear reading by default; raw tree on demand
  const [allOpen, setAllOpen] = useState(false)       // bands COLLAPSED by default (expand one to read it)
  const [nonce, setNonce] = useState(0)               // bumped after "clear trace" to reload the bands
  const [clearing, setClearing] = useState('')        // '' | 'confirm' | 'busy' | error message
  const bodyRef = useRef(null)
  const spans = n.trace?.nodes || []
  const agent = n.agent_report
  // Live status: what the node is doing RIGHT NOW. Two live states: an LLM authoring the code
  // (building → writing / repairing / merging), or the sandbox running its eval pipeline (pending →
  // training / scoring). `_op` is only set in the building case (the eval has no operator), so it
  // cleanly disambiguates the two.
  const building = working && live?.building?.node_id === n.id
  const _op = building ? (live.building.operator || '') : ''
  const statusLabel = !working ? null
    : building
      ? (/repair|debug/.test(_op) ? '🔧 repairing…' : /merge/.test(_op) ? '🔀 merging…' : '✍️ writing code…')
      : '🏋️ training / evaluating…'
  const status = statusLabel && <div className="trace-live-status"><span className="tls-dot" />{statusLabel}
    <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>live — updates on its own</span></div>
  const scrollTo = (where) => { const c = bodyRef.current?.closest('.insp-body'); if (c) c.scrollTop = where === 'top' ? 0 : c.scrollHeight }
  const doClear = async () => {
    setClearing('busy')
    try {
      await clearNodeTrace(runId, n.id)
      setNonce(x => x + 1)          // reload the Conversation bands (now empty until a rebuild re-traces)
      setClearing('')
    } catch (e) {
      // 409 while the engine is live is the common case — surface the server's reason inline.
      setClearing(/live/i.test(e.message) ? 'stop the run first' : ('clear failed: ' + e.message))
      setTimeout(() => setClearing(''), 4000)
    }
  }
  // "Clear trace" erases this node's spans (spans.jsonl is append-only, so a reset+rebuild would else
  // STACK new bands on the old attempt's). Two-click confirm; disabled while THIS node is being worked.
  const clearBtn = <span className="trace-clear">
    {clearing === '' && <button className="seg" title="erase this node's captured trace (spans) — useful before re-running the node so the new trace replaces the old"
      onClick={() => setClearing('confirm')} disabled={working}>✕ clear trace</button>}
    {clearing === 'confirm' && <>
      <button className="seg on" title="confirm: erase this node's spans" onClick={doClear}>✕ confirm clear</button>
      <button className="seg" onClick={() => setClearing('')}>cancel</button></>}
    {clearing === 'busy' && <span className="muted" style={{ fontSize: 11 }}>clearing…</span>}
    {clearing && clearing !== 'confirm' && clearing !== 'busy' &&
      <span className="muted" style={{ fontSize: 11, color: 'var(--fail)' }}>{clearing}</span>}
  </span>
  const nav = <span className="trace-nav">
    <button className="seg" aria-label="Scroll trace to top" title="scroll to top" onClick={() => scrollTo('top')}>↑</button>
    <button className="seg" aria-label="Scroll trace to newest" title="scroll to newest (bottom)" onClick={() => scrollTo('bottom')}>↓</button></span>
  // STICKY control bar: pinned to the top of the scroll area (position:sticky in .trace-head) so the view
  // toggle / collapse-all / scroll nav stay reachable while you page through a long trace, instead of
  // scrolling off the top. collapse-all is shown only for the conversation view (it acts on the bands).
  const head = <div className="trace-head">
    {status}
    <div className="conv-toggle">
      <button aria-pressed={view === 'conversation'} className={'seg' + (view === 'conversation' ? ' on' : '')} onClick={() => setView('conversation')}
        title="Linear, de-duplicated reading: request once, then each turn's reasoning + tools">conversation</button>
      <button aria-pressed={view === 'raw'} className={'seg' + (view === 'raw' ? ' on' : '')} onClick={() => setView('raw')}
        title="The raw span tree with each generation's full re-sent message list">raw spans</button>
      {view === 'conversation' && <button className="seg" aria-pressed={allOpen} style={{ fontSize: 10 }} title="collapse or expand every stage"
        onClick={() => setAllOpen(o => !o)}>{allOpen ? '⊟ collapse all' : '⊞ expand all'}</button>}
      <span style={{ flex: 1 }} />{clearBtn}{nav}
    </div>
  </div>
  if (!spans.length && !agent) {
    // While the agent is WORKING this node, node_detail's trace may still be empty (its create_node
    // root span hasn't closed) — but /conversation rebuilds LIVE from the sub-spans that have already
    // flushed. So mount the live-polling Conversation (it refreshes every 4s) instead of a dead
    // placeholder: steps now appear as each generation/tool completes, not all at once at the end.
    if (working)
      return <div className="trace" ref={bodyRef}>{head}<Conversation n={n} runId={runId} working={working} allOpen={allOpen} reloadNonce={nonce} /></div>
    return <div className="trace" ref={bodyRef}>{head}<div className="muted">No execution spans for this node yet — toy/offline nodes have minimal spans, and a node still in progress fills its trace as it runs.</div></div>
  }
  if (view === 'conversation')
    return <div className="trace" ref={bodyRef}>{head}<Conversation n={n} runId={runId} working={working} allOpen={allOpen} reloadNonce={nonce} />
      {agent && <AgentReport r={agent} />}</div>
  const { t0, total } = traceBounds(spans)
  // create_node already nests propose→implement; if an agent wrote the node, the report belongs
  // right after that authoring stage (placed by index), otherwise it trails the whole lifecycle.
  const authorIdx = spans.findIndex(s => ['create_node', 'implement', 'repair'].includes(s.name))
  const roll = n.trace?.rollup || {}
  const rtok = roll.tokens || {}
  return <div className="trace" ref={bodyRef}>
    {head}
    <div className="muted" style={{ marginBottom: 10 }}>
      Lifecycle of node #{n.id} — each part on a shared timeline (offset = when it ran, bar = how long).
      Expand any observation to read its input &amp; output.
      {(roll.generations || roll.tools) ? <span className="trace-totals"
          title={rtok.total ? `context window peaked at ${rtok.context || 0} tokens; the model generated ${rtok.completion || 0}. Billed ${rtok.total} total — each turn RE-SENDS the growing context, so billed ≫ context.` : undefined}>
        {' · '}{roll.generations || 0} generation{roll.generations === 1 ? '' : 's'}
        {roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? '' : 's'}` : ''}
        {rtok.context ? ` · ${ktok(rtok.context)} ctx` : ''}
        {rtok.completion ? ` · ${ktok(rtok.completion)} out` : ''}
        {roll.cost ? ` · $${roll.cost}` : ''}
      </span> : null}
    </div>
    {spans.map((s, i) => <React.Fragment key={i}>
      <StageBlock s={s} t0={t0} total={total} runId={runId} />
      {agent && i === authorIdx && <AgentReport r={agent} />}
    </React.Fragment>)}
    {agent && authorIdx < 0 && <AgentReport r={agent} />}
  </div>
}

function Code({ n }) {
  const [diff, setDiff] = useState(false)
  const files = n.files || {}
  const codeDiff = useMemo(
    () => diff && n.parent_code != null ? diffLines(n.parent_code, n.code) : null,
    [diff, n.parent_code, n.code])
  return <>
    <div className="toolbar" style={{ marginBottom: 8 }}>
      {n.parent_code != null && <button className={'btn sm' + (diff ? ' primary' : '')} onClick={() => setDiff(d => !d)}>diff vs parent #{n.parent_id_diffed}</button>}
    </div>
    {codeDiff
      ? <CodeViewer diff={codeDiff} copyText={n.code || ''} label={`Node ${n.id} diff`} />
      : <CodeViewer code={n.code || '(no solution.py — repo task or no code)'} label={`Node ${n.id} code`} />}
    {Object.keys(files).length > 0 && <>
      <div className="section-h">Helper files <span className="pill">{Object.keys(files).length}</span></div>
      {Object.entries(files).map(([fn, c]) => <div key={fn}><div className="muted" style={{ marginTop: 6 }}>{fn}</div><CodeViewer code={c} label={fn} maxHeight={300} /></div>)}
    </>}
  </>
}

// Live online metric curves (loss, recall@k, lr, grad norms, …) read from the node's TensorBoard
// events via the metrics adapters. Polls while the node is still running so the curves fill in as
// training progresses; keyed on n.status so a repair-retrain (pending→failed→pending) re-arms the poll.
function MetricCurves({ runId, nodeId, status }) {
  const [metrics, setMetrics] = useState({})
  const done = ['evaluated', 'failed', 'confirmed'].includes(status)
  useEffect(() => {
    if (nodeId == null) return
    setMetrics({})    // node changed → drop the previous node's curves before the first fetch resolves
  }, [runId, nodeId, done])
  usePoll((alive) => get(`/api/runs/${runId}/nodes/${nodeId}/metrics`)
    .then(d => alive() && setMetrics((d && d.metrics) || {})).catch(() => {}),
    done ? 15000 : 3000, [runId, nodeId, done], { enabled: nodeId != null })
  return <MetricLines series={metrics} />
}

function Metrics({ n, detail, state, runId }) {
  const seeds = detail?.confirm_seeds_detail || {}
  const vals = Object.entries(seeds).map(([s, v]) => ({ s: Number(s), v })).filter(x => x.v != null).sort((a, b) => a.s - b.s)
  // Every metric reported anywhere in the run (the objective ★ + all auto-captured extras), shown for
  // THIS node and for the champion (the run's best node), so "the metrics you wanted to see overall"
  // are all visible + comparable. Only the objective drives selection; extras are audit-only.
  const nodes = Object.values(state?.nodes || {})
  const extraKeys = [...new Set(nodes.flatMap(x => Object.keys(x.extra_metrics || {})))]
  const champ = state?.best_node_id != null ? nodes.find(x => x.id === state.best_node_id) : null
  const showChamp = champ && champ.id !== n.id
  const rows = [
    { k: 'objective', mine: n.confirmed_mean ?? n.metric, best: champ ? (champ.confirmed_mean ?? champ.metric) : null, star: true },
    ...extraKeys.map(k => ({ k, mine: n.extra_metrics?.[k], best: champ?.extra_metrics?.[k] })),
  ]
  return <>
    <div className="section-h">Reported metrics{champ ? ` · best = #${champ.id}` : ''}</div>
    <DataTable caption="Node metric comparison" card={false}><table className="tbl"><thead><tr><th>metric</th><th>this node</th>{showChamp && <th>best #{champ.id}</th>}</tr></thead>
      <tbody>{rows.map(r => <tr key={r.k} className={r.star ? 'chosen-row' : ''}>
        <td>{r.star ? '★ ' : ''}{r.k}</td><td>{fmt(r.mine)}</td>
        {showChamp && <td>{fmt(r.best)}</td>}</tr>)}</tbody></table></DataTable>
    {n.confirmed_mean != null && <div className="kv" style={{ marginTop: 8 }}>
      <KV k="robust mean ± std" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} over ${n.confirmed_seeds || vals.length} seeds`} /></div>}
    {vals.length > 0 && <>
      <div className="section-h">Per-seed confirmation</div>
      <DataTable caption="Per-seed confirmation metrics" card={false}><table className="tbl"><thead><tr><th>seed</th><th>metric</th></tr></thead>
        <tbody>{vals.map(x => <tr key={x.s}><td>{x.s}</td><td>{fmt(x.v)}</td></tr>)}</tbody></table></DataTable>
    </>}
    <div className="section-h" style={{ marginTop: 12 }}>Metric curves
      <span className="muted" style={{ fontWeight: 400, marginLeft: 6 }}>· live, every logged scalar — collapsible by group</span></div>
    <MetricCurves runId={runId} nodeId={n.id} status={n.status} />
  </>
}

// Intra-node sweep trials: a sortable table of every config the node ran in-process, plus
// parallel-coords / scatter views. Trials aren't backend nodes, so the charts get pseudo-node
// adapters ({id, metric, idea:{params}, feasible}) — no charts.jsx change needed.
function Trials({ n, detail, state }) {
  const trials = detail?.trials ?? n.trials ?? []
  const summary = n.trials_summary
  const [sortKey, setSortKey] = useState('metric')
  const [sortDir, setSortDir] = useState(state.direction === 'min' ? 'asc' : 'desc')
  const [showAll, setShowAll] = useState(false)
  if (!trials.length) {
    return <div className="muted">{summary
      ? `Sweep of ${summary.count} trial(s) — loading full results…`
      : 'No trials recorded for this node.'}</div>
  }
  const dir = state.direction
  const params = Array.from(new Set(trials.flatMap(t => Object.keys(t.params || {}))))
  // best trial = best metric under direction (matches the node's scalar metric)
  let bestIdx = -1, bestV = null
  trials.forEach((t, i) => { if (t.metric != null && (bestV == null || (dir === 'min' ? t.metric < bestV : t.metric > bestV))) { bestV = t.metric; bestIdx = i } })
  const setSort = (k) => { if (k === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setSortKey(k); setSortDir('asc') } }
  const val = (t, k) => k === 'idx' ? t._i : k === 'metric' ? t.metric : k === 'seconds' ? t.seconds : t.params?.[k]
  const rowsAll = trials.map((t, i) => ({ ...t, _i: i })).sort((a, b) => {
    const av = val(a, sortKey), bv = val(b, sortKey)
    if (av == null) return 1; if (bv == null) return -1
    const cmp = (typeof av === 'number' && typeof bv === 'number') ? av - bv : String(av).localeCompare(String(bv))
    return sortDir === 'asc' ? cmp : -cmp
  })
  const CAP = 100
  const rows = showAll ? rowsAll : rowsAll.slice(0, CAP)
  const okN = trials.filter(t => t.metric != null).length
  const totSec = trials.reduce((s, t) => s + (t.seconds || 0), 0)
  // pseudo-nodes for the existing charts (they read n.idea?.params and n.confirmed_mean ?? n.metric)
  const pseudo = trials.map((t, i) => ({ id: i, metric: t.metric, confirmed_mean: null, idea: { params: t.params || {} }, feasible: t.metric != null }))
  const scatter = params.length
    ? trials.map((t, i) => ({ x: t.params?.[params[0]] ?? i, y: t.metric, feasible: t.metric != null, id: i })).filter(d => d.y != null)
    : []
  const Th = ({ k, children }) => <th aria-sort={sortKey === k ? (sortDir === 'asc' ? 'ascending' : 'descending') : undefined}>
    <button type="button" className="table-sort" onClick={() => setSort(k)}>
      {children}{sortKey === k && <span aria-hidden="true">{sortDir === 'asc' ? ' ▲' : ' ▼'}</span>}
    </button>
  </th>
  return <>
    <div className="kv">
      <KV k="trials" v={trials.length} />
      <KV k="best metric" v={`${fmt(bestV)}${bestIdx >= 0 ? ` (#${bestIdx})` : ''}`} />
      <KV k="ok / failed" v={`${okN} / ${trials.length - okN}`} />
      <KV k="Σ seconds" v={fmt(totSec)} />
    </div>
    {params.length > 0 && <>
      <div className="section-h">Params → metric</div>
      <ParallelCoords nodes={pseudo} direction={dir} height={220} />
    </>}
    {scatter.length > 0 && <>
      <div className="section-h">{params[0]} vs metric</div>
      <Scatter data={scatter} xlab={params[0]} ylab="metric" height={220} />
    </>}
    <div className="section-h">Trials <span className="pill">{trials.length}</span></div>
    <DataTable caption="Hyperparameter sweep trial results" card={false}><table className="tbl">
      <thead><tr><Th k="idx">#</Th>{params.map(p => <Th key={p} k={p}>{p}</Th>)}<Th k="metric">metric</Th><Th k="seconds">s</Th></tr></thead>
      <tbody>{rows.map(t => <tr key={t._i}
        className={t._i === bestIdx ? 'best-row' : ''}>
        <td>#{t._i}{t._i === bestIdx ? <OpIcon name="crown" size={10} /> : ''}</td>
        {params.map(p => <td key={p}>{t.params?.[p] != null ? fmt(t.params[p]) : '—'}</td>)}
        <td>{t.metric != null ? fmt(t.metric) : <span className="badge reason">{t.error ? 'error' : 'failed'}</span>}</td>
        <td className="muted">{fmt(t.seconds)}</td></tr>)}</tbody>
    </table></DataTable>
    {rowsAll.length > CAP && <button className="btn sm ghost" style={{ marginTop: 6 }} onClick={() => setShowAll(s => !s)}>
      {showAll ? 'show fewer' : `show all ${rowsAll.length}`}</button>}
  </>
}

function Trust({ n, drifts = [] }) {
  const feasibility = nodeFeasibilityStatus(n)
  const State = ({ tone, label, detail }) => <div className={`trust-state ${tone}`} role={tone === 'alarm' ? 'alert' : 'status'}>
    <OpIcon name={tone === 'alarm' ? 'alert' : tone === 'ok' ? 'check' : 'dot'} size={14} />
    <strong>{label}</strong><span>{detail}</span>
  </div>
  return <div className="inspector-trust">
    <div className="section-h">Robustness</div>
    {n.confirmed_mean != null
      ? <><State tone="ok" label="Multi-seed confirmed" detail={`${n.confirmed_seeds || 'Multiple'} successful seeds are recorded for this node.`} /><div className="kv">
        <KV k="single" v={fmt(n.metric)} />
        <KV k="robust mean" v={fmt(n.confirmed_mean)} />
        <KV k="std" v={fmt(n.confirmed_std)} />
        <KV k="seeds" v={n.confirmed_seeds} />
      </div></>
      : <State tone="warn" label="Single-evaluation only" detail="This node is not multi-seed confirmed and could be seed-lucky." />}
    <div className="section-h">Feasibility</div>
    <State {...feasibility} />
    {n.violations?.length
      ? <DataTable caption="Constraint violations" card={false}><table className="tbl"><thead><tr><th>constraint</th><th>value</th><th>bound</th></tr></thead>
        <tbody>{n.violations.map((v, i) => <tr key={i}><td className="flag">{v.name}</td><td>{fmt(v.value)}</td><td>{v.max != null ? `≤ ${fmt(v.max)}` : `≥ ${fmt(v.min)}`}</td></tr>)}</tbody></table>
      </DataTable>
      : null}
    <div className="section-h">Metric drift</div>
    {drifts.length
      ? <><State tone="alarm" label={`${drifts.length} divergence${drifts.length === 1 ? '' : 's'} recorded`} detail="The independent metric reader disagreed with the primary metric." /><DataTable caption="Metric drift cross-checks" card={false}><table className="tbl"><thead><tr><th>seed</th><th>primary</th><th>cross-check</th><th>tol</th></tr></thead>
        <tbody>{drifts.map((d, i) => <tr key={i}><td>{d.seed ?? '—'}</td><td className="flag">{fmt(d.primary)}</td><td>{fmt(d.cross)}</td><td className="muted">{fmt(d.tolerance)}</td></tr>)}</tbody></table>
        </DataTable>
      </>
      : <State tone="unknown" label="No drift flag recorded" detail="This does not prove that an independent cross-check ran for this node." />}
    {n.status === 'failed' && <><div className="section-h">Failure</div><span className="badge reason">{n.error_reason}</span><pre className="code">{n.error}</pre></>}
  </div>
}

function Agent({ n }) {
  const r = n.agent_report
  if (!r) return <div className="muted">Not produced by an external coding agent (templated/LLM developer).</div>
  return <>
    <div className="kv">
      <KV k="ok" v={String(r.ok)} />
      <KV k="fell back" v={String(r.fell_back)} />
      <KV k="attempts" v={r.attempts} />
      <KV k="shipped ok" v={String(r.shipped_ok)} />
    </div>
    <div className="section-h">Validation checks</div>
    <DataTable caption="Implementation validation checks" card={false}><table className="tbl"><thead><tr><th>check</th><th>ok</th><th>detail</th></tr></thead>
      <tbody>{(r.checks || []).map((c, i) => <tr key={i}>
        <td>{c.name}</td><td style={{ color: c.ok ? 'var(--ok)' : 'var(--fail)' }}>{c.ok ? '✓' : '✗'}</td>
        <td className="muted">{c.detail || c.severity || ''}</td></tr>)}</tbody></table></DataTable>
  </>
}

function Cost({ state }) {
  const c = state.llm_cost
  if (!c) return <div className="muted">No LLM cost recorded (offline/toy run, or run not finished).</div>
  return <div className="kv">
    <KV k="$ spent" v={fmt(c.cost)} />
    <KV k="calls" v={fmtInt(c.calls)} />
    <KV k="prompt tokens" v={fmtInt(c.prompt_tokens)} />
    <KV k="completion tokens" v={fmtInt(c.completion_tokens)} />
    <KV k="total tokens" v={fmtInt(c.total_tokens)} />
  </div>
}
