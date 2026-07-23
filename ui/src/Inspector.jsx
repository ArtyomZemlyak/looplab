import React, { useEffect, useMemo, useState, useRef } from 'react'
import { get, fmt, fmtInt, isSweep, spanDetail, nodeConversation, CONTROL, clearNodeTrace, commandFeedback,
  runNodeApiPath } from './util.js'
import { usePoll } from './hooks.js'
import { Trajectory, ParallelCoords, Scatter, MetricLines } from './charts.jsx'
import { themeFilteredGroupAggregate } from './grouping.js'
import { mergeSummary, nodeChip } from './report.js'
import { OpIcon } from './icons.jsx'
import Markdown from './markdown.jsx'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import { nodeFeasibilityStatus } from './trustSemantics.js'
import { reviewInspectorTabs } from './runRouteState.js'
import { DataTable, nextRovingIndex } from './accessibility.jsx'
import { traceDetailState, tracePartial, traceUnavailable, unavailableTraceDetail } from './traceProjection.js'
import { nodeTheme } from './conceptId.js'

// # CODEX AGENT: Comments are an explicit Inspector interaction. Keep their independently secured
// review transport out of the base DAG closure, then load the same component only when this tab opens.
const CommentsThread = React.lazy(() => import('./CommentsThread.jsx'))

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
    } catch { onToast?.(`Reset #${id} could not be submitted. Try again.`) }
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
  return <span ref={rootRef} className="reset-control">
    <button ref={triggerRef} className="ctx-chip ctx-chip-action"
            title="re-run THIS node in place (no new node) from a chosen stage"
            aria-haspopup="menu" aria-expanded={open} aria-disabled={busy} aria-busy={busy}
            onClick={() => { if (!busy) setOpen(!open) }}>{busy ? '↻ Resetting…' : '↻ Reset ▾'}</button>
    {open && <div ref={menuRef} role="menu" className="reset-stage-menu" aria-label={`Reset experiment ${id} from stage`}
      onKeyDown={onMenuKeyDown}
      onBlur={event => {
        if (event.relatedTarget !== triggerRef.current && !event.currentTarget.contains(event.relatedTarget)) setOpen(false)
      }}>
      {STAGES.map(([stage, label, desc]) =>
        <button type="button" role="menuitem" key={stage} className="reset-stage-option"
             tabIndex={-1} title={desc} onClick={() => doReset(stage)}>
          <span className="reset-option-title"><b>{label}</b> <span className="muted">from {stage}</span></span>
          <span className="muted reset-option-description">{desc}</span>
        </button>)}
    </div>}
  </span>
}

export default function Inspector({ runId, nodeId, state, live, tab, setTab, onToast, readOnly = false,
  historySeq = null, expectedGeneration = null, readOnlyReason = 'history', evidenceAvailable = true,
  commentsRevision = null, focusCommentId = null }) {
  const nodeAttempt = state?.nodes?.[nodeId]?.attempt
  const detailScope = `${runId}@${expectedGeneration || '?'}:${nodeId ?? '-'}:${nodeAttempt ?? '?'}:${readOnly
    ? historySeq ?? readOnlyReason : 'live'}:${evidenceAvailable ? 1 : 0}`
  const [detailResource, setDetailResource] = useState({ scope: null, data: null })
  const detailCurrent = detailResource.scope === detailScope
  const detail = detailCurrent ? detailResource.data : null
  // Accept a detail payload whose attempt is >= the summary's: the /nodes endpoint is often FRESHER
  // than the lagging run-state poll (e.g. right after an inline repair bumps `attempt`), and showing
  // the current truth is correct — only a genuinely STALER payload (an old attempt's late response)
  // should be rejected. Exact-only matching here flashed a spurious "attempt changed" error banner
  // during normal live repairs until the next poll reconciled.
  const detailMatchesAttempt = value => !Number.isSafeInteger(nodeAttempt)
    || (Number.isSafeInteger(value?.attempt) && value.attempt >= nodeAttempt)
  const detailMatchesNode = value => value != null && typeof value === 'object' && !Array.isArray(value)
    && String(value.id) === String(nodeId) && typeof value.status === 'string'
  const [detailStatus, setDetailStatus] = useState('idle')
  const [detailError, setDetailError] = useState('')
  const [detailNonce, setDetailNonce] = useState(0)
  const detailSurfaceRef = useRef(null)
  const detailFocusScopeRef = useRef(null)
  const retryDetail = () => {
    detailFocusScopeRef.current = detailScope
    setDetailNonce(value => value + 1)
  }
  useEffect(() => {
    setDetailResource({ scope: detailScope, data: null })
    setDetailError('')
    if (nodeId == null) { setDetailStatus('idle'); return } // payload under node B while B's fetch is in flight (or failed)
    if (readOnlyReason === 'review' && !evidenceAvailable) { setDetailStatus('restricted'); return }
    setDetailStatus('loading')
    let on = true
    const at = readOnly && historySeq != null
      ? `?seq=${encodeURIComponent(historySeq)}&expected_generation=${encodeURIComponent(expectedGeneration || '')}`
      : ''
    get(runNodeApiPath(runId, nodeId, at))
      .then(d => {
        const valid = detailMatchesNode(d)
        if (on && valid && detailMatchesAttempt(d)) {
          setDetailResource({ scope: detailScope, data: d }); setDetailStatus('ready')
        } else if (on) {
          setDetailStatus('error')
          setDetailError(valid
            ? 'The experiment attempt changed while details were loading.'
            : 'Full node details returned an invalid response.')
        }
      })
      .catch(() => { if (on) { setDetailStatus('error'); setDetailError('Full node details could not be loaded.') } })
    return () => { on = false }
  }, [runId, nodeId, nodeAttempt, state?.nodes?.[nodeId]?.status, readOnly, historySeq,
    expectedGeneration, readOnlyReason, evidenceAvailable, detailScope, detailNonce])
  useEffect(() => {
    if (detailFocusScopeRef.current == null) return
    if (detailFocusScopeRef.current !== detailScope) {
      detailFocusScopeRef.current = null
      return
    }
    if (detailStatus !== 'ready' && detailStatus !== 'error' && detailStatus !== 'restricted') return
    detailFocusScopeRef.current = null
    const frame = requestAnimationFrame(() => {
      if (document.activeElement === document.body) detailSurfaceRef.current?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [detailScope, detailStatus])
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
  // `withBuilding` splices EVERY in-flight build into `live.nodes` (building:true), so keying off the
  // spliced node — not the singular `live.building` — lights up whichever concurrent build this is.
  // CLAUDE REVIEW: the claim above is false for EXISTING nodes — withBuilding skips ids already in
  // state.nodes (buildingModel.js "never overwrite either with a ghost"), yet node_reset re-builds
  // emit node_building for an existing pending node (orchestrator re-propose/re-implement paths). So
  // after an operator resets a node, building is never true here: the /nodes/{id} poll stops and the
  // Trace tab never shows writing/repairing during the rebuild. Check the raw building markers for
  // nodeId (like Dock's buildingGenerations(live)) instead of the spliced flag.
  const nodeWorking = engineActive && (live?.nodes?.[nodeId]?.building === true || evaluatingThis)
  usePoll((alive) => {
    // alive() gates the async resolution: if the user selects a different node (or the poll is
    // disabled) while this /nodes/{nodeId} request is in flight, its late response must NOT overwrite
    // the newly-selected node's detail — otherwise node A's Code/Trace/Metrics render (stuck) under B.
    get(runNodeApiPath(runId, nodeId)).then(d => {
      if (alive() && detailMatchesNode(d) && detailMatchesAttempt(d)) {
        setDetailResource({ scope: detailScope, data: d }); setDetailStatus('ready'); setDetailError('')
      }
    }).catch(() => {})
  }, 4000, [runId, nodeId, nodeWorking, detailScope],
  { enabled: !readOnly && nodeWorking, immediate: false })

  if (nodeId == null) return <div className="insp-empty">Select a node to inspect its idea, code, metrics, trust, and agent trace.</div>
  const n = detail || state?.nodes?.[nodeId]
  const visibleDetailStatus = detailCurrent ? detailStatus
    : readOnlyReason === 'review' && !evidenceAvailable ? 'restricted' : 'loading'
  if (!n) {
    if (visibleDetailStatus === 'error') return <div ref={detailSurfaceRef}
      className="notice resource-error" role="alert" tabIndex={-1}>
      <span>{detailError || 'Full node details could not be loaded.'}</span>
      <button type="button" className="btn sm" onClick={retryDetail}>Retry</button>
    </div>
    if (visibleDetailStatus === 'restricted') return <div ref={detailSurfaceRef}
      className="insp-empty" role="status" tabIndex={-1}>
      Experiment #{nodeId} is not included in this summary-only review.
    </div>
    return <div ref={detailSurfaceRef} className="insp-empty" role="status" tabIndex={-1}>
      Loading experiment #{nodeId} details…
    </div>
  }
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
      <div ref={detailSurfaceRef} className="insp-body" id={panelId(activeTab)} role="tabpanel"
        aria-labelledby={tabId(activeTab)} tabIndex={0}>
        {visibleDetailStatus === 'loading' && <div className="notice" role="status">Loading full node details…</div>}
        {visibleDetailStatus === 'error' && <div className="notice resource-error" role="alert"><span>{detailError} The summary below may be incomplete.</span><button type="button" className="btn sm" onClick={retryDetail}>Retry</button></div>}
        {readOnly
          ? <div className="insp-hint history-inline">{readOnlyReason === 'review'
              ? evidenceAvailable
                ? 'Read-only review with redacted source evidence. Live traces and actions stay hidden.'
                : 'Summary-only review. Source, live traces, and actions are not included.'
              : `Snapshot seq ${historySeq} · read-only. Live traces, metrics sidecars and actions are hidden.`}</div>
          : <div className="insp-hint muted">Run actions (confirm · ablate · fork · promote) stay in chat. Use Comments for review, or attach <button className="ctx-chip ctx-chip-action" title="attach this node to assistant context" onClick={() => window.dispatchEvent(new CustomEvent('ll:attach-node', { detail: { id: n.id } }))}>＋ #{n.id}</button> as context.<ResetBtn runId={runId} id={n.id} generation={n.attempt} onToast={onToast} /></div>}

        {activeTab === 'Overview' && <Overview n={n} state={state} runId={readOnly ? null : runId} onToast={onToast} />}
        {activeTab === 'Comments' && <CommentsThread runId={runId} nodeId={n.id}
          nodeGeneration={n.attempt} expectedGeneration={expectedGeneration} refreshKey={commentsRevision}
          readOnly={readOnly} reviewMode={readOnlyReason === 'review'} focusCommentId={focusCommentId} />}
        {activeTab === 'Trials' && <Trials n={n} detail={detail} state={state} />}
        {activeTab === 'Trace' && <Trace n={n} runId={runId} live={live} working={nodeWorking}
          onReload={() => setDetailNonce(value => value + 1)} />}
        {activeTab === 'Code' && (visibleDetailStatus === 'ready'
          ? <Code n={n} />
          : visibleDetailStatus === 'error'
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
export function GroupSummary({
  groupKey, memberIds, state, themeFilter = null, highlightIds = null, onSelectNode, onClose,
}) {
  const dir = state.direction
  // Keep the drill-down on exactly the same semantic projection as its collapsed super-node. Without
  // this, a truthful 2/8 card could open a cross-direction best, trajectory, and member table.
  const aggregate = themeFilteredGroupAggregate(
    memberIds || [], state.nodes, dir, themeFilter, state, highlightIds)
  const members = aggregate.matchedIds.map(id => state.nodes[id]).filter(Boolean).sort((a, b) => a.id - b.id)
  const zeroMatch = aggregate.filterActive && aggregate.matchedCount === 0
  const countLabel = aggregate.filterActive
    ? `${aggregate.matchedCount}/${aggregate.totalCount}`
    : String(aggregate.totalCount)
  const themes = [...new Set(members.map(node => nodeTheme(node, state)).filter(Boolean))]
  return <>
    <div className="tabs">
      <div className="tab active">Group · {groupKey}</div>
      <span className="spacer" />
      <button className="btn sm ghost" onClick={onClose} title="close group view" aria-label="Close group details">✕</button>
    </div>
    <div className="insp-body">
      <div className="kv">
        <KV k={aggregate.filterActive ? 'matching experiments' : 'experiments'} v={countLabel} />
        {aggregate.filterActive && <KV k="active filter" v={aggregate.filterDescription} />}
        <KV k="best" v={zeroMatch ? 'No matching result' : fmt(aggregate.best)} />
        {themes.length > 0 && <KV k="primary concept axes" v={themes.join(', ')} />}
      </div>
      {zeroMatch
        ? <div className="insp-empty" role="status">No experiments in this group match {aggregate.filterDescription}.</div>
        : <>
          <div className="section-h">Best over {aggregate.filterActive ? 'matching ' : ''}members</div>
          <Trajectory nodes={members} direction={dir} state={state} height={150} onPick={onSelectNode} />
          <div className="section-h">{aggregate.filterActive ? 'Matching members' : 'Members'} <span className="pill">{countLabel}</span></div>
          <DataTable caption="Group member results" card={false}><table className="tbl"><thead><tr><th>node</th><th>operator</th><th>metric</th><th>status</th></tr></thead>
            <tbody>{members.map(n => <tr key={n.id}>
              <td><button type="button" className="btn xs ghost" onClick={() => onSelectNode(n.id)}>#{n.id}</button></td>
              <td>{n.operator}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td><td>{n.status}</td></tr>)}</tbody></table></DataTable>
        </>}
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
    } catch { onToast?.('Re-run could not be submitted. Try again.') }
    finally { setPendingStage(null) }
  }
  return <div className="eval-pipeline">
    <div className="muted eval-pipeline-label">
      eval pipeline{failed ? ` — failed at ${failed}` : ''}{runId ? ' · click a stage to re-run from there' : ' · historical result (read-only)'}</div>
    <div className="eval-pipeline-stages">
      {stages.map((s, i) => <React.Fragment key={i}>
        {runId ? <button type="button" disabled={pendingStage != null} onClick={() => rerun(s.name)}
          className="eval-pipeline-step" style={{ '--stage-tone': tone(s) }}
          title={`${s.name}: ${s.status}${s.seconds != null ? ` · ${s.seconds}s` : ''}${s.exit_code != null ? ` · exit ${s.exit_code}` : ''} — click to re-run the pipeline FROM here (reuse earlier stages)`}>
          {ic(s)} {s.name}</button> : <span
          className="eval-pipeline-step" style={{ '--stage-tone': tone(s) }}
          title={`${s.name}: ${s.status}${s.seconds != null ? ` · ${s.seconds}s` : ''}${s.exit_code != null ? ` · exit ${s.exit_code}` : ''} · historical result`}>
          {ic(s)} {s.name}</span>}
        {i < stages.length - 1 && <span className="muted eval-pipeline-arrow">→</span>}
      </React.Fragment>)}
    </div>
  </div>
}

function Overview({ n, state, runId, onToast }) {
  const p = n.idea?.params || {}
  const uses = mergeSummary(n, state.nodes || {}, state)   // E3: for merges, which technique each parent fused
  const chg = nodeChip(n, state.nodes || {}, state)        // same chip as the card (sweep-aware; '' for merges)
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
    {n.idea?.rationale && !(chg && chg.includes(n.idea.rationale)) && <><div className="section-h">Rationale</div><Markdown className="rationale-md" text={n.idea.rationale} /></>}
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

// Friendly identity for each span kind — turns recorded span names into "who did what" so the trace
// reads as the node's life story rather than instrumentation. `tone` colours the waterfall bar so
// phases are distinguishable at a glance. (Span names come from orchestrator.py.)
// icon = an OpIcon glyph name (monochrome, inherits the stage tone via currentColor — no color emoji).
// Compact tuple schema: [icon, visible role, description, tone]. This metadata ships with every
// Inspector visit, so positional values avoid repeating four object keys for every trace operation.
const STAGE = {
  onboard:      ['flag', 'Onboarding', 'task setup & eval spec', '#8a7bb0'],
  create_node:  ['trending', 'Author node', 'propose an idea, then build the solution', '#6f8bb0'],
  propose:      ['search', 'Researcher · propose', 'propose the next idea', '#6fa3b0'],
  // the Developer's own sub-phases (repo tasks): STAGES declares the eval pipeline, PLAN decomposes
  // the change into atomic steps — both read-only, before the write-capable implement session(s).
  stages:       ['sliders', 'Developer · stages', 'declare the eval pipeline (prep → train → …)', '#5f9e8f'],
  plan:         ['doc', 'Developer · plan', 'decompose into atomic steps', '#7fae8f'],
  'handoff-summary': ['doc', 'Handoff summary', 'distill this phase for the next (fewer re-reads downstream)', '#8fa8b8'],
  implement:    ['gear', 'Developer · implement', 'write / edit the solution code', '#6fae97'],
  repair:       ['bug', 'Developer · repair', 'fix a failed parent', '#b0936f'],
  inline_repair: ['bug', 'Developer · inline repair', 'quick in-eval fix attempts', '#b08a6f'],
  seed_workspace: ['gear', 'Workspace', 'materialize node files into the eval workdir', '#8b96a5'],
  evaluate:     ['target', 'Evaluate', 'run the solution & score it', '#a87da8'],
  triage:       ['bug', 'Triage', 'a failed node — decide repair / abandon / reject-idea', '#b07a7a'],
  // declared eval-pipeline stages (looplab_stages.json): each runs as its own block in the node story
  train:        ['replay', 'Train', 'declared pipeline stage: train a fresh model', '#4e8f5d'],
  data_prep:    ['sliders', 'Data prep', 'declared pipeline stage: prepare data/features', '#7a9e5f'],
  score:        ['target', 'Evaluate · score', "operator's protected scoring stage", '#a87da8'],
  confirm_seed: ['replay', 'Confirmation', 'multi-seed robustness check', '#9aa06f'],
  ablate:       ['sliders', 'Ablation', 'sensitivity probe', '#6f8bb0'],
  // sub-operation traces the engine wraps in their own named span — give each a distinct hue so the
  // conversation reads as coloured bands (foresight vs strategy vs research vs merge) at a glance.
  // Two DISTINCT Researcher ranking steps — kept apart so the first doesn't read as a duplicate of
  // the second: `hyp_prioritize` runs BEFORE propose (pick which open hypothesis to pursue),
  // `foresight_rank` runs AFTER propose (predict the chosen proposal's payoff, best-of-N pick).
  hyp_prioritize: ['bulb', 'Researcher · prioritize', 'rank the open-hypothesis board', '#c2a24e'],
  foresight_rank: ['bulb', 'Researcher · foresight', 'predict payoff of the chosen idea', '#c2a24e'],
  foresight:      ['bulb', 'Researcher · foresight', 'predict payoff of the chosen idea', '#c2a24e'],
  strategy_consult: ['trending', 'Strategist', 'pick policy / operators / fidelity', '#b0729e'],
  strategy_decision: ['trending', 'Strategist', 'pick policy / operators / fidelity', '#b0729e'],
  hypothesis_merge: ['confluence', 'Hypothesis merge', 'fold paraphrase hypotheses', '#5fa0a8'],
  deep_research:  ['search', 'Deep research', 'read the literature first', '#6fb0a3'],
  lessons:        ['doc', 'Lessons', 'reflect / distil cross-run lessons', '#9a8fb0'],
  lessons_distill: ['doc', 'Lessons', 'reflect / distil cross-run lessons', '#9a8fb0'],
  lessons_refresh: ['doc', 'Lessons', 'reflect / distil cross-run lessons', '#9a8fb0'],
  novelty:        ['gitbranch', 'Novelty gate', 'dedup near-duplicate proposals', '#a89a6f'],
}
const stageMeta = (name) => STAGE[name] || ['dot', name, '', 'var(--accent)']

// Compact info helpers so each trace row carries the data that DIFFERENTIATES it (langfuse/Phoenix
// convention: model · input→output tokens · a content preview), instead of a bare op name repeated.
const ktok = (n) => (n == null ? '' : (n >= 1000 ? +(n / 1000).toFixed(n >= 9950 ? 0 : 1) + 'k' : String(n)))
const shortModel = (m) => (m || '').split('/').pop()
// Roll the whole subtree of a span up to "how many model calls and how many tokens it cost" — shown on
// the stage/span header so you see the expensive steps without expanding anything. Counts first-class
// GENERATION spans. Projection schema 2 deliberately drops legacy event-embedded I/O.
function spanRollup(s) {
  // tok = SUM of every call's total (billed — a tool loop re-sends the growing context each turn, O(n²)).
  // ctx = the PEAK single prompt = the real context-window size. out = generated tokens. The UI shows
  // ctx + out (billed tok in the tooltip) so the number reads as "context", not the re-send sum.
  let calls = 0, tok = 0, ctx = 0, out = 0
  const walk = (x) => {
    if (x.kind === 'generation') { calls++; const u = (x.attributes || {}).usage || {}; const p = u.prompt || 0; tok += (u.total != null ? u.total : p + (u.completion || 0)); ctx = Math.max(ctx, p); out += u.completion || 0 }
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
    tokens: u,
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
      : <div className="muted generation-empty">
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
//    all freezes the browser / black screen). Show the first SPAN_CAP, then a "show N more" button.
//    This local reveal remains subject to the server's bounded/redacted projection and omission receipt.
const SPAN_CAP = 60

export function TraceUnavailable({ label = 'Trace unavailable.', onRetry }) {
  return <div className="notice resource-error compact" role="alert">
    <span>{label}</span>
    {onRetry && <button type="button" className="btn sm" onClick={onRetry}>Retry trace</button>}
  </div>
}

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
    {shown.map(({ c, d, i }) => <SpanRow key={`${runId}:${c.span_id || i}`} s={c} depth={d} t0={t0} total={total} runId={runId} parentOp={parentOp} />)}
    {!all && rows.length > SPAN_CAP && <button className="span-more" style={{ marginLeft: depth * 14 + 4 }}
      onClick={() => setAll(true)}>… show {rows.length - SPAN_CAP} more observations</button>}
  </>
}

// One span and its subtree, drawn as a langfuse-style waterfall row: the bar is positioned by the
// span's OFFSET from the trace start (t0) and sized by its duration, so sequence reads at a glance.
// Renders three observation kinds distinctly — GENERATION (an LLM call: op·model·in→out·preview, its
// prompt/output on expand), TOOL (name·arg, its input/output on expand), and OPERATION (a phase of
// work) — so the tree shows exactly what called what and what each bounded projection produced.
function SpanRow({ s, depth, t0, total, runId, parentOp = null }) {
  const [open, setOpen] = useState(false)
  const [io, setIo] = useState(null)
  const kind = s.kind || 'operation'
  const err = s.status === 'ERROR'
  const off = (typeof s.start === 'number') ? Math.max(0, (s.start - t0) / total * 100) : 0
  const wid = Math.max(1.5, (s.duration_s || 0) / total * 100)
  const barTone = err ? 'var(--fail)' : kind === 'generation' ? 'var(--accent)' : kind === 'tool' ? 'var(--working)' : stageMeta(s.name)[3]
  const bar = <span className="span-bar"><span className="span-fill" style={{ marginLeft: Math.min(98, off) + '%', width: wid + '%', background: barTone }} /></span>
  const kids = <SpanList items={s.children} depth={depth + 1} t0={t0} total={total} runId={runId} parentOp={s.name} />
  const rowIndent = { paddingLeft: depth * 14 }
  const detailIndent = { marginLeft: depth * 14 + 16 }
  // On first expand, pull the bounded/redacted detail projection; its omission receipt is rendered.
  useEffect(() => {
    if (open && io === null && runId && s.span_id && (kind === 'generation' || kind === 'tool')) {
      let on = true
      spanDetail(runId, s.span_id).then(d => on && setIo(traceDetailState(d)))
        .catch(() => on && setIo(unavailableTraceDetail()))
      return () => { on = false }
    }
  }, [open, io, runId, s.span_id, kind])
  const retryIo = () => setIo(null)

  if (kind === 'generation') {
    // Row header from the LIGHT span (op·model·tokens); the prompt/output come from the fetched `io`.
    const a = { ...(s.attributes || {}), ...(io?.attributes || {}) }
    const c = genToCall({ ...s, attributes: a }), t = c.tokens
    return <>
      <button type="button" aria-expanded={open} className={'span-row gen disclosure-button' + (err ? ' err' : '')}
        style={rowIndent} onClick={() => setOpen(o => !o)} title="expand for prompt & output">
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        {(() => {   // name the call by ROLE so "who writes code" is unmistakable: the Developer's LLM
          // call (under implement/repair) is "writing code"; the Researcher's (under propose) is "reasoning".
          const dev = parentOp === 'implement' || parentOp === 'repair'
          const label = dev ? 'writing code' : (parentOp === 'propose' && a.op === 'chat' ? 'reasoning' : (a.op || 'llm'))
          return <span className="span-name gen"><OpIcon name={dev ? 'pencil' : 'bulb'} className="t-ic" /> <span className={'llm-op' + (dev ? ' dev-code' : '')}>{label}</span>{a.model && <span className="llm-model" title={a.model}>{shortModel(a.model)}</span>}</span>
        })()}
        {bar}
        <span className="t">{fmt(s.duration_s, 3)}s</span>
        {(t.prompt != null || t.completion != null) && <span className="badge" title={`${t.prompt || 0} prompt → ${t.completion || 0} completion tokens`}>{ktok(t.prompt)}→{ktok(t.completion)}</span>}
        {err && <span className="badge reason">ERROR</span>}
      </button>
      {open && <div className="span-detail" style={detailIndent}>
        {io === null ? <div className="muted trace-small" role="status">loading…</div> : io.status === 'unavailable'
          ? <TraceUnavailable label="Trace detail unavailable." onRetry={retryIo} />
          : <>{io.partial && <div className="notice compact" role="status">Trace detail truncated.</div>}<GenBody c={c} /></>}</div>}
      {kids}
    </>
  }
  if (kind === 'tool') {
    const a = { ...(s.attributes || {}), ...(io?.attributes || {}) }
    const inp = asText(a.input), outp = asText(a.output), name = (s.attributes || {}).tool || a.tool || 'tool'
    return <>
      <button type="button" aria-expanded={open} className={'span-row tool disclosure-button' + (err ? ' err' : '')}
        style={rowIndent} onClick={() => setOpen(o => !o)} title="expand for input & output">
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        <span className="span-name tool"><OpIcon name="gear" className="t-ic" /> <b className="tool-name">{name}</b></span>
        {bar}
        <span className="t">{fmt(s.duration_s, 3)}s</span>
        {err && <span className="badge reason">ERROR</span>}
      </button>
      {open && <div className="span-detail" style={detailIndent}>
        {io === null ? <div className="muted trace-small" role="status">loading…</div> : io.status === 'unavailable'
          ? <TraceUnavailable label="Trace detail unavailable." onRetry={retryIo} /> : <>
          {io.partial && <div className="notice compact" role="status">Trace detail truncated.</div>}
          {inp && <div className="msg"><div className="msg-role role-user">input</div><pre className="code">{inp}</pre></div>}
          {outp && <div className="msg"><div className="msg-role role-completion">output</div><pre className="code">{outp}</pre></div>}
          {!inp && !outp && <div className="muted trace-small">(no input/output recorded)</div>}</>}
      </div>}
      {kids}
    </>
  }
  // OPERATION span (a phase of work): bounded attributes and events.
  const attrs = Object.entries(s.attributes || {}).filter(([k]) => k !== 'node_id')
  const events = s.events || []
  const [icon, role, desc] = stageMeta(s.name)
  const detail = attrs.length || events.length
  const OperationHeader = detail ? 'button' : 'div'
  return <>
    <OperationHeader type={detail ? 'button' : undefined} aria-expanded={detail ? open : undefined}
         className={'span-row' + (detail ? ' disclosure-button' : '') + (err ? ' err' : '')}
         style={rowIndent} onClick={detail ? () => setOpen(o => !o) : undefined}
         title={detail ? 'click for step detail' : ''}>
      <span className="span-tw">{detail ? (open ? '▾' : '▸') : '·'}</span>
      <span className="span-name" title={desc}><OpIcon name={icon} className="t-ic" /> {role !== s.name ? role : s.name}</span>
      {bar}
      <span className="t">{fmt(s.duration_s, 3)}s</span>
      {err && <span className="badge reason">ERROR</span>}
    </OperationHeader>
    {open && detail && <div className="span-detail" style={detailIndent}>
      {attrs.length > 0 && <div className="kv">{attrs.map(([k, v]) =>
        <KV key={k} k={k} v={typeof v === 'object' ? JSON.stringify(v) : String(v)} />)}</div>}
      {events.map((e, i) => <div key={i} className="span-ev">
        <span className="ty">{e.name}</span>{e.error ? <span className="flag"> {e.error}</span> :
          <span className="muted"> {Object.entries(e).filter(([k]) => k !== 'name').map(([k, v]) => `${k}=${v}`).join(' ')}</span>}
      </div>)}
    </div>}
    {kids}
  </>
}

// A top-level lifecycle stage (one root span = one phase of work on this node), with its sub-steps.
// The header rolls up the stage's model-call count + token cost so the expensive phases stand out.
function StageBlock({ s, t0, total, runId }) {
  const [icon, role, desc] = stageMeta(s.name)
  const roll = spanRollup(s)
  return <div className={'stage' + (s.status === 'ERROR' ? ' err' : '')}>
    <div className="stage-h" title={desc}>
      <span className="stage-ic"><OpIcon name={icon} /></span>
      <b>{role}</b>
      {roll.calls > 0 && <span className="stage-roll" title={`${roll.tok} billed tokens`}>{roll.calls} call{roll.calls > 1 ? 's' : ''}{roll.ctx ? ` · ${ktok(roll.ctx)} ctx` : ''}{roll.out ? ` · ${ktok(roll.out)} out` : ''}</span>}
      <span className="spacer" />
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
export function NodeTrace({ spans, runId, projection = {}, onRetry, onLoadMore }) {
  const roots = spans || []
  if (traceUnavailable(projection)) return <TraceUnavailable onRetry={onRetry} />
  const partial = tracePartial(projection)
  // Prefer an ACTIONABLE control over a dead "projection is partial" notice. The receipt remains in
  // projection; repeating its optional count in this hot render path added branches without utility.
  const loadMore = (partial && onLoadMore)
    ? <button type="button" className="trace-loadmore disclosure-button" onClick={onLoadMore}>
        ↧ load more spans
      </button>
    : null
  if (!roots.length) {
    if (loadMore) return loadMore
    if (partial) return <div className="notice compact" role="status">Trace projection is partial; no observations were included.</div>
    return <div className="muted trace-small">No execution spans captured yet.</div>
  }
  const { t0, total } = traceBounds(roots)
  return <div className="trace">
    {loadMore || (partial && <div className="notice compact" role="status">Trace projection is partial.</div>)}
    {roots.map((s, i) => <StageBlock key={`${runId}:${s.span_id || i}`} s={s} t0={t0} total={total} runId={runId} />)}
  </div>
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
      <span className="spacer" />
      <span className="muted">{r.attempts} attempt{r.attempts === 1 ? '' : 's'}</span>
    </div>
    <DataTable caption="Agent attempt validation checks" card={false}><table className="tbl"><thead><tr><th>check</th><th>ok</th><th>detail</th></tr></thead>
      <tbody>{(r.checks || []).map((c, i) => <tr key={i}>
        <td>{c.name}</td><td style={{ color: c.ok ? 'var(--ok)' : 'var(--fail)' }}>{c.ok ? '✓' : '✗'}</td>
        <td className="muted">{c.detail || c.severity || ''}</td></tr>)}</tbody></table></DataTable>
  </div>
}

// ── linear conversation view ─────────────────────────────────────────────────────────────────────
// The span-tree projection can re-show the retained re-sent message list on every generation (a
// tool-loop re-sends growing history each turn). The conversation projection reconstructs the loop as
// a readable thread: the request once per sub-loop, then each retained generation delta + tool calls.
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
    {!text && !t.think && calls.length === 0 && <div className="muted trace-small">(no output)</div>}
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
      {!t.input && !t.output && <div className="muted trace-small">(no input/output recorded)</div>}
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
  return <div className="stage-log">
    <div className="muted stage-log-label">📄 stage log{live ? ' · live' : ''}</div>
    <pre ref={ref} className="training-log">{shown}</pre>
  </div>
}

function ConvStage({ st, defaultOpen = true, log = '', live = false }) {
  const [icon, role, desc, tone] = stageMeta(st.label)
  const [open, setOpen] = useState(defaultOpen)
  const [allTurns, setAllTurns] = useState(false)
  const roll = st.rollup || {}
  const tk = roll.tokens || {}
  const nTurns = (st.turns || []).length
  const err = st.status === 'ERROR'
  // Colour-band the stage by its tone: a left rail + a tinted header, so foresight/strategy/researcher/
  // developer/eval read as distinct bands. Click the header to collapse the whole band.
  return <div className={'stage stage-dynamic' + (err ? ' err' : '')}
              style={{ '--stage-tone': err ? 'var(--fail)' : tone }}>
    <button type="button" className="stage-h disclosure-button" aria-expanded={open}
         title={desc + ' — click to collapse'} onClick={() => setOpen(o => !o)}>
      <span className="stage-caret">{open ? '▾' : '▸'}</span>
      <span className="stage-ic"><OpIcon name={icon} /></span>
      <b className="stage-role">{role}</b>
      {(roll.generations || roll.tools) ? <span className="stage-roll"
          title={tk.total ? `context window peaked at ${tk.context || 0} tokens; the model generated ${tk.completion || 0}. Billed ${tk.total} total — a tool loop RE-SENDS the growing context every turn, so billed ≫ context.` : undefined}>
        {roll.generations || 0} turn{roll.generations === 1 ? '' : 's'}
        {roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? '' : 's'}` : ''}
        {tk.context ? ` · ${ktok(tk.context)} ctx` : ''}
        {tk.completion ? ` · ${ktok(tk.completion)} out` : ''}</span> : null}
      {!open && nTurns ? <span className="muted stage-hidden-count">· {nTurns} step{nTurns === 1 ? '' : 's'} hidden</span> : null}
    </button>
    {open && <div className="conv-turns">
      {/* Cap the mounted turns like the span-tree view (SPAN_CAP): a heavily-repaired / tool-looping stage
          can carry hundreds of turns, and ConvGen eagerly renders each turn's Markdown — mounting them all
          froze the browser. Show the first SPAN_CAP, then reveal the rest of this server projection. */}
      {(allTurns ? (st.turns || []) : (st.turns || []).slice(0, SPAN_CAP)).map((t, j) =>
        t.type === 'request' ? <ConvRequest key={j} t={t} />
          : t.type === 'tool' ? <ConvTool key={j} t={t} /> : <ConvGen key={j} t={t} />)}
      {!allTurns && (st.turns || []).length > SPAN_CAP && <button className="span-more"
        onClick={() => setAllTurns(true)}>… show {(st.turns || []).length - SPAN_CAP} more turns</button>}
      {log ? <StageLog text={log} live={live} /> : null}
    </div>}
  </div>
}

function Conversation({ n, runId, working, allOpen = true, reloadNonce = 0, onRetry }) {
  const [conv, setConv] = useState(null)
  const [logs, setLogs] = useState({})   // {eval, stages:{train,score,…}} — the live stage/eval logs
  useEffect(() => {
    setConv(null)   // node changed → clear before the first load (poll ticks below don't clear, so no flash)
    setLogs({})     // …likewise the logs, else B's stage bands briefly render A's log text
  }, [runId, n.id, working, reloadNonce])
  usePoll((alive) => {
    nodeConversation(runId, n.id).then(d => alive() && setConv(d || { stages: [] }))
      .catch(() => alive() && setConv({ stages: [], projection: { unavailable: true } }))
    // Stage/eval logs ride ALONGSIDE the trace now (moved out of the old Training tab): each stage
    // band renders its own live log inside it, so opening "Train" shows the training output in place.
    get(runNodeApiPath(runId, n.id, '/logs')).then(d => alive() && setLogs(d || {})).catch(() => {})
  }, working ? 4000 : null,   // interval only while the agent works this node (live-refresh); null = load once
  [runId, n.id, working, reloadNonce])   // reloadNonce also re-runs a finished node's one-shot load
  if (conv === null) return <div className="muted trace-small" role="status">loading…</div>
  const stages = conv.stages || []
  const unavailable = traceUnavailable(conv.projection)
  const partial = tracePartial(conv.projection)
  if (unavailable) return <TraceUnavailable onRetry={onRetry} />
  if (!stages.length) return <div className={partial ? 'notice compact' : 'muted'} role={partial ? 'status' : undefined}>{partial
    ? 'Trace projection is partial.' : 'No conversation captured for this node yet.'}</div>
  // The live log for a stage band: a multi-stage eval logs per stage (stages[label]); a single-command
  // eval logs to eval.log ("evaluate"/"command"); the dep-install step to setup.log. Anything else
  // (propose/implement/…) has no subprocess log.
  const logFor = (label) => (logs.stages && logs.stages[label])
    || ({ setup: logs.setup, evaluate: logs.eval, command: logs.eval }[label]) || ''
  // `allOpen` is owned by the sticky Trace header (so collapse-all lives in the pinned bar). It's folded
  // into each band's key so a collapse/expand-all click remounts them at the new default; a live poll
  // (allOpen unchanged) keeps the key stable, so per-band toggles survive the 4s refresh.
  return <div className="conv">{partial && <div className="notice compact" role="status">Trace projection is partial.</div>}
    {stages.map((st, i) => <ConvStage key={`${st.trace_id || ''}:${st.label || ''}:${st.start || i}:${allOpen}`}
                                      st={st} defaultOpen={allOpen} log={logFor(st.label)} live={working} />)}
    {logs.run_setup ? <RunSetupLog text={logs.run_setup} /> : null}
  </div>
}

// The run-level, one-time dependency install (shared by every node) — moved out of the old Training
// tab; a collapsed footnote under the trace so a setup failure is still inspectable without its own tab.
function RunSetupLog({ text }) {
  const [open, setOpen] = useState(false)
  return <div className="stage run-setup-stage">
    <button type="button" className="stage-h disclosure-button" aria-expanded={open}
      onClick={() => setOpen(o => !o)}>
      <span className="stage-caret">{open ? '▾' : '▸'}</span>
      <b className="muted">Run setup <span className="normal-weight">· deps install (run-level, once)</span></b>
    </button>
    {open && <div className="conv-turns"><StageLog text={text} live={false} /></div>}
  </div>
}

function Trace({ n, runId, live, working, onReload }) {
  const [view, setView] = useState('conversation')   // linear reading by default; span tree on demand
  const [allOpen, setAllOpen] = useState(false)       // bands COLLAPSED by default (expand one to read it)
  const [nonce, setNonce] = useState(0)               // bumped after "clear trace" to reload the bands
  const [clearing, setClearing] = useState('')        // '' | 'confirm' | 'busy' | error message
  const bodyRef = useRef(null)
  const spans = n.trace?.nodes || []
  const unavailable = traceUnavailable(n.trace?.projection)
  const partial = tracePartial(n.trace?.projection)
  const agent = n.agent_report
  // Live status: what the node is doing RIGHT NOW. Two live states: an LLM authoring the code
  // (building → writing / repairing / merging), or the sandbox running its eval pipeline (pending →
  // training / scoring). `_op` is only set in the building case (the eval has no operator), so it
  // cleanly disambiguates the two.
  // Read this node's OWN build marker off the spliced node (`withBuilding` sets building:true + operator
  // on every concurrent build), not the singular `live.building` which is only the last-appended one.
  const _bnode = live?.nodes?.[n.id]
  const building = working && _bnode?.building === true
  const _op = building ? (_bnode.operator || '') : ''
  const statusLabel = !working ? null
    : building
      ? (/repair|debug/.test(_op) ? '🔧 repairing…' : /merge/.test(_op) ? '🔀 merging…' : '✍️ writing code…')
      : '🏋️ training / evaluating…'
  const status = statusLabel && <div className="trace-live-status" role="status"><span className="tls-dot" />{statusLabel}
    <span className="muted trace-live-note">live · auto-updates</span></div>
  const scrollTo = (where) => { const c = bodyRef.current?.closest('.insp-body'); if (c) c.scrollTop = where === 'top' ? 0 : c.scrollHeight }
  const doClear = async () => {
    setClearing('busy')
    try {
      await clearNodeTrace(runId, n.id)
      setNonce(x => x + 1)          // reload the Conversation bands (now empty until a rebuild re-traces)
      onReload?.()                  // refresh the parent-owned span tree and rollup as well
      setClearing('')
    } catch (e) {
      // 409 while the engine is live is the common case. Keep server free text out of the UI.
      setClearing(e?.status === 409 ? 'stop the run first' : 'clear failed — try again')
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
    {clearing === 'busy' && <span className="muted trace-clear-status">clearing…</span>}
    {clearing && clearing !== 'confirm' && clearing !== 'busy' &&
      <span className="muted trace-clear-status trace-clear-error">{clearing}</span>}
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
      <button aria-pressed={view === 'raw'} className={'seg' + (view === 'raw' ? ' on' : '')} onClick={() => setView('raw')}>span tree</button>
      {view === 'conversation' && <button className="seg trace-collapse" aria-pressed={allOpen} title="collapse or expand every stage"
        onClick={() => setAllOpen(o => !o)}>{allOpen ? '⊟ collapse all' : '⊞ expand all'}</button>}
      <span className="spacer" />{clearBtn}{nav}
    </div>
  </div>
  if (view === 'conversation')
    return <div className="trace" ref={bodyRef}>{head}<Conversation n={n} runId={runId} working={working} allOpen={allOpen}
      reloadNonce={nonce} onRetry={() => setNonce(value => value + 1)} />
      {agent && <AgentReport r={agent} />}</div>
  if (!spans.length && !agent) {
    if (unavailable)
      return <div className="trace" ref={bodyRef}>{head}<TraceUnavailable onRetry={onReload} /></div>
    if (partial)
      return <div className="trace" ref={bodyRef}>{head}<div className="notice compact" role="status">Trace projection is partial; no observations were included.</div></div>
    return <div className="trace" ref={bodyRef}>{head}<div className="muted">No execution spans yet. Offline nodes may have none; active nodes update here as they run.</div></div>
  }
  if (unavailable)
    return <div className="trace" ref={bodyRef}>{head}<TraceUnavailable onRetry={onReload} />
      {agent && <AgentReport r={agent} />}</div>
  if (!spans.length && partial)
    return <div className="trace" ref={bodyRef}>{head}<div className="notice compact" role="status">Trace projection is partial; no observations were included.</div>
      {agent && <AgentReport r={agent} />}</div>
  const { t0, total } = traceBounds(spans)
  // create_node already nests propose→implement; if an agent wrote the node, the report belongs
  // right after that authoring stage (placed by index), otherwise it trails the whole lifecycle.
  const authorIdx = spans.findIndex(s => ['create_node', 'implement', 'repair'].includes(s.name))
  const roll = n.trace?.rollup || {}
  const rtok = roll.tokens || {}
  return <div className="trace" ref={bodyRef}>
    {head}
    {partial && <div className="notice compact" role="status">Trace projection is partial.</div>}
    <div className="muted trace-rollup-intro">
      Node #{n.id} lifecycle · offset = start, bar = duration. Expand an observation for bounded,
      redacted I/O.
      {(roll.generations || roll.tools) ? <span className="trace-totals"
          title={rtok.total ? `context window peaked at ${rtok.context || 0} tokens; the model generated ${rtok.completion || 0}. Billed ${rtok.total} total — each turn RE-SENDS the growing context, so billed ≫ context.` : undefined}>
        {' · '}{roll.generations || 0} generation{roll.generations === 1 ? '' : 's'}
        {roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? '' : 's'}` : ''}
        {rtok.context ? ` · ${ktok(rtok.context)} ctx` : ''}
        {rtok.completion ? ` · ${ktok(rtok.completion)} out` : ''}
        {roll.cost ? ` · $${roll.cost}` : ''}
      </span> : null}
    </div>
    {spans.map((s, i) => <React.Fragment key={`${n.attempt ?? ''}:${s.span_id || i}`}>
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
    <div className="toolbar code-toolbar">
      {n.parent_code != null && <button className={'btn sm' + (diff ? ' primary' : '')} onClick={() => setDiff(d => !d)}>diff vs parent #{n.parent_id_diffed}</button>}
    </div>
    {codeDiff
      ? <CodeViewer diff={codeDiff} copyText={n.code || ''} label={`Node ${n.id} diff`} />
      : <CodeViewer code={n.code || '(no solution.py — repo task or no code)'} label={`Node ${n.id} code`} />}
    {Object.keys(files).length > 0 && <>
      <div className="section-h">Helper files <span className="pill">{Object.keys(files).length}</span></div>
      {Object.entries(files).map(([fn, c]) => <div key={fn}><div className="muted helper-file-label">{fn}</div><CodeViewer code={c} label={fn} maxHeight={300} /></div>)}
    </>}
  </>
}

// Live online metric curves (loss, recall@k, lr, grad norms, …) read from the node's TensorBoard
// events via the metrics adapters. Polls while the node is still running so the curves fill in as
// training progresses; keyed on n.status so a repair-retrain (pending→failed→pending) re-arms the poll.
export function MetricCurves({ runId, nodeId, status }) {
  const done = ['evaluated', 'failed', 'confirmed'].includes(status)
  const [resource, setResource] = useState(null)
  const [retryNonce, setRetryNonce] = useState(0)
  const requestRef = useRef(0)
  // A terminal node's metrics are immutable — fetch ONCE (ms=null: immediate, no interval) instead of
  // polling every 15s forever. A running node still polls at 3s; a status change (via the `done` dep)
  // re-arms the effect, so a repair-retrain (pending→failed→pending) resumes live polling.
  usePoll((alive) => {
    const request = ++requestRef.current
    get(runNodeApiPath(runId, nodeId, '/metrics')).then(d => {
      if (!d?.metrics || Array.isArray(d.metrics)) throw 0
      if (alive() && request === requestRef.current) setResource(d.metrics)
    }).catch(() => {
      if (alive() && request === requestRef.current) setResource(r => r
        ? Array.isArray(r) ? r : [r] : false)
    })
  }, done ? null : 3000, [runId, nodeId, done, retryNonce], { enabled: nodeId != null })
  const retry = () => {
    if (resource === false) setResource(null)
    setRetryNonce(n => n + 1)
  }
  if (resource === null) return <div className="notice compact" role="status">Loading metric curves…</div>
  const failed = resource === false, stale = Array.isArray(resource)
  return <>
    {(failed || stale) && <div
      className={`notice ${failed ? 'resource-error' : 'resource-warning'} compact`}
      role={failed ? 'alert' : 'status'}>
      {failed ? 'Metric curves unavailable.' : 'Last loaded metric curves; refresh failed.'}
      {' '}<button className="btn sm" onClick={retry}>Retry</button>
    </div>}
    {!failed && <MetricLines series={stale ? resource[0] : resource} />}
  </>
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
    {n.confirmed_mean != null && <div className="kv confirmed-metric">
      <KV k="robust mean ± std" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} over ${n.confirmed_seeds || vals.length} seeds`} /></div>}
    {vals.length > 0 && <>
      <div className="section-h">Per-seed confirmation</div>
      <DataTable caption="Per-seed confirmation metrics" card={false}><table className="tbl"><thead><tr><th>seed</th><th>metric</th></tr></thead>
        <tbody>{vals.map(x => <tr key={x.s}><td>{x.s}</td><td>{fmt(x.v)}</td></tr>)}</tbody></table></DataTable>
    </>}
    <div className="section-h metric-curves-heading">Metric curves
      <span className="muted metric-curves-note">· live logged scalars · grouped</span></div>
    <MetricCurves key={`${runId}:${n.id}`} runId={runId} nodeId={n.id} status={n.status} />
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
    {rowsAll.length > CAP && <button className="btn sm ghost trials-reveal" onClick={() => setShowAll(s => !s)}>
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
