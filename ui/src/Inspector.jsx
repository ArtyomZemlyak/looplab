import React, { useEffect, useId, useMemo, useState, useRef } from 'react'
import { conditionalGet, costPricing, deadlineGet, get, fmt, fmtInt, isSweep, CONTROL,
  commandFeedback, commandCanRetry, createIdempotencyKey, getRunCommand,
  retryRunCommand, runApiPath, runNodeApiPath, submitCommand, traceDeadlineGet, traceGenerationMatches,
  traceReadQuery } from './util.js'
import { useNodeSpanWindow, usePoll, useScopedResource, useTraceRetry, useTraceScroll } from './hooks.js'
import { Trajectory, ParallelCoords, Scatter, MetricLines } from './charts.jsx'
import { themeFilteredGroupAggregate } from './grouping.js'
import { mergeSummary, nodeChip } from './report.js'
import { OpIcon } from './icons.jsx'
import Markdown from './markdown.jsx'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import { nodeFeasibilityStatus, isSalvagedMetricViolation,
  OBJECTIVE_SOURCE_LABEL, objectiveMetricSource, objectiveSourceCaveated,
  objectiveSourceHelp } from './trustSemantics.js'
import { EXTRA_METRIC_CHANNEL_HELP, EXTRA_METRIC_CHANNEL_LABEL,
  extraMetricChannel } from './extraMetrics.js'
import { reviewInspectorTabs } from './runRouteState.js'
import { DataTable, nextRovingIndex } from './accessibility.jsx'
import {
  NODE_TRACE_SPAN_WINDOW, TRACE_PARTIAL_EMPTY_NOTICE, attemptReadRequired, conversationWindow,
  conversationWindowNotice, nodeAttemptOptions, spansOmitted, stageLogAttribution,
  traceDetailState, traceForAttempt,
  traceUnavailable, traceWindow, traceWindowNotice, unavailableTraceDetail,
} from './traceProjection.js'
import { cardTraceNotice, cardTraceSections, researchLinkLabel } from './cardTraceModel.js'
import { stagePipelineView } from './stageAttribution.js'
import {
  EPISODE_MAP_EMPTY, EPISODE_MAP_UNAVAILABLE, buildEpisodeMap, clampEpisodeIndex, episodeAnchor,
  episodeAt, episodeKindOptions, episodeMapNotice, episodePosition, episodeSummary,
  mergeEpisodePagePayload,
} from './traceEpisodeModel.js'
import {
  TRACE_SURFACE_VIEWS, TRACE_SURFACE_VIEW_LABELS,
  TRACE_VIEW_CONVERSATION, TRACE_VIEW_SPANS, nodeTraceSubject, opTraceSubject, traceRequestPath,
  traceSubjectAttempt, traceSubjectBefore, traceSubjectEmptyNotice, traceSubjectHasLogs,
  traceSubjectKey, traceSubjectLead, traceSubjectMatches, traceSubjectSpans, traceSubjectValid,
} from './traceSurfaceModel.js'
import {
  TRACE_SCROLL_BOUNDED, TRACE_SCROLL_LOADING, TRACE_SCROLL_LOADING_LABEL, TRACE_SCROLL_REACH_LABEL,
  TRACE_FAILURE_SUPERSEDED, TRACE_FAILURE_UNREADABLE, TRACE_SCROLL_SETTLED,
  settleTraceRead, traceFailureLabel, traceReadDeadlineMs, traceRetryMs,
  traceScrollBoundedSuffix,
  traceScrollState, traceWidenStalled,
} from './traceScrollModel.js'
import { nodeTheme } from './conceptId.js'
import { nodeCanonicalConcepts, parseConceptTagsInput } from './conceptChips.js'
import { cardText } from './cardBoardModel.js'
import { nodeCardLink, nodeConceptLanes } from './inspectorLinks.js'
import { liveLessonsForNode, nodeLessons } from './derivedMemory.js'
import { conceptMaterializationStatus } from './nodeProjection.js'
import { buildingMarkers } from './buildingModel.js'
import { deadlineRequest } from './requestDeadline.js'
import { createInspectorDraftStore, useInspectorDraftField } from './inspectorDraftStore.js'
import { useTraceClear } from './useTraceClear.js'
import VirtualTimeline from './VirtualTimeline.jsx'
import {
  flattenSpanTree, spanTreeMatches,
} from './spanTreeModel.js'

// Comments are an explicit Inspector interaction. Keep their independently secured
// review transport out of the base DAG closure, then load the same component only when this tab opens.
const CommentsThread = React.lazy(() => import('./CommentsThread.jsx'))

const withoutNodeTrace = value => value && typeof value === 'object'
  ? { ...value, trace: { nodes: [] } }
  : value

// `deadlineGet`'s own default, kept as a named bound now that the detail read spells its deadline
// rather than inheriting one from the helper it no longer calls.
const DETAIL_REQUEST_TIMEOUT_MS = 8000
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
      // `transport` deliberately WITHHOLDS the thrown message here: a reset menu is a dense control
      // surface and the actionable half is "it never reached the server, press it again".
      await submitCommand(CONTROL.resetNode(runId, id, stage, generation), {
        success: `Reset #${id} from ${stage} applied — the engine is processing it`, noop: `#${id} already reflects that reset`,
        executing: `Reset #${id} from ${stage} requested — waiting for the engine`, failure: `Reset #${id} failed`,
        transport: `Reset #${id} could not be submitted. Try again.`,
      }, onToast)
    }
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

// `onOpenLineage` is the Inspector's ONE piece of host awareness, and it is deliberately a callback
// rather than a view name: the Inspector must not learn to branch on which workspace is showing it.
// A host that is already the Lineage graph passes null and the button does not render — an affordance
// that lands you where you are is worse than none. Everyone else (the Card board's detail pane, the
// concept tree's) passes the jump, because from those surfaces "where does this sit in the run?" is
// a real question, and it is exactly the jump the concept tree used to make without being asked.
export default function Inspector({ runId, nodeId, state, live, tab, setTab, onToast, readOnly = false,
  onOpenLineage = null, onOpenCard = null,
  historySeq = null, expectedGeneration = null, readOnlyReason = 'history', evidenceAvailable = true,
  commentsRevision = null, focusCommentId = null, traceClearRecoveryStore: sharedClearStore = null,
  traceClearRecoverySnapshot: sharedClearSnapshot = null,
  publishTraceClearRecovery: publishSharedClearRecovery = null, draftStore: sharedDraftStore = null }) {
  const fallbackDraftStoreRef = useRef(null)
  if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore()
  const draftStore = sharedDraftStore || fallbackDraftStoreRef.current
  const nodeAttempt = state?.nodes?.[nodeId]?.attempt
  const detailScope = `${runId}@${expectedGeneration || '?'}:${nodeId ?? '-'}:${nodeAttempt ?? '?'}:${readOnly
    ? historySeq ?? readOnlyReason : 'live'}:${evidenceAvailable ? 1 : 0}`
  // Accept a detail payload whose attempt is >= the summary's: the /nodes endpoint is often FRESHER
  // than the lagging run-state poll (e.g. right after an inline repair bumps `attempt`), and showing
  // the current truth is correct — only a genuinely STALER payload (an old attempt's late response)
  // should be rejected. Exact-only matching here flashed a spurious "attempt changed" error banner
  // during normal live repairs until the next poll reconciled.
  const detailMatchesAttempt = value => !Number.isSafeInteger(nodeAttempt)
    || (Number.isSafeInteger(value?.attempt)
      && (readOnly ? value.attempt === nodeAttempt : value.attempt >= nodeAttempt))
  const detailMatchesNode = value => value != null && typeof value === 'object' && !Array.isArray(value)
    && String(value.id) === String(nodeId) && typeof value.status === 'string'
  const [traceClearedScopes, setTraceClearedScopes] = useState(() => new Set())
  const detailQuery = []
  if (readOnly && historySeq != null) detailQuery.push(`seq=${historySeq}`)
  if (expectedGeneration) detailQuery.push(`expected_generation=${encodeURIComponent(expectedGeneration)}`)
  const at = detailQuery.length ? `?${detailQuery.join('&')}` : ''
  // This machine is the shared one (doc 25 UI-06) — `useScopedResource.js` over
  // `resourceModel.js`. It was the superset the shared hook had to be designed for, and every part
  // of it survives: the scope fence, `supersede`, `mapLastGood`, `onSettled`, and a status that
  // keeps last-good detail visible-but-stale rather than blanking it. What stays HERE is the part
  // that is genuinely about node detail: which payloads count as this node's, and how each failure
  // reads to an operator.
  const detailResource = useScopedResource(
    signal => get(runNodeApiPath(runId, nodeId, at), { cache: 'no-store', signal }), {
      scope: detailScope,
      timeout: DETAIL_REQUEST_TIMEOUT_MS,
      // Two gates, and the first wins: with no node selected there is nothing to read, and a
      // summary-only review withholds the evidence rather than failing to fetch it.
      gate: nodeId == null ? 'idle'
        : readOnlyReason === 'review' && !evidenceAvailable ? 'restricted' : null,
      validate: value => {
        const valid = detailMatchesNode(value)
        if (valid && traceGenerationMatches(value, expectedGeneration)
            && detailMatchesAttempt(value)) return null
        return valid
          ? 'The experiment attempt changed while details were loading.'
          : 'Full node details returned an invalid response.'
      },
      classifyFailure: ({ transport, message, intent, lastGood }) => ({
        error: intent === 'reconcile'
          ? transport
            ? 'Trace was cleared, but the remaining experiment details could not be refreshed.'
            : String(message).startsWith('The experiment attempt')
              ? 'Trace was cleared, but the experiment attempt changed before details could be refreshed.'
              : 'Trace was cleared, but the detail refresh returned an invalid response.'
          : transport
            ? lastGood == null
              ? 'Full node details could not be loaded.'
              : 'Experiment details could not be refreshed.'
            : message,
      }),
      onSuccess: scope => setTraceClearedScopes(current => {
        if (!current.has(scope)) return current
        const next = new Set(current)
        next.delete(scope)
        return next
      }),
      // The node's own status is deliberately NOT part of detailScope — a status change must
      // re-read the SAME scope (that is what fills the Trace tab in place) rather than reset it.
      deps: [state?.nodes?.[nodeId]?.status],
    })
  const detail = detailResource.data
  const detailStatus = detailResource.status
  const detailError = detailResource.error
  const detailPending = detailResource.pending
  const detailPendingLabel = detailPending === 'retry' ? 'Retrying…'
    : ['refresh', 'reconcile'].includes(detailPending) ? 'Refreshing…' : 'Loading…'
  const detailSurfaceRef = useRef(null)
  const detailFocusScopeRef = useRef(null)
  const fallbackClearStore = useRef(new Map())
  const [fallbackClearSignal, setFallbackClearSignal] = useState({
    scope: null, kind: null, revision: 0,
  })
  const traceClearRecoveryStore = sharedClearStore || fallbackClearStore
  const publishClearRecovery = publishSharedClearRecovery || ((scope, kind) => {
    setFallbackClearSignal(current => ({
      scope, kind, revision: current.revision + 1,
    }))
  })
  const requestDetail = (intent = 'refresh', options) => detailResource.request(intent, options)
  const retryDetailWith = (options = {}) => {
    detailFocusScopeRef.current = detailScope
    // An explicit user retry owns freshness over an invisible background refresh. Superseding it
    // guarantees immediate busy feedback and prevents that older response from removing the focused
    // retry control without passing through the focus-restoration state.
    return requestDetail('retry', { supersede: true, ...options })
  }
  const retryDetail = () => retryDetailWith()
  useEffect(() => {
    if (detailFocusScopeRef.current == null) return
    if (detailFocusScopeRef.current !== detailScope) {
      detailFocusScopeRef.current = null
      return
    }
    if (detailPending || !['ready', 'stale', 'error', 'restricted'].includes(detailStatus)) return
    const frame = requestAnimationFrame(() => {
      const active = document.activeElement
      if (active === document.body || !active?.isConnected) {
        detailSurfaceRef.current?.focus({ preventScroll: true })
      }
      detailFocusScopeRef.current = null
    })
    return () => cancelAnimationFrame(frame)
  }, [detailScope, detailStatus, detailPending])
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
  // Poll ANY pending node the user is inspecting while the engine is active (peer review). "Latest
  // pending" was not an evaluation-ownership test: under eval_parallel>1 several nodes are evaluated
  // concurrently, so inspecting an active OLDER pending node used to disable detail polling and freeze
  // its live Trace/metrics. There is no client-visible eval-ownership marker, so poll the selected
  // pending lifecycle conservatively (the poll is per-inspected-node — it never spins more than the one
  // open node, and a pending node in an active run is genuinely in the eval pipeline).
  const evaluatingThis = nodeStatus === 'pending' && !live?.paused
  // Building = a RAW build marker for this node (buildingMarkers), NOT the spliced `building` flag:
  // withBuilding skips ids already in state.nodes, so a node_reset re-build (which emits node_building
  // for an EXISTING pending node) never sets the spliced flag — the poll then stopped and the Trace tab
  // never showed writing/repairing during the rebuild.
  const buildingThis = buildingMarkers(live).some(m => Number(m?.node_id) === Number(nodeId))
  const nodeWorking = engineActive && (buildingThis || evaluatingThis)
  // Initial load, polling, and manual retries share one scope-owned request. A rejected or invalid
  // refresh therefore keeps last-good detail visible but explicitly stale instead of silently
  // presenting it as current. Returning the owned request lets usePoll abort it during cleanup.
  usePoll(() => requestDetail('refresh'), 4000,
    [runId, nodeId, nodeWorking, detailScope, detailStatus],
    { enabled: !readOnly && nodeWorking && detailStatus === 'ready', immediate: false })

  if (nodeId == null) return <div className="insp-empty">Select a node to inspect its idea, code, metrics, trust, and agent trace.</div>
  const baseNode = detail || state?.nodes?.[nodeId]
  const n = traceClearedScopes.has(detailScope) ? withoutNodeTrace(baseNode) : baseNode
  const visibleDetailStatus = detailStatus
  if (!n) {
    if (visibleDetailStatus === 'error') return <div ref={detailSurfaceRef}
      className="notice resource-error detail-resource-notice" role="alert" tabIndex={-1}>
      <span>{detailError || 'Full node details could not be loaded.'}</span>
      <button type="button" className="btn sm" onClick={retryDetail} disabled={!!detailPending}>
        {detailPending ? detailPendingLabel : 'Retry'}
      </button>
    </div>
    if (visibleDetailStatus === 'restricted') return <div ref={detailSurfaceRef}
      className="insp-empty" role="status" tabIndex={-1}>
      Experiment #{nodeId} is not included in this summary-only review.
    </div>
    return <div ref={detailSurfaceRef} className="insp-empty" role="status" tabIndex={-1}>
      Loading experiment #{nodeId} details…
    </div>
  }
  // Detail may legitimately be one attempt ahead of the run summary. Bind recovery to the exact
  // rendered attempt, not detailScope's lagging summary attempt, so a catch-up poll cannot shed an
  // in-flight clear fence for the same lifecycle.
  const traceClearScope =
    `${runId}@${expectedGeneration || '?'}:${n.id}:${n.attempt ?? '?'}:trace-clear`
  const traceClearRecoverySignal =
    sharedClearSnapshot?.signals?.get(traceClearScope) || fallbackClearSignal
  // Metric-drift is run-level state (state.drifts), each entry tagged with its node_id — the
  // per-node detail payload has no `drifts` key, so filter the run state down to this node.
  // Reset keeps historical audit rows, so only the exact lifecycle may alarm the current Trust tab.
  // Legacy rows had no generation stamp and can only belong to the original (attempt-zero) node.
  const nodeDrifts = (state?.drifts || []).filter(d => d.node_id === n.id
    && (Object.hasOwn(d, 'generation') ? d.generation === n.attempt : n.attempt === 0))
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
        {visibleDetailStatus === 'stale' && <div
          className="notice resource-warning detail-resource-notice" role="status">
          <span>{detailPending
            ? detailPending === 'reconcile'
              ? 'Trace cleared. Refreshing the remaining experiment details…'
              : 'Retrying experiment details… Last loaded details remain visible.'
            : `${detailError || 'Experiment details could not be refreshed.'} Last loaded details remain visible.`}</span>
          <button type="button" className="btn sm" onClick={retryDetail} disabled={!!detailPending}>
            {detailPending ? detailPendingLabel : 'Retry'}
          </button>
        </div>}
        {visibleDetailStatus === 'error' && <div
          className="notice resource-error detail-resource-notice" role="alert">
          <span>{detailError || 'Full node details could not be loaded.'} The summary below may be incomplete.</span>
          <button type="button" className="btn sm" onClick={retryDetail} disabled={!!detailPending}>
            {detailPending ? detailPendingLabel : 'Retry'}
          </button>
        </div>}
        {readOnly
          ? <div className="insp-hint history-inline">{readOnlyReason === 'review'
              ? evidenceAvailable
                ? 'Read-only review with redacted source evidence. Live traces and actions stay hidden.'
                : 'Summary-only review. Source, live traces, and actions are not included.'
              : readOnlyReason === 'start-over'
                ? 'Start over is unresolved. Actions and live traces stay locked until the exact request is recovered.'
                : `Snapshot seq ${historySeq} · read-only. Live traces, metrics sidecars and actions are hidden.`}</div>
          : <div className="insp-hint muted">Run actions (confirm · ablate · fork · promote) stay in chat. Use Comments for review, or attach <button className="ctx-chip ctx-chip-action" title="attach this node to assistant context" onClick={() => window.dispatchEvent(new CustomEvent('ll:attach-node', { detail: { id: n.id } }))}>＋ #{n.id}</button> as context.<ResetBtn runId={runId} id={n.id} generation={n.attempt} onToast={onToast} /></div>}

        {onOpenLineage && <div className="insp-hint">
          <button type="button" className="ctx-chip ctx-chip-action"
            title="open the Lineage graph with this experiment selected, among its parents and children"
            onClick={() => onOpenLineage(n.id)}>↗ Show #{n.id} in Lineage</button>
          <span className="muted"> — the experiment graph, with this node selected.</span>
        </div>}

        {activeTab === 'Overview' && <Overview n={n} state={state} runId={readOnly ? null : runId}
          onToast={onToast} draftStore={draftStore} expectedGeneration={expectedGeneration}
          onOpenCard={onOpenCard} />}
        {activeTab === 'Comments' && <CommentsThread runId={runId} nodeId={n.id}
          nodeGeneration={n.attempt} expectedGeneration={expectedGeneration} refreshKey={commentsRevision}
          readOnly={readOnly} reviewMode={readOnlyReason === 'review'} focusCommentId={focusCommentId}
          draftStore={draftStore} draftSurface="inspector" />}
        {activeTab === 'Trials' && <Trials n={n} detail={detail} state={state} />}
        {activeTab === 'Trace' && <Trace key={`trace:${detailScope}:${n.attempt ?? 'pending'}`}
          n={n} runId={runId} expectedGeneration={expectedGeneration} onOpenCard={onOpenCard}
          expectedTraceRevision={n.trace_revision}
          live={live} working={nodeWorking}
          detailStatus={detailStatus} reloadPending={!!detailPending}
          clearScope={traceClearScope} clearRecoveryStore={traceClearRecoveryStore}
          recoverClearState={traceClearRecoveryStore.current.get(traceClearScope) || null}
          clearRecoverySignal={traceClearRecoverySignal}
          publishClearRecovery={publishClearRecovery}
          onReload={reason => {
            if (reason === 'trace-cleared') {
              setTraceClearedScopes(current => {
                if (current.has(detailScope)) return current
                const next = new Set(current)
                next.add(detailScope)
                return next
              })
              return requestDetail('reconcile', {
                supersede: true,
                mapLastGood: withoutNodeTrace,
                onSettled: ok => {
                  if (ok) {
                    traceClearRecoveryStore.current.delete(traceClearScope)
                    publishClearRecovery(traceClearScope, 'refresh-succeeded')
                    return
                  }
                  const message = {
                    kind: 'error',
                    blocking: true,
                    text: 'Trace was cleared, but experiment details could not be refreshed. Clear remains unavailable until a refresh succeeds.',
                  }
                  traceClearRecoveryStore.current.set(
                    traceClearScope, { phase: 'blocked', message })
                  publishClearRecovery(traceClearScope, 'refresh-failed')
                },
              })
            }
            if (reason === 'trace-clear-recovery') {
              return retryDetailWith({
                onSettled: ok => {
                  if (ok) {
                    traceClearRecoveryStore.current.delete(traceClearScope)
                    publishClearRecovery(traceClearScope, 'refresh-succeeded')
                    return
                  }
                  const message = {
                    kind: 'error',
                    blocking: true,
                    text: 'Experiment refresh did not complete. Trace clear remains unavailable until a refresh succeeds.',
                  }
                  traceClearRecoveryStore.current.set(
                    traceClearScope, { phase: 'blocked', message })
                  publishClearRecovery(traceClearScope, 'refresh-failed')
                },
              })
            }
            return reason === 'retry' ? retryDetail() : requestDetail('refresh')
          }} />}
        {activeTab === 'Code' && (['ready', 'stale'].includes(visibleDetailStatus)
          ? <Code n={n} draftStore={draftStore}
              draftScope={`code:${runId}@${expectedGeneration || '?'}:${n.id}:${n.attempt ?? '?'}`} />
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
      <h2 className="tab active group-summary-title" tabIndex={-1}
        data-group-summary-title>Group · {groupKey}</h2>
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
              <td><button type="button" className="btn xs ghost" data-group-member-id={n.id}
                aria-label={`Open experiment #${n.id}`} onClick={() => onSelectNode(n.id)}>#{n.id}</button></td>
              <td>{n.operator}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td><td>{n.status}</td></tr>)}</tbody></table></DataTable>
        </>}
    </div>
  </>
}

// Phase 1: the node's declared eval pipeline as a coloured strip (data_prep ✓ → train ✓ → eval ✗), so a
// crash is pinpointed to its stage instead of hiding behind one opaque "evaluate". Empty on single-command
// evals. The failed stage is tinted red; a still-pending tail (not yet reached) shows muted.
//
// Every DECISION lives in `stageAttribution.js` (tone, glyph, title, which rows a later repair has
// superseded and the sentence that says so) — see that module's header for the measurement. This
// half keeps only the choreography: the re-run submit and its pending lock.
function StagePipeline({ node, runId, id, generation, onToast }) {
  const [pendingStage, setPendingStage] = useState(null)
  const { rows, notice, failedStage } = stagePipelineView(node)
  if (!rows.length) return null
  const rerun = async (name) => {
    if (!runId || pendingStage) return
    setPendingStage(name)
    try {
      await submitCommand(CONTROL.resetNode(runId, id, name, generation), {
        success: `Reset #${id} from '${name}' applied — the engine is processing it`, noop: `#${id} already reflects that reset`,
        executing: `Re-run of #${id} from '${name}' requested — waiting for the engine`, failure: 'Re-run failed',
        transport: 'Re-run could not be submitted. Try again.',
      }, onToast)
    }
    finally { setPendingStage(null) }
  }
  return <div className="eval-pipeline">
    <div className="muted eval-pipeline-label">
      eval pipeline{failedStage ? ` — failed at ${failedStage}` : ''}{runId ? ' · click a stage to re-run from there' : ' · historical result (read-only)'}</div>
    {/* The supersession notice sits ABOVE the chips and is `role="status"`, not a title: what an
        operator asked for is a sign that a red strip is not about the attempt that is running, and
        a tooltip is not a sign. It is absent entirely when nothing is superseded, so an unrepaired
        node's strip is exactly the historical one. */}
    {notice && <div className="eval-pipeline-superseded" role="status">
      <span className="badge reason" title={notice.text}>superseded</span>
      <span> {notice.text}{notice.failureText ? ` ${notice.failureText}` : ''}</span>
    </div>}
    <div className="eval-pipeline-stages">
      {/* A superseded row's glyph no longer names its own outcome (stageAttribution.js:
          STAGE_SUPERSEDED_ICON), so it — and only it — carries the status as sr-only text. Every
          other row's markup is unchanged, which is what keeps the unrepaired strip byte-identical. */}
      {rows.map((v, i) => <React.Fragment key={i}>
        {runId ? <button type="button" disabled={pendingStage != null} onClick={() => rerun(v.name)}
          className={'eval-pipeline-step' + (v.superseded ? ' superseded' : '')} style={{ '--stage-tone': v.tone }}
          title={`${v.title} — click to re-run the pipeline FROM here (reuse earlier stages)`}>
          {v.icon}{v.iconLabel && <span className="sr-only"> {v.iconLabel}</span>} {v.name}</button> : <span
          className={'eval-pipeline-step' + (v.superseded ? ' superseded' : '')} style={{ '--stage-tone': v.tone }}
          title={`${v.title} · historical result`}>
          {v.icon}{v.iconLabel && <span className="sr-only"> {v.iconLabel}</span>} {v.name}</span>}
        {i < rows.length - 1 && <span className="muted eval-pipeline-arrow">→</span>}
      </React.Fragment>)}
    </div>
  </div>
}

// PART V Phase 2c: the node's concept tags with a direct operator re-tag affordance. The tags are
// displayed exactly as on the Dag (canonical, de-duped); on a LIVE run with an AUTHORITATIVE concept
// projection an operator can replace the whole set, which folds with `operator-edited` provenance the
// classifier re-tag cadence must not clobber (docs/guide/concepts.md). Read-only history (runId null),
// a partial/unavailable projection, and a still-building node stay display-only — a fabricated "current"
// set must never be presented as something to overwrite.
const commandRecordPending = record =>
  record?.status === 'accepted' || record?.status === 'executing'
const recoveryCommandRecord = (error, boundRecord) => {
  const observed = error?.commandRecord
  if (!observed || error?.code === 'COMMAND_PROTOCOL_ERROR') return boundRecord
  if (boundRecord?.id && observed.id !== boundRecord.id) return boundRecord
  return observed
}

function ConceptTags({ n, state, runId, onToast, draftStore, expectedGeneration }) {
  const draftScope = `concept-tags:${runId}@${expectedGeneration || '?'}:${n.id}:${n.attempt ?? '?'}`
  const [editing, setEditing] = useInspectorDraftField(draftStore, draftScope, 'editing', false)
  const [text, setText] = useInspectorDraftField(draftStore, draftScope, 'text', '')
  const [busy, setBusy] = useInspectorDraftField(draftStore, draftScope, 'busy', false)
  const [baseline, setBaseline] = useInspectorDraftField(draftStore, draftScope, 'baseline', null)
  const [intent, setIntent] = useInspectorDraftField(draftStore, draftScope, 'intent', null)
  const [error, setError] = useInspectorDraftField(draftStore, draftScope, 'error', '')
  const [messageKind, setMessageKind] = useInspectorDraftField(
    draftStore, draftScope, 'messageKind', '')
  const areaRef = useRef(null)
  const triggerRef = useRef(null)
  const focusEditorRef = useRef(false)
  // The editor is keyed and stored on the complete run-generation/node-attempt identity. Temporary
  // Inspector unmounts resume that exact scope; a replacement run or reset node starts clean.
  const current = useMemo(
    () => nodeCanonicalConcepts(state?.node_concepts || {}, n.id, state?.concept_consolidation || {}),
    [state?.node_concepts, state?.concept_consolidation, n.id])
  const currentKey = current.join('\n')
  const status = conceptMaterializationStatus(state, n.id)
  // Editable only for a SETTLED experiment (terminal lifecycle) with an authoritative complete concept
  // projection. `status === 'complete'` is concept-PROJECTION completeness, NOT node lifecycle — a
  // still-building or reset-rebuilding node folds back to `pending` yet can keep a prior 'complete'
  // projection, so gating on the projection alone would wrongly expose Edit on a node whose concepts
  // aren't settled. Require a terminal node status too; read-only history (runId null) stays display-only.
  const canEdit = !!runId && /^[0-9a-f]{64}$/.test(expectedGeneration || '') && status === 'complete'
    && (n.status === 'evaluated' || n.status === 'failed')
  const baselineChanged = editing && baseline != null && baseline !== currentKey
  const exactIntent = intent?.text === text && intent?.baseline === baseline
  const operationFenced = !!intent && (intent.unknown === true || commandRecordPending(intent.record))
  const exactRetry = exactIntent && commandCanRetry(intent?.record)
  useEffect(() => {
    // Restoring a conditional Inspector must not steal focus from the control that remounted it.
    if (!editing || !focusEditorRef.current) return
    focusEditorRef.current = false
    requestAnimationFrame(() => areaRef.current?.focus())
  }, [editing])
  const open = () => {
    focusEditorRef.current = true
    setText(currentKey); setBaseline(currentKey); setIntent(null); setError('')
    setMessageKind(''); setEditing(true)
  }
  const cancel = () => {
    // A pending/unknown full replacement can still apply; keep its identity until terminal state.
    if (busy || operationFenced) {
      onToast?.('Check the re-tag command before closing this draft.')
      return
    }
    draftStore.clear(draftScope)
    requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }))
  }
  const copyPendingInput = async () => {
    try {
      await navigator.clipboard.writeText(intent?.text || '')
      onToast?.('Pending re-tag input copied.')
    } catch {
      onToast?.('Clipboard is unavailable. The submitted tags remain visible in the editor.')
    }
  }
  const save = async () => {
    if (busy || (!canEdit && !operationFenced)) return
    let submission
    let concepts, dropped
    if (operationFenced) {
      // Check the exact earlier operation even if the operator edited the visible next draft.
      submission = intent
      concepts = submission.concepts
      dropped = submission.dropped || 0
    } else {
      if (baselineChanged) return
      const parsed = parseConceptTagsInput(text)
      concepts = parsed.concepts
      dropped = parsed.dropped
      // "The operator cleared the tags" and "every token they typed was rejected" are NOT the same
      // intent. An explicit clear is blank input; a fully-rejected input is a typo to correct.
      if (concepts.length === 0 && dropped > 0) {
        onToast?.('No valid concept IDs — fix the input, or clear it to remove every tag.')
        return
      }
      submission = exactRetry ? intent : {
        text, baseline, concepts, dropped, idempotencyKey: createIdempotencyKey(),
        record: null, unknown: false,
      }
    }
    const checking = operationFenced
    const retrying = !checking && commandCanRetry(submission.record)
    const observing = checking && submission.record?.id && commandRecordPending(submission.record)
    let closeAfterSuccess = false
    setIntent(submission)
    setError('')
    setMessageKind('')
    setBusy(true)
    try {
      const record = observing
        ? await getRunCommand(runId, submission.record.id)
        : retrying
          ? await retryRunCommand(runId, submission.record.id, { waitMs: 12_000 })
          : await CONTROL.retagConcepts(
          runId,
          { nodeId: n.id, nodeGeneration: n.attempt, concepts: submission.concepts },
          {
            expectedGeneration,
            idempotencyKey: submission.idempotencyKey,
            waitMs: 12_000,
          },
        )
      const feedback = commandFeedback(
        record, {
          success: `Re-tagged #${n.id} → ${concepts.length} concept${concepts.length === 1 ? '' : 's'}`
            + `${dropped ? ` (${dropped} invalid dropped)` : ''} — the engine is processing it`,
          noop: `#${n.id} already carries exactly those concepts`,
          executing: `Re-tag of #${n.id} requested — waiting for the engine`,
          failure: `Re-tag of #${n.id} failed`,
        })
      onToast?.(feedback.message)
      if (feedback.kind === 'pending') {
        setIntent({ ...submission, record, unknown: false })
        setError(feedback.message)
        setMessageKind('status')
      } else if (feedback.kind === 'success') {
        if (text === submission.text) {
          closeAfterSuccess = true
          requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }))
        } else {
          setIntent(null)
          setError('The earlier re-tag completed. Review the current tags before saving this newer draft.')
          setMessageKind('status')
        }
      } else {
        const sameDraft = text === submission.text && baseline === submission.baseline
        setIntent(commandCanRetry(record) && sameDraft
          ? { ...submission, record, unknown: false }
          : null)
        setError(feedback.message)
        setMessageKind('error')
      }
    } catch (caught) {
      const record = recoveryCommandRecord(caught, submission.record)
      const pending = commandRecordPending(record)
      const unknown = caught?.commandUnknown === true
        // A read error cannot prove that the already-observed write did not apply. Preserve its
        // identity across access, abort, missing and protocol failures until a valid terminal record.
        // The same applies to an id-less exact-key replay: a rejected recovery request says nothing
        // about whether the original request reached the durable command store.
        || pending || checking
      const sameDraft = text === submission.text && baseline === submission.baseline
      setIntent(unknown
        ? { ...submission, record, unknown: !pending }
        : commandCanRetry(record) && sameDraft
          ? { ...submission, record, unknown: false }
          : null)
      const message = unknown
        ? `Re-tag of #${n.id} has an uncertain outcome. Retry will reuse the same command identity.`
        : `Re-tag of #${n.id} could not be submitted. Your draft is preserved.`
      setError(message)
      setMessageKind('error')
      onToast?.(message)
    }
    finally {
      // Avoid recreating an empty entry by writing busy=false after clearing a completed scope.
      if (closeAfterSuccess) draftStore.clear(draftScope)
      else setBusy(false)
    }
  }
  return <>
    <div className="section-h">Concepts{status === 'partial' ? ' · partial (display-only)' : ''}
      {canEdit && !editing && <button ref={triggerRef} type="button" className="ctx-chip ctx-chip-action"
        title="replace this experiment's concept tags (operator authoring; the classifier re-tag will not overwrite it)"
        onClick={open}>✎ Edit tags</button>}
    </div>
    {!editing && (current.length
      ? <div className="node-concepts-list">{current.map(c =>
          <span key={c} className="nc-tag" title={c}>{c}</span>)}</div>
      : <div className="muted">{status === 'unavailable' ? 'concepts unavailable'
          : status === 'partial' ? 'none retained (partial projection)' : 'none'}</div>)}
    {editing && <div className="concept-tag-editor">
      <label className="muted" htmlFor={`ct-${n.id}`}>One concept id per line (or comma-separated),
        e.g. <code>loss/contrastive</code>. Invalid ids are dropped.</label>
      <textarea id={`ct-${n.id}`} ref={areaRef} className="concept-tag-input" rows={4} value={text}
        disabled={busy}
        aria-describedby={operationFenced ? `ct-${n.id}-command-hint` : undefined}
        onChange={event => {
          const next = event.target.value
          setText(next)
          if (intent && !operationFenced && next !== intent.text) setIntent(null)
          if (error && !operationFenced) { setError(''); setMessageKind('') }
        }}
        onKeyDown={event => {
          if (event.key !== 'Escape') return
          event.preventDefault()
          cancel()
        }} />
      {baselineChanged && <div className="notice warn compact concept-tag-recovery">
        <span role="status">Concept tags changed after this draft started. Review the latest set before replacing it.</span>
        <div className="concept-tag-latest"><b>Latest tags:</b> {current.length ? current.join(', ') : 'none'}</div>
        {operationFenced && <span className="muted">
          Check the earlier command before choosing a baseline for this next draft.
        </span>}
        <div className="concept-tag-recovery-actions">
          <button type="button" className="btn xs" disabled={busy || operationFenced} onClick={() => {
            setText(currentKey)
            setBaseline(currentKey)
            setIntent(null)
            setError('')
            setMessageKind('')
            onToast?.('Latest tags loaded into the editor.')
          }}>Use latest</button>
          <button type="button" className="btn xs" disabled={busy || operationFenced} onClick={() => {
            setBaseline(currentKey)
            setIntent(null)
            setError('')
            setMessageKind('')
            onToast?.('Latest tags acknowledged. Your draft remains in the editor.')
          }}>Continue with my draft</button>
        </div>
      </div>}
      {error && <div className={`notice compact ${messageKind === 'status' ? 'warn' : 'resource-error'}`}
        role={messageKind === 'status' ? 'status' : 'alert'}>{error}</div>}
      {operationFenced && <div className="concept-command-recovery"
        aria-label="Pending concept command recovery">
        <span id={`ct-${n.id}-command-hint`} className="muted">
          The earlier re-tag is still unresolved. Check that command before closing this draft.
        </span>
        <div className="concept-tag-latest"><b>Submitted tags:</b>{' '}
          {intent.concepts?.length ? intent.concepts.join(', ') : 'none (clear all)'}
        </div>
        <button type="button" className="btn xs" onClick={copyPendingInput}>
          Copy pending input
        </button>
      </div>}
      <div className="concept-tag-actions">
        <button type="button" className="btn sm"
          disabled={busy || (!operationFenced && (baselineChanged || !canEdit))}
          aria-describedby={operationFenced ? `ct-${n.id}-command-hint` : undefined}
          onClick={save}>
          {busy ? (operationFenced ? 'Checking…' : exactRetry ? 'Retrying…' : 'Saving…')
            : operationFenced ? 'Check command' : exactRetry ? 'Retry same command' : 'Save tags'}</button>
        <button type="button" className="btn sm ghost" disabled={busy || operationFenced}
          aria-describedby={operationFenced ? `ct-${n.id}-command-hint` : undefined}
          title={operationFenced ? 'Check the pending re-tag before closing this draft' : undefined}
          onClick={cancel}>Cancel</button>
      </div>
    </div>}
  </>
}

// The Card this experiment tested — item 3's "links to hypotheses", and the correction to the
// standing misreading that one node IS one hypothesis. Since Cards absorbed Hypothesis, the Card IS
// the hypothesis (`core/cards.py`: "1 card = 1 hypothesis"), so the honest shape of this section is
// "the question, its verdict, and how many OTHER attempts there were" — never "the hypothesis of this
// node". `inspectorLinks.js::nodeCardLink` owns which of its four answers applies.
function CardLink({ link, onOpenCard }) {
  if (link.kind === 'none') {
    return <><div className="section-h">Work item (hypothesis)</div>
      <div className="muted">This experiment carries no work-item stamp. A node may belong to no
        Card (<code>Idea.card_id</code> is optional), so this is a fact about the node, not a
        missing link.</div></>
  }
  if (link.kind === 'unknown') {
    return <><div className="section-h">Work item (hypothesis)</div>
      <div className="muted">Stamped <b>{link.cardId}</b>, which the displayed board does not
        publish — a historical snapshot, or beyond the published card cap. The stamp is durable; the
        record is simply not in this frame.</div></>
  }
  const { card, cardId, summary } = link
  // `seed_statement` is the IMMUTABLE statement captured at `card_added` and the join key the whole
  // Card ledger keys on; `statement` is an operator-editable DISPLAY overlay. When they differ, the
  // operator paraphrased the question — and only showing the paraphrase hides that.
  const seed = cardText(card.seed_statement)
  const shown = cardText(card.statement)
  const paraphrased = !!seed && !!shown && seed !== shown
  return <>
    <div className="section-h">Work item (hypothesis)
      {onOpenCard && <button type="button" className="ctx-chip ctx-chip-action"
        title="open this work item on the Cards board, with its full record and every attempt"
        onClick={() => onOpenCard(cardId)}>↗ Open {cardId}</button>}
    </div>
    <div className="v">{shown || seed || cardId}</div>
    {paraphrased && <div className="muted">Seed statement (immutable, the join key): {seed}</div>}
    <div className="node-concepts-list">
      <span className="chip xs">{cardId}</span>
      {cardText(card.verdict) && cardText(card.verdict) !== 'open'
        && <span className="chip xs">verdict · {card.verdict}</span>}
      {cardText(card.status) && <span className="chip xs">{card.status}</span>}
      {card.best_delta != null && <span className="chip xs">best Δ {fmt(card.best_delta)}</span>}
    </div>
    {/* The count IS the correction. One card, N attempts — including attempts that are merely
        reserved for it and are not evidence yet, which is why the union and not `card.evidence`. */}
    <div className="muted">{summary.total === 1
      ? 'This is the only attempt at this work item.'
      : `${summary.total} attempts at this work item — this one and ${summary.total - 1} other${summary.total === 2 ? '' : 's'}.`}
      {summary.evidence > 0 && ` ${summary.evidence} in its evidence list`}
      {summary.ownedOnly > 0 && `, ${summary.ownedOnly} reserved but not evidence yet`}
      {summary.missing > 0 && `, ${summary.missing} not in this snapshot`}.
    </div>
  </>
}

const LESSON_STORE_TIMEOUT_MS = 8000
const LESSON_STANDING = {
  present: ['still in memory', 'A live cross-run lesson still carries this exact statement under this run.'],
  absorbed: ['no longer in memory as written',
    'Consolidation merged this statement into another row, compaction dropped it, or a re-evaluation retired it. '
    + 'The store keeps no record that distinguishes those, and no redirect to a descendant.'],
  unknown: ['standing unknown',
    'The cross-run store was not read, or only its recent tail was, so absence here is not evidence of absence.'],
}

// Item 4. What this experiment TAUGHT, and why it is two things rather than one — see the long note
// at the head of `derivedMemory.js`. The spine is the run's own append-only event log, which cannot go
// stale; the live cross-run store supplies only each row's STANDING, fetched at read time because it
// is a mutable file outside the event log that merges and consolidates behind us.
//
// The store read is deliberately lazy AND conditional: nothing is fetched for a node the event log
// credits with no lessons, which is most nodes. So the common Inspector open costs zero requests, and
// a node that did teach something costs one bounded run-scoped read.
function DerivedMemory({ n, state, runId }) {
  const history = useMemo(() => nodeLessons(state, n.id, n.attempt, null), [state?.lessons_distilled, n.id, n.attempt])
  // The store read is worth making for a node with no event-log lesson too, because consolidation
  // runs the other way: measured across `runs/`, the surviving MERGED row usually still credits the
  // node whose own lesson was absorbed. Gated on the run having distilled anything at all, so a run
  // with no lessons still costs nothing.
  const wanted = (history.length > 0 || (state?.lessons_distilled || []).length > 0) && !!runId
  const storeResource = useScopedResource(
    signal => get(`/api/memory?run_id=${encodeURIComponent(runId)}`, { cache: 'no-store', signal }), {
      scope: `node-memory:${runId}`,
      timeout: LESSON_STORE_TIMEOUT_MS,
      // Not "no lessons to check" as an error — it is simply no reason to spend a request.
      gate: wanted ? null : 'idle',
      validate: value => value && typeof value === 'object' && Array.isArray(value.lessons)
        ? null : 'Cross-run memory returned an invalid response.',
      classifyFailure: () => ({ error: 'Cross-run memory could not be read, so these lessons’ current standing is unknown.' }),
    })
  // The RAW body, not `panels.jsx::memoryPayload`'s normalized one: `derivedMemory.js` reads the
  // receipt in either spelling, and importing the Memory panel's validator would pull `panels.jsx`
  // into the Inspector's chunk for one field rename.
  const store = ['ready', 'stale'].includes(storeResource.status) ? storeResource.data : null
  const rows = useMemo(() => nodeLessons(state, n.id, n.attempt, store),
    [state?.lessons_distilled, n.id, n.attempt, store])
  const live = useMemo(
    () => liveLessonsForNode(store, n.id, n.attempt, runId), [store, n.id, n.attempt, runId])
  if (!history.length && !live.length) return null
  return <>
    {live.length > 0 && <>
      <div className="section-h">What memory still says about this experiment</div>
      <div className="muted">Live cross-run lessons whose recorded evidence names this experiment.
        These are the current text, re-read each time — consolidation may have rewritten them.</div>
      <ul className="bul">{live.map(row => <li key={`live:${row.statement}`}>
        <span className="v">{row.statement}</span>
        <div className="node-concepts-list">
          {row.outcome && <span className="chip xs">{row.outcome}</span>}
          {row.attemptMatch === 'unrecorded' && <span className="chip xs warn"
            title="This row records no node attempt for this experiment, so it may have been drawn from an earlier attempt of the same id.">attempt not recorded</span>}
          {/* The visible fingerprint of a merge: more agreeing observations than traceable ids. */}
          {row.evidenceCount != null && row.evidenceCount > row.alsoFrom.length + 1
            && <span className="chip xs" title="Consolidation keeps only a count when it merges rows from other runs; their own evidence is not retained.">
              {row.evidenceCount} agreeing observations, {row.alsoFrom.length + 1} still traceable</span>}
          {row.concepts.map(id => <span key={id} className="nc-tag">{id}</span>)}
        </div>
        {row.alsoFrom.length > 0 && <div className="muted">Also credits {row.alsoFrom.map(id => `#${id}`).join(', ')}.</div>}
      </li>)}</ul>
    </>}
    {history.length > 0 && <>
    <div className="section-h">What this experiment taught</div>
    <div className="muted">From this run’s own event log, which is append-only — so this list is what
      the experiment produced, not what memory happens to hold now. Each row’s standing below is read
      live from cross-run memory, because that store merges and consolidates.</div>
    <ul className="bul">{rows.map(row => {
      const [label, why] = LESSON_STANDING[row.storeStatus] || LESSON_STANDING.unknown
      return <li key={row.lessonId || row.statement}>
        <span className="v">{row.statement}</span>
        <div className="node-concepts-list">
          {row.outcome && <span className="chip xs">{row.outcome}</span>}
          {row.claimStance && <span className="chip xs">{row.claimStance}</span>}
          {row.atNode != null && <span className="chip xs">distilled at #{row.atNode}</span>}
          <span className={'chip xs' + (row.storeStatus === 'present' ? ' ok' : row.storeStatus === 'absorbed' ? ' warn' : '')}
            title={why}>{label}</span>
        </div>
        {row.alsoFrom.length > 0 && <div className="muted">Drawn from this experiment together
          with {row.alsoFrom.map(id => `#${id}`).join(', ')}.</div>}
      </li>
    })}</ul>
    </>}
    {storeResource.status === 'error' && <div className="muted">{storeResource.error}</div>}
    {/* The run-end whole-run reflection is a DIAGNOSTIC event: not folded, not on the wire, and its
        projection carries neither a lesson id nor evidence. For most runs those are the MAJORITY of
        lessons, and they can only ever be attributed to the run. Saying so is the honest close to
        this section — the alternative is a list that silently claims to be complete. */}
    <div className="muted">Run-end reflection lessons are recorded without per-experiment evidence and
      cannot appear here; open Lab → Memory to read them at run level.</div>
  </>
}

function Overview({ n, state, runId, onToast, draftStore, expectedGeneration, onOpenCard }) {
  const p = n.idea?.params || {}
  const uses = mergeSummary(n, state.nodes || {}, state)   // E3: for merges, which technique each parent fused
  const chg = nodeChip(n, state.nodes || {}, state)        // same chip as the card (sweep-aware; '' for merges)
  const cardLink = nodeCardLink(state, n)
  // Item 5: the node's OWN concepts already had a section (`ConceptTags`, editable). What was absent
  // is the Card's `concept_tags`, a separately-derived set that can name concepts this attempt never
  // carried — so it renders as its own lane, never folded into the node's list.
  const lanes = nodeConceptLanes(
    nodeCanonicalConcepts(state?.node_concepts || {}, n.id, state?.concept_consolidation || {}),
    cardLink.card)
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
    <CardLink link={cardLink} onOpenCard={onOpenCard} />
    <ConceptTags key={`${runId}:${expectedGeneration || '?'}:${n.id}:${n.attempt}`} n={n} state={state} runId={runId}
      onToast={onToast} draftStore={draftStore} expectedGeneration={expectedGeneration} />
    {lanes.cardOnly.length > 0 && <>
      <div className="section-h">Also on its work item</div>
      {/* Kept OUT of the node's own list on purpose: a card tag is derived from the card's FIRST
          linked node and carried across merges, so presenting it beside this node's memberships
          would silently upgrade a card-level claim into a claim about this attempt's evidence. */}
      <div className="node-concepts-list">{lanes.cardOnly.map(c =>
        <span key={c} className="nc-tag" title={`${c} — carried by the work item, not by this experiment`}>{c}</span>)}</div>
      <div className="muted">Concepts its work item carries that this attempt does not
        {lanes.cardTagOrigin ? ` (card tags derived from: ${lanes.cardTagOrigin})` : ''}.</div>
    </>}
    <StagePipeline node={n} runId={runId} id={n.id} generation={n.attempt} onToast={onToast} />
    {chg && <><div className="section-h">What this node did</div><div className="v">{chg}</div></>}
    {uses.length > 0 && <><div className="section-h">Merge — techniques fused</div>
      <ul className="bul">{uses.map(u => <li key={u.parentId}>
        <b>#{u.parentId}</b>{u.theme ? ` · ${u.theme}` : ''}{u.change && u.change !== '—' ? ` — ${u.change}` : ''}</li>)}</ul></>}
    <div className="section-h">Idea params</div>
    {Object.keys(p).length ? <div className="kv">{Object.entries(p).map(([k, v]) => <KV key={k} k={k} v={fmt(v)} />)}</div> : <div className="muted">none</div>}
    {n.idea?.rationale && !(chg && chg.includes(n.idea.rationale)) && <><div className="section-h">Rationale</div><Markdown className="rationale-md" text={n.idea.rationale} /></>}
    <DerivedMemory n={n} state={state} runId={runId} />
    {n.deleted?.length > 0 && <><div className="section-h">Deleted files</div><div className="v">{n.deleted.join(', ')}</div></>}
  </>
}

// Trace timeline bounds: earliest start + total wall-span across the forest, so every span bar can be
// positioned by its OFFSET from t0 (a langfuse-style waterfall) rather than just sized by duration.
function traceBounds(spans) {
  let lo = Infinity, hi = 0
  const stack = [...(spans || [])]
  while (stack.length) {
    const s = stack.pop()
    const st = (typeof s.start === 'number') ? s.start : null
    const en = st != null ? st + (s.duration_s || 0) : (s.duration_s || 0)
    if (st != null && st < lo) lo = st
    if (en > hi) hi = en
    stack.push(...(s.children || []))
  }
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
  // The Card lane's name for the SAME work `implement` is on the serial path — one producer turn
  // that plans, declares the stages and writes the code. Same tone on purpose: an operator reading
  // two runs side by side must not have to learn that they are the same block.
  card_build:   ['gear', 'Developer · build', 'write / edit the solution code (Card lane)', '#6fae97'],
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
  const stack = [s]
  while (stack.length) {
    const x = stack.pop()
    if (x.kind === 'generation') { calls++; const u = (x.attributes || {}).usage || {}; const p = u.prompt || 0; tok += (u.total != null ? u.total : p + (u.completion || 0)); ctx = Math.max(ctx, p); out += u.completion || 0 }
    stack.push(...(x.children || []))
  }
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
function GenBody({ c, thinkOpen, onThink }) {
  const think = thinkOpen === true
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
        onClick={() => onThink(!think)}>
        {think ? '▾' : '▸'} reasoning (debug)</button>
      {think && <Markdown className="think-body" text={c.thinking} />}</div>}
  </div>
}

const CONVERSATION_TURN_CAP = 60
const spanTreeKey = row => row.key

const spanAttrs = span => Object.entries(span.attributes || {}).filter(([key]) => key !== 'node_id')
const spanHasDetail = row => {
  const kind = row.span.kind || 'operation'
  return kind === 'generation' || kind === 'tool'
    || spanAttrs(row.span).length > 0 || (row.span.events || []).length > 0
}
const pruneSpanState = (current, live) => {
  const next = new Map([...current].filter(([key]) => live.has(key)))
  return next.size === current.size ? current : next
}

function SpanFacts({ span }) {
  const attrs = spanAttrs(span)
  const events = span.events || []
  return <>
    {attrs.length > 0 && <div className="kv">{attrs.map(([key, value]) =>
      <KV key={key} k={key} v={typeof value === 'object' ? JSON.stringify(value) : String(value)} />)}</div>}
    {events.map((event, index) => <div key={index} className="span-ev">
      <span className="ty">{event.name}</span>{event.error ? <span className="flag"> {event.error}</span> :
        <span className="muted"> {Object.entries(event).filter(([key]) => key !== 'name')
          .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`).join(' ')}</span>}
    </div>)}
  </>
}

export function TraceUnavailable({ label = 'Trace unavailable.', onRetry, pending = false }) {
  return <div className="notice resource-error compact" role="alert">
    <span>{label}</span>
    {onRetry && <button type="button" className="btn sm" onClick={onRetry} disabled={pending}>
      {pending ? 'Retrying…' : 'Retry trace'}
    </button>}
  </div>
}

// One span and its subtree, drawn as a langfuse-style waterfall row: the bar is positioned by the
// span's OFFSET from the trace start (t0) and sized by its duration, so sequence reads at a glance.
// Renders three observation kinds distinctly — GENERATION (an LLM call: op·model·in→out·preview, its
// prompt/output on expand), TOOL (name·arg, its input/output on expand), and OPERATION (a phase of
// work) — so the tree shows exactly what called what and what each bounded projection produced.
function SpanRow({ row, t0, total, runId, expectedGeneration, open, io,
  onToggle, onActivate, onIo, detailId, thinkOpen, onThink, parentOp }) {
  const s = row.span, depth = Math.max(0, row.level - 2)
  const kind = s.kind || 'operation'
  const err = s.status === 'ERROR'
  const off = (typeof s.start === 'number') ? Math.max(0, (s.start - t0) / total * 100) : 0
  const wid = Math.max(1.5, (s.duration_s || 0) / total * 100)
  const barTone = err ? 'var(--fail)' : kind === 'generation' ? 'var(--accent)' : kind === 'tool' ? 'var(--working)' : stageMeta(s.name)[3]
  const bar = <span className="span-bar"><span className="span-fill" style={{ marginLeft: Math.min(98, off) + '%', width: wid + '%', background: barTone }} /></span>
  const rowIndent = { paddingLeft: depth * 14 }
  const detailIndent = { marginLeft: depth * 14 + 16 }
  // On first expand, pull the bounded/redacted detail projection; its omission receipt is rendered.
  usePoll((alive) => {
    const request = traceDeadlineGet(
      runApiPath(runId, `/spans/${encodeURIComponent(s.span_id)}`), expectedGeneration)
    request.promise.then(d => {
      if (!traceGenerationMatches(d, expectedGeneration)) throw 0
      if (alive()) onIo(traceDetailState(d))
    }).catch(() => { if (alive()) onIo(unavailableTraceDetail()) })
    return request
  }, null, [open, io, runId, expectedGeneration, s.span_id, kind], {
    enabled: open && io === null && !!runId && !!s.span_id
      && (kind === 'generation' || kind === 'tool'),
  })
  const retryIo = () => onIo(null)
  const toggle = () => { onActivate(); onToggle() }

  if (kind === 'generation') {
    // Row header from the LIGHT span (op·model·tokens); the prompt/output come from the fetched `io`.
    const a = { ...(s.attributes || {}), ...(io?.attributes || {}) }
    const c = genToCall({ ...s, attributes: a }), t = c.tokens
    return <>
      <button type="button" tabIndex={-1} aria-expanded={open} aria-controls={detailId}
        className={'span-row gen disclosure-button' + (err ? ' err' : '')}
        style={rowIndent} onClick={toggle} title="expand for prompt & output">
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
      {open && <div className="span-detail" id={detailId} style={detailIndent}>
        {io === null ? <div className="muted trace-small" role="status">loading…</div> : io.status === 'unavailable'
          ? <TraceUnavailable label="Trace detail unavailable." onRetry={retryIo} />
          : <>{io.partial && <div className="notice compact" role="status">Trace detail truncated.</div>}
            <GenBody c={c} thinkOpen={thinkOpen} onThink={onThink} /></>}</div>}
    </>
  }
  if (kind === 'tool') {
    const a = { ...(s.attributes || {}), ...(io?.attributes || {}) }
    const inp = asText(a.input), outp = asText(a.output), name = (s.attributes || {}).tool || a.tool || 'tool'
    return <>
      <button type="button" tabIndex={-1} aria-expanded={open} aria-controls={detailId}
        className={'span-row tool disclosure-button' + (err ? ' err' : '')}
        style={rowIndent} onClick={toggle} title="expand for input & output">
        <span className="span-tw">{open ? '▾' : '▸'}</span>
        <span className="span-name tool"><OpIcon name="gear" className="t-ic" /> <b className="tool-name">{name}</b></span>
        {bar}
        <span className="t">{fmt(s.duration_s, 3)}s</span>
        {err && <span className="badge reason">ERROR</span>}
      </button>
      {open && <div className="span-detail" id={detailId} style={detailIndent}>
        {io === null ? <div className="muted trace-small" role="status">loading…</div> : io.status === 'unavailable'
          ? <TraceUnavailable label="Trace detail unavailable." onRetry={retryIo} /> : <>
          {io.partial && <div className="notice compact" role="status">Trace detail truncated.</div>}
          {inp && <div className="msg"><div className="msg-role role-user">input</div><pre className="code">{inp}</pre></div>}
          {outp && <div className="msg"><div className="msg-role role-completion">output</div><pre className="code">{outp}</pre></div>}
          {!inp && !outp && <div className="muted trace-small">(no input/output recorded)</div>}</>}
      </div>}
    </>
  }
  // OPERATION span (a phase of work): bounded attributes and events.
  const [icon, role, desc] = stageMeta(s.name)
  const detail = spanHasDetail(row)
  const Header = detail ? 'button' : 'div'
  const stage = row.parent < 0
  const roll = stage ? spanRollup(s) : null
  return <div className={stage
    ? 'stage span-stage-row' + (err ? ' err' : '') : undefined}>
    <Header type={detail ? 'button' : undefined} tabIndex={detail ? -1 : undefined}
         aria-expanded={detail ? open : undefined} aria-controls={detail ? detailId : undefined}
         className={(stage ? 'stage-h ' : '') + 'span-row'
           + (detail ? ' disclosure-button' : '') + (err && !stage ? ' err' : '')}
         style={rowIndent} onClick={detail ? toggle : onActivate}
         title={detail ? `${desc} — expand detail` : desc}>
      <span className={stage ? 'stage-caret' : 'span-tw'}>
        {detail ? (open ? '▾' : '▸') : '·'}</span>
      {stage ? <><span className="stage-ic"><OpIcon name={icon} /></span><b>{role}</b>
        {roll.calls > 0 && <span className="stage-roll" title={`${roll.tok} billed tokens`}>
          {roll.calls} call{roll.calls > 1 ? 's' : ''}
          {roll.ctx ? ` · ${ktok(roll.ctx)} ctx` : ''}{roll.out ? ` · ${ktok(roll.out)} out` : ''}</span>}
        <span className="spacer" /></>
        : <span className="span-name" title={desc}><OpIcon name={icon} className="t-ic" />
          {role !== s.name ? role : s.name}</span>}
      {!stage && bar}
      <span className="t">{fmt(s.duration_s, 3)}s</span>
      {err && <span className="badge reason">ERROR</span>}
    </Header>
    {open && detail && <div className={'span-detail' + (stage ? ' stage-root-detail' : '')}
      id={detailId} style={stage ? undefined : detailIndent}>
      <SpanFacts span={s} />
    </div>}
  </div>
}

// One dependency-free, variable-height window over the ENTIRE forest. Unlike the old per-sibling
// cap, it cannot multiply at every depth and has no "show all" escape hatch. All rows remain in the
// logical/ARIA tree and search index; only the viewport plus overscan is mounted in the DOM.
function VirtualSpanTree({ roots, t0, total, runId, expectedGeneration, identity }) {
  const rows = useMemo(() => flattenSpanTree(roots), [roots])
  const byKey = useMemo(() => new Map(rows.map((row, index) => [row.key, index])), [rows])
  const [activeKey, setActiveKey] = useState(() => rows[0]?.key || null)
  const [spanState, setSpanState] = useState(() => new Map())
  const [query, setQuery] = useState('')
  const matches = useMemo(() => spanTreeMatches(rows, query), [rows, query])
  const treeId = useId()
  const activeIndex = byKey.get(activeKey) ?? (rows.length ? 0 : -1)
  const activeId = activeIndex >= 0 ? `${treeId}-item-${activeIndex}` : null
  const matchAt = matches.indexOf(activeIndex)
  useEffect(() => {
    if (!rows.length) setActiveKey(null)
    else if (!byKey.has(activeKey)) setActiveKey(rows[0].key)
  }, [activeKey, byKey, rows])
  // A live fixed window can evict old span ids while this component stays mounted. Keep useful
  // disclosure/detail state for retained ids, but release heavy fetched I/O as soon as its row leaves
  // the logical projection; reusing an id after eviction must fetch and disclose afresh.
  useEffect(() => setSpanState(current => pruneSpanState(current, byKey)), [byKey])
  const updateSpan = (key, patch) => setSpanState(current => {
    const previous = current.get(key) || {}
    const next = new Map(current)
    next.set(key, { ...previous, ...(typeof patch === 'function' ? patch(previous) : patch) })
    return next
  })
  const toggle = key => updateSpan(key, state => ({ open: !state.open }))
  const find = step => {
    if (!matches.length) return
    const current = matchAt < 0 ? (step < 0 ? 0 : -1) : matchAt
    const next = (current + step + matches.length) % matches.length
    setActiveKey(rows[matches[next]].key)
  }
  const onTreeKey = event => {
    // `contains`, not identity. This is a roving virtual collection: focus is meant to stay on the
    // viewport and `aria-activedescendant` points into it, but every row control is `tabIndex={-1}`
    // and a `tabIndex=-1` button still TAKES focus when clicked. So after the operator clicked any
    // row, the keydown originated on that button, `target !== currentTarget` held, and Arrow/Home/
    // End/Enter all silently did nothing — the highlight frozen, and Tab leaving the tree because no
    // row is in the tab order. The click handler below restores focus to the viewport; this accepts
    // the keys either way. There is no inner text input to protect: the search box is a SIBLING of
    // the viewport, not a descendant.
    if (!event.currentTarget.contains(event.target) || activeIndex < 0) return
    const row = rows[activeIndex]
    if ((event.key === 'Enter' || event.key === ' ') && spanHasDetail(row)) {
      event.preventDefault(); toggle(row.key); return
    }
    if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    let next = activeIndex
    if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = rows.length - 1
    else if (event.key === 'ArrowDown') next = Math.min(rows.length - 1, next + 1)
    else if (event.key === 'ArrowUp') next = Math.max(0, next - 1)
    else if (event.key === 'ArrowRight' && rows[next + 1]?.parent === next) next += 1
    else if (event.key === 'ArrowLeft' && row.parent >= 0) next = row.parent
    setActiveKey(rows[next].key)
  }
  return <div className="span-tree-shell">
    <div className="span-tree-tools">
      <label className="sr-only" htmlFor={`${treeId}-search`}>Find an observation in this span tree</label>
      <input id={`${treeId}-search`} className="span-tree-search" type="search" value={query}
        placeholder="Find span…" onChange={event => {
          const value = event.target.value
          const next = spanTreeMatches(rows, value)
          setQuery(value)
          if (next.length) setActiveKey(rows[next[0]].key)
        }} onKeyDown={event => {
          if (event.key === 'Enter') { event.preventDefault(); find(event.shiftKey ? -1 : 1) }
        }} />
      <button type="button" className="seg" disabled={!matches.length} aria-label="Previous span match"
        onClick={() => find(-1)}>↑</button>
      <button type="button" className="seg" disabled={!matches.length} aria-label="Next span match"
        onClick={() => find(1)}>↓</button>
      <span className="muted span-tree-count" role="status" aria-live="polite">
        {query ? (matches.length ? (matchAt < 0 ? `${matches.length} matches`
          : `${matchAt + 1} of ${matches.length}`) : 'No matches')
          : `${rows.length} span${rows.length === 1 ? '' : 's'}`}</span>
    </div>
    <VirtualTimeline rows={rows} getKey={spanTreeKey} renderRow={row => {
      const state = spanState.get(row.key) || {}
      return <SpanRow row={row} open={!!state.open} onToggle={() => toggle(row.key)}
        onActivate={() => setActiveKey(row.key)} detailId={`${treeId}-detail-${byKey.get(row.key)}`}
        t0={t0} total={total} runId={runId} expectedGeneration={expectedGeneration}
        parentOp={row.parent < 0 ? null : rows[row.parent].span.name}
        io={state.io ?? null} onIo={io => updateSpan(row.key, { io })}
        thinkOpen={state.think} onThink={think => updateSpan(row.key, { think })} />
    }} identity={identity} className="span-tree-virtual" estimateSize={32} overscan={10}
      activeIndex={activeIndex} viewportProps={{ role: 'tree',
        'aria-label': `Span tree with ${rows.length} observations`,
        'aria-activedescendant': activeId || undefined, onKeyDown: onTreeKey }}
      getItemProps={(row, index) => ({ id: `${treeId}-item-${index}`,
        role: 'treeitem',
        'aria-level': row.level, 'aria-posinset': row.pos, 'aria-setsize': row.size,
        'aria-selected': index === activeIndex, 'data-active': index === activeIndex ? '' : undefined,
        // Focus belongs on the VIEWPORT for the whole life of a roving `aria-activedescendant`
        // collection. Clicking a row moves it to the clicked control (or to `<body>` for a row that
        // renders as a plain div), which takes the tree out of the tab order; put it back.
        onClick: event => {
          setActiveKey(row.key)
          event.currentTarget.closest('[role="tree"]')?.focus()
        } })} />
  </div>
}

// The ONE affordance every bounded trace surface uses to reveal its earlier steps. It replaces the
// `↧` pager button the Inspector and the chat feed each carried, and the sentence that button's
// ABSENCE used to print — the one claiming the window could go no further. That sentence was not
// even true where it appeared most: it was keyed on "there is no pager", which is also the state of
// any surface with no pager wired, so a node whose entire 258-span trace the server had already read
// was told its window was maximal while eight doublings were still available (measured on
// runs/rubert-dr-0807 node 2: 256 of 308 steps at the default, all 308 at ONE doubling).
//
// What renders, per traceScrollModel state:
//   settled   → nothing at all
//   reachable → a VISIBLE button in a sentinel zone; scrolling it into view also widens the window
//   loading   → the same zone plus a `role="status"` live region announcing the read
//   bounded   → the honest terminal receipt: the count, and that this surface cannot go further
// The affordance is visible at all times (styles.css `.trace-reach`), and only its focus outline is
// focus-conditional. It was sr-only-until-focused on the reasoning that a pointer user reaches the
// earlier steps by scrolling anyway; the operator reported "the button to load the whole trace is
// gone again" — twice — so that reasoning was measured and reversed. Scrolling still works and still
// loads. The observer half remains load-bearing for a different reason: an IntersectionObserver
// fires for anyone who scrolls, including a keyboard user pressing Page Up, while a screen-reader
// user driving a virtual cursor never scrolls the container — hence a real focusable control too.
function TraceReach({ state, notice, onReach, failed = false }) {
  const { sentinelRef, onReachFocus } = useTraceScroll({ state, onReach, failed })
  if (state === TRACE_SCROLL_SETTLED) return null
  if (state === TRACE_SCROLL_BOUNDED) {
    return <div className="notice compact" role="status">{notice} {traceScrollBoundedSuffix}</div>
  }
  return <div className="trace-reach-zone" ref={sentinelRef}>
    <button type="button" className="trace-reach" onFocus={onReachFocus} onClick={onReachFocus}>
      {TRACE_SCROLL_REACH_LABEL}</button>
    {/* One live region, always mounted while the zone is, so an assistive reader announces the
        transition rather than a region appearing and disappearing under it. */}
    <div className="muted trace-small trace-reach-status" role="status">
      {state === TRACE_SCROLL_LOADING ? TRACE_SCROLL_LOADING_LABEL : ''}</div>
    {failed && <div className="notice compact trace-reach-failed" role="alert">
      Could not load earlier steps. Scroll again to retry.</div>}
  </div>
}

// Reusable langfuse-style trace for ONE node's span forest — the lifecycle stages on a shared
// timeline. Exported so the chat feed can show the same waterfall inline (Dock.jsx) as the Inspector.
export function NodeTrace({ spans, runId, projection = {}, onRetry, onLoadMore,
  spanLimit = NODE_TRACE_SPAN_WINDOW, expectedGeneration, treeKey = expectedGeneration || runId }) {
  const roots = spans || []
  // The scroll affordance is a hook, so it is resolved before any early return. `unavailable` is
  // still handled FIRST below: a failed observation never gets a sentinel (see traceScrollModel).
  const spanWindow = traceWindow(projection, { canPage: !!onLoadMore })
  const scroll = traceScrollState({ view: spanWindow, window: spanLimit })
  if (traceUnavailable(projection)) return <TraceUnavailable onRetry={onRetry} />
  // Route the partial handling through the ONE window rule (traceProjection.js) instead of the raw
  // `truncated` union. That flag conflates "spans were omitted" (a bigger limit surfaces them) with
  // "per-span text was clamped" (no limit ever will), so the old `tracePartial` branch rendered the
  // pager on traces where clicking it could not add a single row — measured, 8 of the 13 real node
  // traces in rubert-dr-0805 / rubertlite-dr-unified-v4 report truncated=true with omitted_spans=0.
  // With nowhere to go the rule still owes the operator the COUNT, never a bare adjective.
  const reach = <TraceReach state={scroll} onReach={onLoadMore}
    notice={traceWindowNotice(spanWindow)} />
  if (!roots.length) {
    if (spanWindow.kind === 'complete')
      return <div className="muted trace-small">No execution spans captured yet.</div>
    // A bounded surface's own notice already carries the count and says it cannot go further;
    // stacking the generic empty notice on top of it says "partial" twice and adds nothing.
    return <>{scroll === TRACE_SCROLL_BOUNDED ? null
      : <div className="notice compact" role="status">{TRACE_PARTIAL_EMPTY_NOTICE}</div>}{reach}</>
  }
  const { t0, total } = traceBounds(roots)
  return <div className="trace">
    {reach}
    <VirtualSpanTree key={treeKey} roots={roots} t0={t0} total={total} runId={runId}
      expectedGeneration={expectedGeneration} identity={String(treeKey)} />
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

function ConvStage({ st, defaultOpen = true, log = '', logShare = null, live = false }) {
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
      {/* Conversation Markdown is not part of the span-tree virtual window. Keep a local turn cap: a heavily-repaired / tool-looping stage
          can carry hundreds of turns, and ConvGen eagerly renders each turn's Markdown — mounting them all
          froze the browser. Show one bounded tranche, then reveal the rest of this server projection. */}
      {(allTurns ? (st.turns || []) : (st.turns || []).slice(0, CONVERSATION_TURN_CAP)).map((t, j) =>
        t.type === 'request' ? <ConvRequest key={j} t={t} />
          : t.type === 'tool' ? <ConvTool key={j} t={t} /> : <ConvGen key={j} t={t} />)}
      {!allTurns && (st.turns || []).length > CONVERSATION_TURN_CAP && <button className="span-more"
        onClick={() => setAllTurns(true)}>… show {(st.turns || []).length - CONVERSATION_TURN_CAP} more turns</button>}
      {log && logShare ? <div className="muted trace-small stage-log-share" role="note">
        {logShare.note}</div> : null}
      {log ? <StageLog text={log} live={live} /> : null}
    </div>}
  </div>
}

const matchingNodePayload = (result, nodeId, attempt, expectedGeneration) => {
  const payload = result.value
  return result.status === 'fulfilled' && String(payload?.node_id) === String(nodeId)
    && payload?.attempt === attempt && traceGenerationMatches(payload, expectedGeneration)
    ? payload : null
}

// The same gate for whichever SUBJECT is being read — the node fence above generalized so the
// proposal reading cannot land under a node's heading (or a second proposal's) either.
const matchingTracePayload = (result, subject, expectedGeneration) => {
  const payload = result.value
  return result.status === 'fulfilled' && traceSubjectMatches(subject, payload)
    && traceGenerationMatches(payload, expectedGeneration)
    ? payload : null
}

// The linear reading, for whatever SUBJECT the surface is showing: a node's build+eval, or ONE
// operation's own trace (a Researcher proposal, which carries no node_id and is therefore reachable
// no other way — see traceSurfaceModel.js). Everything below is the same reading; only the path, the
// fence and whether there are subprocess logs come from the subject.
export function Conversation({ subject, runId, expectedGeneration, working, allOpen = true,
  reloadNonce = 0, onRetry, spanLimit = NODE_TRACE_SPAN_WINDOW, onLoadMore }) {
  const subjectKey = traceSubjectKey(subject)
  const subjectAttempt = traceSubjectAttempt(subject)
  const subjectBefore = traceSubjectBefore(subject)
  // This is the evidence lifecycle, deliberately NOT the request representation: widening the span
  // window may retain last-good evidence on failure, while a reset/clear nonce may not. Gate during
  // render as well as commit so correctness never depends on a passive clearing effect winning a race.
  const lifecycleScope = [runId, expectedGeneration || '', subjectKey, reloadNonce].join('\0')
  // The last SETTLED read, carried with the window that produced it and what that window made
  // visible. Both extra fields earn their place: the window is how "a wider read is in flight" is
  // derived without a second piece of state, and the visible count is what proves a widen actually
  // BOUGHT something — the auto-loader has to be provably terminating (traceScrollModel).
  const [read, setRead] = useState(null)
  // Deliberately NOT part of `lifecycleScope`: a scheduled re-read must re-run the effect
  // WITHOUT clearing the last-good payload, which is what a scope change does.
  const [retryNonce, setRetryNonce] = useState(0)
  const [logs, setLogs] = useState({})   // {eval, stages:{train,score,…}} — the live stage/eval logs
  // A ref mirror of the settled read, so the outcome below is computed OUTSIDE a setState updater.
  // `usePoll` serializes ticks (one read unsettled at a time), so this is never behind; and an
  // updater that calls another setState is impure — React may invoke it twice.
  const readRef = useRef(null)
  const currentRead = read?.lifecycleScope === lifecycleScope ? read : null
  readRef.current = currentRead
  // A finished node reads ONCE (`working` false ⇒ no poll interval), so one bad read used to
  // be a permanent receipt until the operator clicked. The budget is bounded and lives in the
  // model; a superseded read gets none, because retrying that scope keeps answering about the
  // node that replaced it.
  const autoRetryMs = working ? null
    : traceRetryMs(currentRead?.failures, currentRead?.failure)
  useTraceRetry(autoRetryMs, currentRead?.failures || 0, setRetryNonce)
  useEffect(() => {
    setRead(null)   // release the prior lifecycle after the render-time scope gate already hid it
    setLogs({})     // …likewise the logs, else B's stage bands briefly render A's log text
  // `spanLimit` is deliberately NOT in this list any more. Clearing on a widen blanked the thread to
  // "loading…" for the whole read — 17 s at the ceiling on the measured stress node — so scrolling
  // for older steps took away the ones already on screen. The poll below still re-runs on it.
  }, [lifecycleScope])
  usePoll((alive) => {
    // A validator is reusable only for the exact selected representation. The server independently
    // mixes all five identities into its ETag; keeping the same scope key here prevents even a
    // broken intermediary's 304 from carrying a prior node/window across this client boundary.
    const scope = [runId, expectedGeneration || '', subjectKey, spanLimit].join('\0')
    const prior = readRef.current
    const validator = prior?.scope === scope && prior.etag
    const conversationPath = runApiPath(runId,
      traceRequestPath(subject, TRACE_VIEW_CONVERSATION)
      + traceReadQuery(expectedGeneration, subjectAttempt, spanLimit, subjectBefore))
    // ONE settle rule for both outcomes below, so the deadline path and the rejected-response path
    // cannot disagree about what a failure costs the operator.
    const commit = (ok, payload, etag = null, failure = TRACE_FAILURE_UNREADABLE) => {
      const previous = readRef.current
      const settled = settleTraceRead(previous?.payload, { ok, payload })
      const failedWiden = settled.reachFailed && spanLimit > previous.window
      // Consecutive failures WITHIN one lifecycle scope. A new scope is a new question, so its
      // budget starts full; carrying the count across would let an old node's bad minute silence the
      // retry on the one the operator just opened.
      const failures = ok ? 0
        : (previous?.lifecycleScope === lifecycleScope ? previous.failures || 0 : 0) + 1
      if (settled.unavailable) {
        setRead({ lifecycleScope, payload: { stages: [], projection: { unavailable: true } },
          window: spanLimit, before: subjectBefore || null, visible: 0, failures, failure })
        return
      }
      // A kept payload keeps its OWN window: recording the window we failed to reach would read as
      // "that read landed", and the next widen would then look like no widen at all.
      if (settled.reachFailed) {
        setRead({ ...previous, reachFailed: failedWiden,
          stale: failedWiden ? previous.stale : true, failures, failure })
        return
      }
      const visible = settled.payload?.projection?.visible_turns
      // The ANCHOR travels on the record because `traceWidenStalled` is scoped to it: a widen that
      // bought nothing at one `?before=` says nothing about the next episode the operator seeks to,
      // and carrying it there is what silently retired the "load earlier steps" control.
      const next = { lifecycleScope, payload: settled.payload, window: spanLimit,
        before: subjectBefore || null,
        visible: Number.isSafeInteger(visible) && visible >= 0 ? visible : 0,
        scope, etag, failures: 0 }
      setRead({ ...next, stalled: traceWidenStalled(previous, next) })
    }
    const readConversation = async signal => {
      const read = etag => conditionalGet(conversationPath, etag, { signal, cache: 'no-store' })
      let observation = await read(validator)
      // Defensive protocol recovery: a 304 with no exact same-scope payload is not an empty
      // conversation and not permission to borrow another scope. Retry this tick unconditionally.
      if (observation.unchanged
          && (!prior?.payload || observation.etag !== validator)) observation = await read(null)
      return observation
    }
    const timed = deadlineRequest(signal => Promise.allSettled([
        readConversation(signal),
        // Only a NODE has subprocess logs. A proposal ran no sandbox and no stage, so asking for
        // its logs would be a second request per poll whose 404 says nothing.
        ...(traceSubjectHasLogs(subject)
          ? [get(runNodeApiPath(runId, subject.nodeId,
            `/logs${traceReadQuery(expectedGeneration, subjectAttempt)}`),
          { signal, cache: 'no-store' })]
          : []),
      ]), traceReadDeadlineMs(spanLimit))
    timed.promise.then(([conversation, logs]) => {
      if (!alive()) return
      const observation = conversation.status === 'fulfilled' ? conversation.value : null
      const candidate = observation?.unchanged ? prior?.payload : observation?.data
      const payload = matchingTracePayload(
        { status: observation ? 'fulfilled' : 'rejected', value: candidate },
        subject, expectedGeneration)
      // A 200's cursor and ETag must agree before either is sent back. A 304 already matched the
      // exact validator this scope supplied. A proxy/header anomaly costs only the optimization.
      const etag = payload && (observation.unchanged || payload.cursor === observation.etag)
        ? observation.etag : null
      // Transport success is not enough: a delayed response for the previous lifecycle must never
      // settle this attempt's window. The server echoes both identity fields and independently rejects
      // an attempt change before/after its read; this client-side gate also contains proxy/schema drift.
      // A FULFILLED response the subject fence refused is a SUPERSEDED read, not an unreadable one:
      // the server answered, about a node/attempt/generation that is no longer on screen. Retrying
      // this scope will keep answering about the new one; only reloading the run helps. The two were
      // indistinguishable, and both printed "Trace unavailable".
      commit(!!payload, payload, etag,
        observation && !payload ? TRACE_FAILURE_SUPERSEDED : TRACE_FAILURE_UNREADABLE)
      const logPayload = logs
        ? matchingNodePayload(logs, subject.nodeId, subjectAttempt, expectedGeneration)
        : null
      // The stage bands and their log text are one evidence snapshot. Never combine a retained
      // attempt-A conversation with logs returned while A was rejected/resetting.
      if (payload && logPayload) setLogs(logPayload)
    // `allSettled` never rejects, so this branch is the DEADLINE — which is exactly the failure a
    // widened read hits. Routing it through the same `commit` is what stops a timed-out widen from
    // silently keeping the old payload with nothing said about it.
    }).catch(() => { if (alive()) commit(false, null) })
    return timed
  }, working ? 4000 : null,   // interval only while the agent works this node (live-refresh); null = load once
  [runId, expectedGeneration, subjectKey, working, reloadNonce, retryNonce, spanLimit])   // reloadNonce also re-runs a finished node's one-shot load
  if (currentRead === null) return <div className="muted trace-small" role="status">loading…</div>
  const conv = currentRead.payload || { stages: [] }
  const stages = conv.stages || []
  const unavailable = traceUnavailable(conv.projection)
  if (unavailable) return <TraceUnavailable
    label={traceFailureLabel(currentRead?.failure, { retrying: autoRetryMs != null })}
    onRetry={onRetry} />
  // The operator's actual complaint lives here: this view is the DEFAULT one, it is where "N steps
  // hidden" is read, and until now it printed a count and then refused to pass it.
  // `conversationWindow` (not `traceWindow`) because what is hidden here is STAGES and TURNS, and the
  // span counters in the same envelope describe a different quantity — see its comment.
  const convWindow = conversationWindow(conv.projection, { canPage: !!onLoadMore })
  const staleNotice = currentRead.stale
    ? <div className="notice compact conversation-stale" role="alert">
      Conversation refresh failed; showing confirmed trace while run state reloads.
    </div>
    : null
  const scroll = traceScrollState({
    view: convWindow,
    window: spanLimit,
    // A widen is in flight exactly while the requested window is ahead of the settled one AND the
    // last read did not fail. Both halves are needed: a failed widen leaves the settled window
    // BEHIND the requested one forever (`settleTraceRead` deliberately does not record a window it
    // could not reach), so the window comparison alone latches a spinner that never clears — and a
    // surface stuck in `loading` never re-arms, so the failure would also be unretryable.
    pending: !currentRead.reachFailed && spanLimit > currentRead.window,
    stalled: currentRead.stalled === true,
  })
  const reach = <TraceReach state={scroll} onReach={onLoadMore} failed={currentRead.reachFailed}
    notice={conversationWindowNotice(convWindow)} />
  if (!stages.length) return convWindow.kind === 'complete'
    ? <>{staleNotice}<div className="muted">{traceSubjectHasLogs(subject)
      ? 'No conversation captured for this node yet.'
      : 'No conversation captured for this operation.'}</div></>
    : <div className="conv">{staleNotice}{reach}</div>
  // The live log for a stage band: a multi-stage eval logs per stage (stages[label]); a single-command
  // eval logs to eval.log ("evaluate"/"command"); the dep-install step to setup.log. Anything else
  // (propose/implement/…) has no subprocess log.
  const logFor = (label) => (logs.stages && logs.stages[label])
    || ({ setup: logs.setup, evaluate: logs.eval, command: logs.eval }[label]) || ''
  // …and WHOSE log it is. `logFor` is keyed by stage LABEL, and a repaired node has one band per
  // attempt under the same label, so several bands legitimately resolve to one file's tail. Say so
  // on the bands where it happens (traceProjection.js::stageLogAttribution) rather than letting each
  // band present the shared text as its own attempt's.
  const logShare = stageLogAttribution(stages)
  // `allOpen` is owned by the sticky Trace header (so collapse-all lives in the pinned bar). It's folded
  // into each band's key so a collapse/expand-all click remounts them at the new default; a live poll
  // (allOpen unchanged) keeps the key stable, so per-band toggles survive the 4s refresh.
  // The sentinel goes ABOVE the bands, because that is where the gap is: every cap in this projection
  // keeps the newest TAIL, so the missing stages are the OLDEST and the thread's first visible band is
  // the truncation boundary. Scrolling UP is therefore the gesture that means "show me earlier", and
  // the trigger has to sit where that gesture ends. (The Dock's live tail puts "load earlier" at the
  // top for the same reason.)
  return <div className="conv">{staleNotice}{reach}
    {stages.map((st, i) => <ConvStage key={`${st.trace_id || ''}:${st.label || ''}:${st.start || i}:${allOpen}`}
                                      st={st} defaultOpen={allOpen} log={logFor(st.label)}
                                      logShare={logShare[i]} live={working} />)}
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

// The span tree's paged read, for whatever SUBJECT the surface is showing. For a node it returns
// null until the operator has raised the window (the detail payload's default window is what renders
// until then), then that node's `/trace` projection at the requested limit; for one operation's
// trace it is the only source there is, so it reads immediately. Polls on the same cadence as the
// conversation while the subject is being worked, so paging a LIVE node does not freeze its trace at
// the moment it was paged; `nonce` re-reads after a trace clear. A failed read stays null and falls
// back to the detail payload rather than blanking a trace the operator can still see — asking for
// more must never cost them what they had.
function usePagedTrace({
  subject, runId, expectedGeneration, limit, nonce, working, enabled,
}) {
  const [settled, setSettled] = useState(null)
  const subjectKey = traceSubjectKey(subject)
  const scope = `${expectedGeneration || runId}:${subjectKey}:${nonce}`
  // Identity changes (and an explicit post-clear nonce) discard evidence. A larger window does not:
  // the narrower successful page remains the last confirmed truth until its replacement settles.
  useEffect(() => { setSettled(null) }, [enabled, scope])
  usePoll((alive) => {
    const request = traceDeadlineGet(
      runApiPath(runId, traceRequestPath(subject, TRACE_VIEW_SPANS)),
      expectedGeneration, traceSubjectAttempt(subject), limit, traceReadDeadlineMs(limit),
      traceSubjectBefore(subject))
    request.promise.then(d => {
      // Same fence the Dock applies: a response for another node/attempt — or another trace — is a
      // stale in-flight read from the previous scope, never this subject's trace.
      if (!traceSubjectMatches(subject, d)
          || !traceGenerationMatches(d, expectedGeneration)) throw 0
      if (alive()) setSettled({ scope, payload: d })
    })
      .catch(() => {
        if (alive()) setSettled(previous => previous?.scope === scope
          ? { ...previous, stale: true }
          : { scope, payload: null, stale: true })
      })
    return request
  },
    working ? 4000 : null,
    [runId, subjectKey, expectedGeneration, limit, nonce, working, enabled], { enabled })
  return enabled && settled?.scope === scope ? settled : null
}

// THE TRACE SURFACE — the one way this UI reads a trace, wherever a trace is read.
//
// It exists because there were two. The node Inspector's Trace tab grew the whole apparatus (the
// conversation/span-tree switcher over ONE shared window, in-tree search, scroll-to-reach for
// earlier steps, the honest partial receipts), and the card's Trace tab then got a second, poorer
// one: rows that opened a bare `/trace/by_trace/{tid}` span tree. That second surface was not merely
// thinner — for the Developer it showed the WRONG trace (see traceSurfaceModel.js for the measured
// counts), so the card's Trace tab reported the Developer's work as two spans and the operator's
// verdict was that the Developer trace had disappeared.
//
// What the OWNER of a surface still provides, because it is not part of reading a trace:
//   `status`   the live "what is this node doing right now" line
//   `controls` chrome that belongs on the control bar — the attempt picker, the destructive clear,
//              the scroll nav. Rendered INSIDE the switcher row, which is what makes the bar one row.
//   `below`    chrome under the bar (the research disclosure)
//   `footer`   evidence that follows the trace in every branch (the agent's validation report)
// Everything else — which views exist, which is showing, the window, the fences, what a bounded or
// failed read owes the reader — is here, once.
//
// Exported so the card can render it (CardBoard lazily imports this module — a static edge would put
// the whole Inspector in the board's chunk) AND so a test can mount it: the property that matters is
// that the switcher issues a real request for the other reading and the response reaches the screen,
// which no amount of reading this file's text can see. The Inspector shipped a dead partial notice
// for months underneath pins that were all green.
export function TraceSurface({
  subject, runId, expectedGeneration = null, working = false,
  // The node-detail payload already in hand: `{ payload, attempt }`. It is what renders until the
  // operator asks for a bigger window, so this tab fills in live during a build off the detail poll
  // that already runs. A trace subject has no such payload and reads immediately.
  detail = null,
  nonce = 0,                     // bumped after "clear trace" to reload the bands
  chrome = 'tab',                // 'tab' = the sticky Inspector bar; 'inline' = embedded in a row
  status = null, controls = null, below = null, footer = null,
  detailUnavailable = false, onRetry = null, retryPending = false,
  bodyRef = null,
}) {
  const [view, setView] = useState(TRACE_VIEW_CONVERSATION)  // linear reading by default
  const [allOpen, setAllOpen] = useState(false)       // bands COLLAPSED by default (expand one to read it)
  // "Try again" on a failed READ, which is not the same button as "reload this node" — the owner's
  // `onRetry` reloads the detail payload the span tree falls back to, and only the owner can do
  // that. A surface with no detail payload has nothing to ask its owner for, so it retries itself.
  const [retryNonce, setRetryNonce] = useState(0)
  const retryRead = () => setRetryNonce(value => value + 1)
  // ONE window for the subject, shared by both readings of it (hooks.js::useNodeSpanWindow, the same
  // hook the chat feed pages with). Not one per view: "show me more of experiment #7" is about the
  // experiment, and two independent windows would let the span tree and the conversation disagree
  // about how much of the same thing they are each showing.
  const { limit: spanLimit, canPage, loadMore } = useNodeSpanWindow()
  const subjectAttempt = traceSubjectAttempt(subject)
  const subjectAnchored = traceSubjectBefore(subject) != null
  const detailAttempt = detail ? detail.attempt : null
  // `view` is part of the gate, not just the window: the conversation branch returns below without
  // ever reading `paged`, and both views raise the SAME shared window BY DESIGN — so an operator who
  // reached for earlier steps in the conversation had a second 4 s poll running against this node for
  // as long as it worked, fetching a span tree whose response was thrown away (up to ~1.6 MB per tick
  // at the x8 ceiling). With no detail payload there is nothing to fall back to, so the read is not
  // optional at all.
  const readNonce = `${nonce}:${retryNonce}`
  const pagedRead = usePagedTrace({
    subject, runId, expectedGeneration, limit: spanLimit, nonce: readNonce, working,
    // The validity gate belongs HERE as well as at the early return below: hooks run during render,
    // so an unguarded read would already be in flight by the time the refusal renders.
    enabled: traceSubjectValid(subject) && !!runId
      && view !== TRACE_VIEW_CONVERSATION && (!detail || attemptReadRequired({
        selected: subjectAttempt, current: detailAttempt, anchored: subjectAnchored,
        canPageFurther: spanLimit > NODE_TRACE_SPAN_WINDOW,
      })),
  })
  const paged = pagedRead?.payload
  const trace = detail
    ? traceForAttempt({
      selected: subjectAttempt, current: detailAttempt, paged, detail: detail.payload,
      anchored: subjectAnchored,
    })
    : paged
  const spans = traceSubjectSpans(subject, trace)
  // For a node the owner owns this (it is bound to the DETAIL payload and feeds the destructive
  // clear's fence, which a failed pager may not move). For a trace subject the read IS the evidence.
  // A STALE SETTLE WITH NO PAYLOAD IS UNAVAILABLE. For a detail-less subject (any op-trace, or a
  // node at a historical attempt) the paged read is the only read there is, so when it fails
  // `usePagedTrace` settles `{payload: null, stale: true}` — and `traceUnavailable(undefined)` is
  // false while `traceWindow(undefined)` reads as COMPLETE. The surface then rendered the positive
  // empty claim ("No observations were recorded…") about a read that merely timed out, under a
  // header notice saying "showing confirmed spans" with zero spans ever confirmed. A failed
  // observation is never evidence that the subject recorded nothing.
  const unavailable = detailUnavailable
    || (!detail && (traceUnavailable(trace?.projection)
                    || (pagedRead?.stale && !pagedRead?.payload)))
  const inline = chrome === 'inline'
  const head = <div className={'trace-head' + (inline ? ' trace-head-inline' : '')}>
    {status}
    {view !== TRACE_VIEW_CONVERSATION && pagedRead?.stale && <TraceUnavailable
      label="Span-tree refresh failed; showing confirmed spans." />}
    <div className="conv-toggle">
      {TRACE_SURFACE_VIEWS.map(v => <button key={v} type="button" aria-pressed={view === v}
        className={'seg' + (view === v ? ' on' : '')}
        onClick={() => setView(v)}
        title={TRACE_SURFACE_VIEW_LABELS[v].title}>{TRACE_SURFACE_VIEW_LABELS[v].label}</button>)}
      {view === TRACE_VIEW_CONVERSATION && <button type="button" className="seg trace-collapse"
        aria-pressed={allOpen} title="collapse or expand every stage"
        onClick={() => setAllOpen(o => !o)}>{allOpen ? '⊟ collapse all' : '⊞ expand all'}</button>}
      {controls}
    </div>
    {below}
  </div>
  const shell = body => <div className={'trace' + (inline ? ' trace-inline' : '')} ref={bodyRef}>
    {head}{body}{footer}</div>
  // A subject with no id has no path. Both reads below compose `runApiPath(runId, <suffix>)`, and an
  // empty suffix is `/api/runs/{id}` — the RUN, which would 200 with a payload that fences out and
  // read as "unavailable". Refuse it here, where the reason can be stated.
  if (!traceSubjectValid(subject) || !runId)
    return shell(<div className="muted" role="status">No trace is linked to this item.</div>)
  if (view === TRACE_VIEW_CONVERSATION) {
    return shell(<Conversation subject={subject} runId={runId}
      expectedGeneration={expectedGeneration} working={working} allOpen={allOpen}
      reloadNonce={readNonce} onRetry={retryRead}
      spanLimit={spanLimit} onLoadMore={loadMore} />)
  }
  // The span tree's window rule, over whichever payload is rendering (paged read or detail default).
  // Same `canPage` as the conversation, because both raise the SAME window.
  const spanWindow = traceWindow(trace?.projection, { canPage })
  // No `pending` here, and that is deliberate: `usePagedTrace` keeps the previous payload rendered
  // while the wider read is in flight and never blanks it, so there is nothing to announce beyond
  // the rows arriving. `stalled` likewise has no state to hang on — this surface re-reads the whole
  // window each time, and its termination is the ceiling the shared hook already stops at.
  const spanPager = <TraceReach
    state={traceScrollState({ view: spanWindow, window: spanLimit })}
    onReach={loadMore} notice={traceWindowNotice(spanWindow)} />
  // Unavailable takes precedence over every empty/partial shape: a failed observation is never
  // evidence that the subject recorded nothing.
  // (`unavailable` above already folds in the detail-less subject's failed sole read, which used
  // to fall through to the positive empty claim.)
  if (unavailable)
    return shell(<TraceUnavailable onRetry={onRetry || retryRead} pending={retryPending} />)
  if (!spans.length) {
    if (spanWindow.kind !== 'complete')
      return shell(<><div className="notice compact" role="status">
        {TRACE_PARTIAL_EMPTY_NOTICE}</div>{spanPager}</>)
    return shell(<div className="muted">{traceSubjectEmptyNotice(subject)}</div>)
  }
  const { t0, total } = traceBounds(spans)
  // Rollup from the RENDERED payload, not always the detail one: after paging, the totals below
  // describe the spans on screen, so reading them off the narrower window would caption a widened
  // tree with the old window's generation/token counts.
  const roll = trace?.rollup || {}
  const rtok = roll.tokens || {}
  const identity = `${expectedGeneration || runId}:${traceSubjectKey(subject)}`
  return shell(<>
    {spanPager}
    <div className="muted trace-rollup-intro">
      {traceSubjectLead(subject)} · offset = start, bar = duration. Expand an observation for
      bounded, redacted I/O.
      {(roll.generations || roll.tools) ? <span className="trace-totals"
          title={rtok.total ? `context window peaked at ${rtok.context || 0} tokens; the model generated ${rtok.completion || 0}. Billed ${rtok.total} total — each turn RE-SENDS the growing context, so billed ≫ context.` : undefined}>
        {' · '}{roll.generations || 0} generation{roll.generations === 1 ? '' : 's'}
        {roll.tools ? ` · ${roll.tools} tool call${roll.tools === 1 ? '' : 's'}` : ''}
        {rtok.context ? ` · ${ktok(rtok.context)} ctx` : ''}
        {rtok.completion ? ` · ${ktok(rtok.completion)} out` : ''}
        {roll.cost ? ` · $${roll.cost}` : ''}
      </span> : null}
    </div>
    <VirtualSpanTree key={identity} roots={spans} t0={t0} total={total} runId={runId}
      expectedGeneration={expectedGeneration} identity={identity} />
  </>)
}

// THE RESEARCH BEHIND AN EXPERIMENT — the Researcher's proposal(s) for one work item, each on the
// SAME trace surface as everything else. Rendered by the node's Trace tab (as a disclosure: this
// node's reasoning lives one level up and is shared with every sibling experiment on the card) and
// by the card's own Trace tab, so there is one implementation of "read the research" rather than a
// good one and a poor one.
export function ResearchTraces({ rows, runId, expectedGeneration = null }) {
  // One proposal is the common case and the operator asked to READ it, not to find another button.
  // Several means a re-proposal happened and the reader has to choose, so they stay closed until
  // picked — each open row is a real read.
  //
  // `undefined` is "the operator has not chosen yet", which is NOT the same as `null` ("they closed
  // it"). The default has to be derived on every render rather than seeded once: this component
  // mounts while the card read is still in flight, so a mount-time seed would always see zero rows
  // and the single proposal would render collapsed — which is exactly what it did.
  const [chosen, setChosen] = useState(undefined)
  const open = chosen === undefined
    ? (rows.length === 1 ? rows[0]?.trace_id || null : null)
    : chosen
  const setOpen = update => setChosen(current => update(current === undefined
    ? (rows.length === 1 ? rows[0]?.trace_id || null : null)
    : current))
  if (!rows.length) return null
  return <div className="research-traces">
    {rows.map(row => {
      const shown = open === row.trace_id && !!row.trace_id
      return <section key={row.span_id} className={'research-trace' + (shown ? ' open' : '')}>
        <h4 className="research-trace-h">
          <button type="button" className="btn xs ghost research-trace-toggle"
            aria-expanded={shown} disabled={!row.trace_id}
            onClick={() => setOpen(current => (current === row.trace_id ? null : row.trace_id))}>
            <span className="research-trace-caret" aria-hidden="true">{shown ? '▾' : '▸'}</span>
            <span className="research-who">Researcher</span>
            <span className="muted">· {researchLinkLabel(row.link)}</span>
          </button>
          {(row.generations || row.tools) ? <span className="muted research-trace-roll">
            {fmtInt(row.generations)} gen · {fmtInt(row.tools)} tools
            · {fmtInt(row.tokens?.total)} tok</span> : null}
        </h4>
        {/* The trace gets the whole width, below its heading. It used to be a flex SIBLING of that
            label, which squeezed a span tree — search bar, timeline bars and all — into whatever
            was left of the row. */}
        {shown && <TraceSurface subject={opTraceSubject(row.trace_id)} runId={runId}
          expectedGeneration={expectedGeneration} chrome="inline" />}
      </section>
    })}
  </div>
}

// WHERE TO POINT THE TRACE WINDOW — the node's episode map, as a control.
//
// The window a trace surface reads is bounded and reads the TAIL of one lifecycle. `TraceReach`
// makes it BIGGER; this makes it MOVE, which is the half that was missing and the half the operator
// asked for. On the measured stress node (rubert-dr-0804 node 1: 14,507 spans, 2,345 inline
// repairs, 3 h 50 m) the default window shows the last 7.6 minutes and the ceiling the last 59.3, so
// the early repairs — the ones where the bug first showed — were unreachable however far the reach
// affordance climbed. Every decision below (which kinds exist, what a position is, what may be
// offered) lives in `traceEpisodeModel.js`; this keeps only the fetch and the setState.
//
// VISIBLE, always, whenever this node HAS a bounded trace — the same rule `TraceReach` learned the
// hard way (a control that appears only on focus does not exist to a pointer user, reported twice).
// The newest map page loads when the control is opened and older pages only on demand. Derivation
// costs the server no spans.jsonl bytes at all, but it is 7,048 rows on the measured node and nobody
// should pay for it while reading a two-band one.
function TraceEpisodes({ runId, nodeId, attempt, expectedGeneration, anchor, onSeek, nonce = 0 }) {
  const [open, setOpen] = useState(false)
  // `undefined` = not read yet, `null` = failed. Keep the wire payload so an older cursor page can
  // be merged with its projection receipt before the pure model folds it into picker kinds.
  const [payload, setPayload] = useState(undefined)
  const [olderStatus, setOlderStatus] = useState('idle')
  const [kind, setKind] = useState(null)        // the label the operator is stepping through
  const [draft, setDraft] = useState(1)         // the 1-based ordinal in the number field
  const pageRequest = useRef(null)
  const pickerId = useId()
  const map = useMemo(
    () => payload === undefined ? null : buildEpisodeMap(payload), [payload])
  // A new node, lifecycle or trace clear invalidates every anchor in hand.
  useEffect(() => {
    setPayload(undefined); setOlderStatus('idle'); setKind(null); setOpen(false)
    return () => {
      pageRequest.current?.controller.abort()
      pageRequest.current = null
    }
  }, [nodeId, attempt, expectedGeneration, nonce])
  useEffect(() => {
    if (!open || payload !== undefined || !runId) return undefined
    let alive = true
    // `deadlineGet` returns a HANDLE, not a promise. The map is index-only work, but it shares the
    // absent-fence probes every trace route pays on a geesefs mount, so it gets the same deadline
    // rule as an unwidened read rather than the flat one that was aborting six-span reads.
    const request = deadlineGet(
      runNodeApiPath(runId, nodeId, `/episodes${traceReadQuery(expectedGeneration, attempt)}`),
      traceReadDeadlineMs(0))
    request.promise
      .then(d => {
        if (String(d?.node_id) !== String(nodeId) || d?.attempt !== attempt
            || !traceGenerationMatches(d, expectedGeneration)) throw new Error('stale episode map')
        if (alive) setPayload(d)
      })
      // A failed read is not an absent history: the model's `unavailable` says so and the control
      // prints it, rather than rendering an empty picker that reads as "no earlier steps".
      .catch(() => { if (alive) setPayload(null) })
    return () => { alive = false; request.controller.abort() }
  }, [open, payload, runId, nodeId, attempt, expectedGeneration])
  const loadEarlier = () => {
    const cursor = map?.nextBefore
    const snapshot = payload?.page?.snapshot
    if (!cursor || !snapshot || olderStatus === 'loading' || !payload || !runId) return
    const base = payload
    const token = Symbol('episode-page')
    const request = deadlineGet(
      runNodeApiPath(runId, nodeId,
        `/episodes${traceReadQuery(expectedGeneration, attempt, null, cursor, snapshot)}`),
      traceReadDeadlineMs(0))
    pageRequest.current?.controller.abort()
    pageRequest.current = { token, controller: request.controller }
    setOlderStatus('loading')
    request.promise.then(d => {
      if (pageRequest.current?.token !== token) return
      if (String(d?.node_id) !== String(nodeId) || d?.attempt !== attempt
          || !traceGenerationMatches(d, expectedGeneration)) throw new Error('stale episode page')
      const merged = mergeEpisodePagePayload(base, d)
      if (!merged) throw new Error('episode page cursor mismatch')
      setPayload(current => current === base ? merged : current)
      setOlderStatus('idle')
    }).catch(() => {
      if (pageRequest.current?.token === token) setOlderStatus('error')
    }).finally(() => {
      if (pageRequest.current?.token === token) pageRequest.current = null
    })
  }
  const reloadEpisodes = () => {
    pageRequest.current?.controller.abort()
    pageRequest.current = null
    setPayload(undefined); setOlderStatus('idle'); setKind(null)
  }
  const kinds = map?.status === 'ready' ? episodeKindOptions(map) : []
  // The anchor RENDERING is the one the surface is fenced on (`traceSubjectMatches` refuses a
  // payload for any other), so the requested anchor and the shown one cannot disagree on screen.
  const here = map?.status === 'ready' ? episodePosition(map, anchor) : null
  const activeKind = kind || here?.label || kinds[0]?.label || null
  const active = kinds.find(row => row.label === activeKind) || null
  // The field is the operator's, so it holds what they typed — but it FOLLOWS the window whenever
  // the window moves under them (a seek settling, a re-read after a repair landed). Deriving it from
  // `here` instead would make typing a number do nothing while anchored; keeping it purely local
  // would let it drift away from the position the caption reports.
  useEffect(() => {
    if (here && here.label === activeKind) setDraft(here.index + 1)
  }, [here?.label, here?.index, activeKind])
  const index = Math.max(0, (Number.isSafeInteger(draft) ? draft : 1) - 1)
  const seek = target => {
    const episode = episodeAt(map, activeKind, target)
    const next = episodeAnchor(episode)
    if (next) { setDraft(clampEpisodeIndex(map, activeKind, target) + 1); onSeek(next) }
  }
  return <span className="trace-episodes">
    <button type="button" className="btn xs ghost" aria-expanded={open}
      title="Jump the trace window to an earlier step of this experiment — an early repair, the first training run, the proposal."
      onClick={() => setOpen(value => !value)}>
      <span aria-hidden="true">{open ? '▾' : '▸'}</span> steps
      {here ? <span className="muted"> · {here.name} {here.ordinal}/{here.count}</span> : null}
    </button>
    {anchor && <button type="button" className="btn xs ghost" onClick={() => onSeek(null)}
      title="Return the window to this experiment's most recent steps">latest ›</button>}
    {open && <span className="trace-episodes-body">
      {map === null && <span className="muted" role="status">loading steps…</span>}
      {map?.status === 'unavailable' && <span className="muted" role="status">
        {EPISODE_MAP_UNAVAILABLE}</span>}
      {map?.status === 'empty' && <span className="muted" role="status">{EPISODE_MAP_EMPTY}</span>}
      {/* READ FINE, AND NOT EMPTY. This node has earlier steps and the map can point at none of
          them — a bounded server map, or rows with no anchor. It used to fall into `empty` above
          and tell the operator their whole trace fits in one window while its own payload said
          otherwise; the count is the only honest thing there is to say, so the notice says it. */}
      {map?.status === 'unseekable' && <span className="muted" role="status">
        {episodeMapNotice(map)}</span>}
      {map?.status === 'ready' && <>
        <label className="muted" htmlFor={`${pickerId}-kind`}>step </label>
        <select id={`${pickerId}-kind`} className="text" value={activeKind || ''}
          onChange={e => { setKind(e.target.value); setDraft(1) }}>
          {kinds.map(row => <option key={row.label} value={row.label}>
            {row.name} ({row.count})</option>)}
        </select>
        {/* Prev/next/first/last plus a typed ordinal, because a picker cannot list 2,345 repairs and
            a list that long is a phone book, not a map. Every position is clamped in the model, so a
            typo can never issue a request the server would refuse. */}
        <button type="button" className="btn xs ghost" title="first" disabled={index <= 0}
          onClick={() => seek(0)}>«</button>
        <button type="button" className="btn xs ghost" title="previous" disabled={index <= 0}
          onClick={() => seek(index - 1)}>‹</button>
        <input className="text trace-episode-n" type="number" min="1" max={active?.count || 1}
          aria-label={`which ${active?.name || 'step'} to show`}
          value={draft}
          onChange={e => setDraft(Number(e.target.value) || 1)}
          onKeyDown={e => { if (e.key === 'Enter') seek(index) }} />
        <span className="muted">of {active?.count || 0}</span>
        <button type="button" className="btn xs ghost" title="next"
          disabled={!active || index >= active.count - 1}
          onClick={() => seek(index + 1)}>›</button>
        <button type="button" className="btn xs ghost" title="last"
          disabled={!active || index >= active.count - 1}
          onClick={() => seek((active?.count || 1) - 1)}>»</button>
        <button type="button" className="btn xs" onClick={() => seek(index)}>show</button>
        {map.hasOlder && <button type="button" className="btn xs ghost trace-episodes-earlier"
          disabled={olderStatus === 'loading'}
          onClick={olderStatus === 'error' ? reloadEpisodes : loadEarlier}
          title="Load the preceding page of this experiment’s steps">
          {olderStatus === 'loading' ? 'loading earlier steps…'
            : olderStatus === 'error' ? 'reload steps' : 'load earlier steps'}</button>}
        {olderStatus === 'error' && <span className="muted" role="alert">
          The steps changed or could not be loaded. Reload and try again.</span>}
        {here && <span className="muted trace-episode-sum">
          {episodeSummary(here.episode, here.index)}</span>}
        {map.partial && <span className="muted">{episodeMapNotice(map)}</span>}
      </>}
    </span>}
  </span>
}

export function Trace({ n, runId, expectedGeneration, expectedTraceRevision, live, working, onReload,
  onOpenCard = null,
  detailStatus = 'ready',
  reloadPending = false, clearScope, clearRecoveryStore, recoverClearState = null,
  clearRecoverySignal = null, publishClearRecovery }) {
  const [nonce, setNonce] = useState(0)               // bumped after "clear trace" to reload the bands
  const bodyRef = useRef(null)
  const nodeGeneration = Number.isSafeInteger(n.attempt) && n.attempt >= 0 ? n.attempt : null
  // Which ATTEMPT of this node to show. A repaired node has several generations and only the last
  // was ever reachable — the routes have taken `?attempt=` all along, this component just always
  // sent the current number. So the trace of the attempt that actually crashed, which is the one an
  // operator opens the trace to read, could not be opened at all.
  const [viewAttempt, setViewAttempt] = useState(null)
  // WHERE inside the selected lifecycle to read — an episode's span id, or null for the newest
  // steps. Deliberately separate state from the attempt above: they answer different questions
  // (which lifecycle vs where inside one) and the operator's repaired node has 2,345 of the second
  // and one of the first. See traceEpisodeModel.js.
  const [viewBefore, setViewBefore] = useState(null)
  const attemptOptions = useMemo(
    () => nodeAttemptOptions(nodeGeneration), [nodeGeneration])
  // `null` follows the node forward: a live node that repairs mid-read must not pin the operator to
  // the generation that happened to be current when they opened the tab.
  const selectedAttempt = viewAttempt == null ? (nodeGeneration ?? 0) : viewAttempt
  const historicalAttempt = selectedAttempt !== (nodeGeneration ?? 0)
  useEffect(() => { setViewAttempt(null) }, [n.id, expectedGeneration])
  // A different node or lifecycle invalidates the anchor: a span id is meaningless outside the trace
  // it came from, and carrying one across would ask the server to place a window it must refuse.
  useEffect(() => { setViewBefore(null) }, [n.id, expectedGeneration, selectedAttempt])
  // What this tab is reading, in the vocabulary every trace surface now shares. The window, the
  // views, the fences and the paged read live in `TraceSurface`; what stays HERE is the node's own
  // chrome — the attempt picker, the destructive clear, the live status, the research disclosure.
  const subject = useMemo(
    () => nodeTraceSubject(n.id, selectedAttempt, viewBefore), [n.id, selectedAttempt, viewBefore])
  // `unavailable` stays bound to the DETAIL payload, never the paged one. It feeds the trace-clear
  // fence below, and a failed paging request is not evidence that this node's telemetry is
  // unreadable — letting it flip `unavailable` would quietly change when a destructive clear is
  // offered, which is not a thing a pager may do.
  const unavailable = traceUnavailable(n.trace?.projection)
  // "Clear trace" erases this node's spans (spans.jsonl is append-only, so a reset+rebuild would else
  // STACK new bands on the old attempt's). Two-click confirm; disabled while THIS node is being worked.
  // The phase machine, its durable recovery record and the request live in ./useTraceClear.js
  // (doc 25 UI-09); what stays here is the control it renders.
  const {
    clearing, clearMessage, clearAvailable, clearUnavailableText,
    clearPrimaryBusy, clearPrimaryVerifying, clearPrimaryConfirm, clearFenced,
    clearTriggerRef, clearConfirmRef, clearRefreshRef,
    beginClear, cancelClear, doClear, refreshClearScope,
  } = useTraceClear({
    nodeId: n.id, nodeStatus: n.status, nodeGeneration, runId,
    expectedGeneration, expectedTraceRevision, live, detailStatus, reloadPending, unavailable,
    onReload, clearScope, clearRecoveryStore, recoverClearState, clearRecoverySignal,
    publishClearRecovery, bodyRef,
    // reload the Conversation bands (now empty until a rebuild re-traces)
    onCleared: () => setNonce(x => x + 1),
  })
  const agent = n.agent_report
  // Live status: what the node is doing RIGHT NOW. Two live states: an LLM authoring the code
  // (building → writing / repairing / merging), or the sandbox running its eval pipeline (pending →
  // training / scoring). `_op` is only set in the building case (the eval has no operator), so it
  // cleanly disambiguates the two.
  // Read this node's OWN raw build marker (buildingMarkers covers EVERY concurrent build AND a
  // node_reset re-build of an existing node, which the spliced `building` flag misses because
  // withBuilding never overwrites an id already in state.nodes), not the singular `live.building`.
  const _bmarker = buildingMarkers(live).find(m => Number(m?.node_id) === Number(n.id))
  const building = working && !!_bmarker
  const _op = building ? (_bmarker.operator || '') : ''
  const statusLabel = !working ? null
    : building
      ? (/repair|debug/.test(_op) ? '🔧 repairing…' : /merge/.test(_op) ? '🔀 merging…' : '✍️ writing code…')
      : '🏋️ training / evaluating…'
  const status = statusLabel && <div className="trace-live-status" role="status"><span className="tls-dot" />{statusLabel}
    <span className="muted trace-live-note">live · auto-updates</span></div>
  const retryParentTrace = () => onReload?.('retry')
  const scrollTo = (where) => { const c = bodyRef.current?.closest('.insp-body'); if (c) c.scrollTop = where === 'top' ? 0 : c.scrollHeight }
  // The attempt picker. Only rendered when there IS an earlier generation — a node that never got
  // repaired has nothing to choose between, and an always-present control implies history that does
  // not exist. Selecting an older attempt is a READ; the destructive clear below stays bound to
  // `nodeGeneration` (the current one) on purpose, so browsing history can never erase it.
  const attemptPicker = attemptOptions.length > 1 && <span className="trace-attempt">
    <label className="muted" htmlFor={`attempt-${n.id}`}>attempt </label>
    <select id={`attempt-${n.id}`} className="seg" value={selectedAttempt}
      title="This node was repaired. Earlier attempts keep their own trace — including the one that crashed."
      onChange={e => setViewAttempt(Number(e.target.value))}>
      {attemptOptions.map(o => <option key={o.attempt} value={o.attempt}>{o.label}</option>)}
    </select>
    {historicalAttempt && <button type="button" className="seg"
      onClick={() => setViewAttempt(null)}>back to current</button>}
  </span>
  // The EPISODE picker, the attempt picker's sibling and NOT its substitute: attempt chooses a
  // lifecycle (what a reset creates), this chooses a position inside one (what a repair loop creates,
  // 2,345 times on the measured node — and none of them an attempt). Rendered exactly when this
  // node's own trace projection says steps are omitted or will not say: with nothing hidden there is
  // nowhere to seek to, and a control implying history that does not exist is the mistake the
  // attempt picker above avoids for the same reason. `null` from `spansOmitted` means "the payload
  // states no usable count" — fail safe and offer the map, since that is the case where the operator
  // is least able to tell what they are missing.
  const traceIsBounded = !unavailable && spansOmitted(n.trace?.projection) !== 0
  const episodePicker = traceIsBounded && <TraceEpisodes
    runId={runId} nodeId={n.id} attempt={selectedAttempt} expectedGeneration={expectedGeneration}
    anchor={viewBefore} onSeek={setViewBefore} nonce={nonce} />
  // THE RESEARCH BEHIND THIS EXPERIMENT. The Researcher works per CARD (a hypothesis) and the
  // Developer per NODE, so this node's reasoning lives one level up and is SHARED with every sibling
  // experiment on the same card. Rather than making the operator go and find it, the node's own trace
  // offers it here: expand it in place, or jump to the card for the full story. Fetched only when
  // opened — most visits to this tab are about the Developer's work, not the proposal.
  const cardId = n.idea?.card_id || null
  const [researchOpen, setResearchOpen] = useState(false)
  const [research, setResearch] = useState(null)
  useEffect(() => { setResearchOpen(false); setResearch(null) }, [cardId, expectedGeneration])
  useEffect(() => {
    if (!researchOpen || research !== null || !cardId || !runId) return undefined
    let alive = true
    // `deadlineGet` returns a HANDLE, not a promise — see the same fix in CardBoard.
    //
    // MEASURED 2026-08-12 on the live run: this route answers in 2.2-10.1 s, so `deadlineGet`'s flat
    // 8 s default aborted it MORE OFTEN THAN IT SUCCEEDED and the disclosure opened on "Trace
    // unavailable for this work item" — a receipt the client manufactured about a read the server
    // would have answered. The cost is not the spans (the light index serves a whole node in
    // 0.03 ms); it is five absent-marker `lstat`s per request at 105-950 ms each on this FUSE mount.
    // Fenced like every other trace read: `expected_generation` on the request and
    // `traceGenerationMatches` on the response. Without both, a read issued before a reset resolves
    // after it and commits the ARCHIVED generation's research rows, while every sibling trace
    // surface refuses the same payload as superseded.
    const request = traceDeadlineGet(
      runApiPath(runId, `/cards/${encodeURIComponent(cardId)}/trace`),
      expectedGeneration, null, 0, traceReadDeadlineMs(0))
    request.promise
      .then(d => {
        if (!alive) return
        if (!traceGenerationMatches(d, expectedGeneration)) {
          setResearch({ projection: { unavailable: true, superseded: true } })
          return
        }
        setResearch(d || {})
      })
      .catch(() => { if (alive) setResearch({ projection: { unavailable: true } }) })
    return () => { alive = false; request.controller.abort() }
  }, [researchOpen, research, cardId, runId, expectedGeneration])
  const researchRows = research
    ? (cardTraceSections(research).find(section => section.kind === 'research')?.rows || [])
    : []
  const researchStrip = cardId && <div className={'trace-research' + (researchOpen ? ' open' : '')}>
    <div className="trace-research-bar">
      <button type="button" className="btn xs ghost trace-research-toggle" aria-expanded={researchOpen}
        title={`The research that proposed ${cardId}. Shared with every experiment on this work item.`}
        onClick={() => setResearchOpen(value => !value)}>
        <span className="trace-research-caret" aria-hidden="true">{researchOpen ? '▾' : '▸'}</span>
        research <span className="muted">· shared with {cardId}</span>
      </button>
      {onOpenCard && <button type="button" className="btn xs ghost"
        onClick={() => onOpenCard(cardId)}>open {cardId} ›</button>}
    </div>
    {researchOpen && <div className="trace-research-body">
      {research === null && <div className="muted" role="status">loading research…</div>}
      {/* A FAILED read is not an absence. The `.catch` above sets `{projection:{unavailable:true}}`,
          and `cardTraceNotice` is the model function that owns exactly this distinction — the card
          board's `_CardTrace` already calls it for the identical payload, so printing our own
          sentence here made the two screens disagree about the same fact. "No research is linked to
          card-3" is a positive claim about the Researcher's proposal; a read that never landed
          cannot support it, and the disclosure is not re-fetched on collapse, so it stuck. */}
      {research !== null && !researchRows.length && <div className="muted" role="status">
        {cardTraceNotice(research)
          || `No research is linked to ${cardId} — it is never inferred from timing.`}</div>}
      <ResearchTraces rows={researchRows} runId={runId} expectedGeneration={expectedGeneration} />
    </div>}
  </div>
  const clearBtn = <span className="trace-clear">
    <button type="button" ref={clearing === '' ? clearTriggerRef : clearConfirmRef}
      className={'seg' + (clearing ? ' on' : '')}
      title={clearPrimaryConfirm ? 'confirm: erase this node’s spans'
        : clearing === '' && !clearAvailable
          ? clearUnavailableText
          : clearing === '' && clearFenced
            ? clearMessage?.verifyOperation
              ? 'verify the original clear operation before clearing trace data again'
              : 'refresh the experiment successfully before clearing trace data again'
          : clearing === '' ? 'erase this node’s captured trace (spans) — useful before re-running the node so the new trace replaces the old'
          : undefined}
      disabled={clearing === '' && (!clearAvailable || clearFenced)}
      aria-disabled={clearPrimaryBusy || undefined} aria-busy={clearPrimaryBusy || undefined}
      aria-label={clearPrimaryConfirm
        ? `Confirm clear trace for experiment #${n.id}, attempt ${nodeGeneration}. Results and run history stay intact.`
        : clearPrimaryVerifying
          ? `Checking the original trace clear outcome for experiment #${n.id}, attempt ${nodeGeneration}.`
        : undefined}
      onClick={clearing === '' ? beginClear : clearPrimaryConfirm ? doClear : undefined}>
      {clearPrimaryVerifying
        ? 'Checking…'
        : clearPrimaryBusy ? 'Clearing…' : clearPrimaryConfirm ? '✕ confirm clear' : '✕ clear trace'}
    </button>
    {clearPrimaryConfirm && <>
      <button className="seg" onClick={cancelClear}>cancel</button>
      <span className="muted trace-clear-status">
        Clear #{n.id} · attempt {nodeGeneration}? Results and run history stay intact.
      </span></>}
    {clearPrimaryBusy && !clearMessage
      && <span className="sr-only" role="status">Clearing trace…</span>}
    {!clearAvailable && clearing === '' && <span className="muted trace-clear-status" role="status">
      {clearUnavailableText}
    </span>}
    {clearMessage && <span className={'muted trace-clear-status'
      + (clearMessage.kind === 'error' ? ' trace-clear-error' : '')}
      role={clearMessage.kind === 'error' ? 'alert' : 'status'}>{clearMessage.text}</span>}
    {clearMessage?.blocking && !clearMessage.pending
      && <button type="button" className="seg" ref={clearRefreshRef}
      aria-disabled={reloadPending || clearMessage.refreshing || undefined}
      aria-busy={reloadPending || clearMessage.refreshing || undefined}
      onClick={refreshClearScope}>
      {clearMessage.verifyOperation
        ? '↻ verify clear outcome'
        : reloadPending || clearMessage.refreshing ? 'Refreshing…' : '↻ refresh experiment'}
    </button>}
  </span>
  const nav = <span className="trace-nav">
    <button className="seg" aria-label="Scroll trace to top" title="scroll to top" onClick={() => scrollTo('top')}>↑</button>
    <button className="seg" aria-label="Scroll trace to newest" title="scroll to newest (bottom)" onClick={() => scrollTo('bottom')}>↓</button></span>
  // STICKY control bar: pinned to the top of the scroll area (position:sticky in .trace-head) so the
  // view toggle / collapse-all / scroll nav stay reachable while you page through a long trace,
  // instead of scrolling off the top. The bar itself belongs to `TraceSurface`; these are the
  // node's own controls, which ride INSIDE it so it stays one row.
  return <TraceSurface subject={subject} runId={runId} expectedGeneration={expectedGeneration}
    working={working} nonce={nonce} bodyRef={bodyRef}
    // The node-DETAIL payload's window is what renders until the operator reaches for more, so this
    // tab fills in live during a build off the detail poll that already runs and the extra request
    // is paid only by the operator who asked for it. (node_detail takes no limit ON PURPOSE — see
    // the comment at its trace assembly; it folds the whole log and serves no trace at all in
    // History.) The detail payload always describes the CURRENT attempt, which is why the surface
    // is told which one that is rather than assuming the selected one.
    detail={{ payload: n.trace, attempt: nodeGeneration ?? 0 }}
    detailUnavailable={unavailable} onRetry={retryParentTrace} retryPending={reloadPending}
    status={status}
    controls={<>{attemptPicker}{episodePicker}<span className="spacer" />{clearBtn}{nav}</>}
    below={researchStrip}
    // Validation is node-level evidence, not a span-tree item. Passing it as the FOOTER keeps it
    // after the bounded tree, which avoids lying about its ARIA parent or pinning a large non-span
    // card inside the virtual list.
    footer={agent ? <AgentReport r={agent} /> : null} />
}

function Code({ n, draftStore, draftScope }) {
  const [diff, setDiff] = useInspectorDraftField(
    draftStore, draftScope, 'diff', false, { disposable: true })
  const files = n.files || {}
  const codeDiff = useMemo(
    () => diff && n.parent_code != null ? diffLines(n.parent_code, n.code) : null,
    [diff, n.parent_code, n.code])
  return <>
    <div className="toolbar code-toolbar">
      {n.parent_code != null && <button className={'btn sm' + (diff ? ' primary' : '')} onClick={() => setDiff(d => !d)}>diff vs parent #{n.parent_id_diffed}</button>}
    </div>
    {codeDiff
      ? <CodeViewer diff={codeDiff} copyText={n.code || ''} label={`Node ${n.id} diff`}
          draftStore={draftStore} draftScope={`${draftScope}:main`} />
      : <CodeViewer code={n.code || '(no solution.py — repo task or no code)'} label={`Node ${n.id} code`}
          draftStore={draftStore} draftScope={`${draftScope}:main`} />}
    {Object.keys(files).length > 0 && <>
      <div className="section-h">Helper files <span className="pill">{Object.keys(files).length}</span></div>
      {Object.entries(files).map(([fn, c]) => <div key={fn}><div className="muted helper-file-label">{fn}</div>
        <CodeViewer code={c} label={fn} maxHeight={300}
          draftStore={draftStore} draftScope={`${draftScope}:file:${fn}`} /></div>)}
    </>}
  </>
}

// Live online metric curves (loss, recall@k, lr, grad norms, …) read from the node's TensorBoard
// events via the metrics adapters. Polls while the node is still running so the curves fill in as
// training progresses; keyed on n.status so a repair-retrain (pending→failed→pending) re-arms the poll.
export function MetricCurves({ runId, nodeId, attempt = 0, status }) {
  const done = ['evaluated', 'failed', 'confirmed'].includes(status)
  const metricAttempt = Number.isInteger(attempt) && attempt >= 0 ? attempt : 0
  const [resource, setResource] = useState(null)
  const [retryNonce, setRetryNonce] = useState(0)
  const requestRef = useRef(0)
  // A terminal node's metrics are immutable — fetch ONCE (ms=null: immediate, no interval) instead of
  // polling every 15s forever. A running node still polls at 3s; a status change (via the `done` dep)
  // re-arms the effect, so a repair-retrain (pending→failed→pending) resumes live polling.
  usePoll((alive) => {
    const request = ++requestRef.current
    const timed = deadlineGet(
      runNodeApiPath(runId, nodeId, `/metrics?attempt=${metricAttempt}`))
    timed.promise.then(d => {
      if (!d?.metrics || Array.isArray(d.metrics)) throw 0
      if (d.node_id !== nodeId || d.attempt !== metricAttempt) throw 0
      if (alive() && request === requestRef.current) setResource(d.metrics)
    }).catch(() => {
      if (alive() && request === requestRef.current) setResource(r => r
        ? Array.isArray(r) ? r : [r] : false)
    })
    return timed
  }, done ? null : 3000, [runId, nodeId, metricAttempt, done, retryNonce],
  { enabled: nodeId != null })
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

// Exported for the same reason `MetricCurves` is: nothing in the suite MOUNTS `Inspector.jsx`, and
// the objective row's label is a claim about a RECORD that has to be driven, not read off the source.
export function Metrics({ n, detail, state, runId }) {
  const seeds = detail?.confirm_seeds_detail || {}
  const vals = Object.entries(seeds).map(([s, v]) => ({ s: Number(s), v })).filter(x => x.v != null).sort((a, b) => a.s - b.s)
  // Every metric reported anywhere in the run (the objective ★ + all extras), shown for
  // THIS node and for the champion (the run's best node), so "the metrics you wanted to see overall"
  // are all visible + comparable. Only the objective drives selection; extras are audit-only.
  //
  // AND WHERE EACH EXTRA CAME FROM, which this table could not say until 2026-08-14. Two channels
  // fill `extra_metrics`: an operator-declared `eval.metrics` reader (guarded — it refuses
  // agent-authored reader code) and auto-capture, which takes every other numeric key off the
  // experiment's own stdout with nothing declaring or checking it. Both rendered here in the same
  // column as the protected objective, an operator reading `speculation_cuda_probe_v` or
  // `train_auc` had no way to tell which was measured and which the candidate simply printed.
  // `unknown` is a run recorded before the channel was written down — NOT a synonym for declared:
  // every such value in the preserved corpus was in fact auto-captured.
  //
  // AND THE ★ ROW'S OWN SOURCE, which said a hardcoded `measured` about every node until
  // 2026-08-15 — including one whose `violations` carry `metric_salvaged`, which this same record
  // describes to the Trust tab as "Metric salvaged, not measured". `trustSemantics.js` owns the
  // decision (see `objectiveMetricSource`): the vocabulary must have ONE home, because two homes
  // drifting is exactly the defect, and the JSX one was reachable by no test.
  const nodes = Object.values(state?.nodes || {})
  const extraKeys = [...new Set(nodes.flatMap(x => Object.keys(x.extra_metrics || {})))]
  const champ = state?.best_node_id != null ? nodes.find(x => x.id === state.best_node_id) : null
  const showChamp = champ && champ.id !== n.id
  const objective = objectiveMetricSource(n)
  // Through the shared predicate, not the inline compare this line used to be: `ParetoPanel` asks
  // the same question about the same nodes, and two spellings of it is how the front and this tab
  // would come to mark different ones.
  const objectiveCaveated = objectiveSourceCaveated(objective)
  // The ★ row prints TWO numbers and the source column can only label one of them. The `best #N`
  // cell is a DIFFERENT node's record, so it gets its own read rather than inheriting this one —
  // under `metric_salvage: "select"` the champion is precisely the node that can be salvaged, and a
  // row reading `salvaged | 0.74 | 0.81` must not leave the second number looking like the measured
  // one by contrast.
  const champObjective = champ ? objectiveMetricSource(champ) : null
  // REVIEW 2026-08-18 (correctness): `extraKeys` is the UNION over ALL nodes, but each extras row's
  // `channel` is read from THIS node only — a key this node never reported still gets
  // `extraMetricChannel(n, k)` = 'unknown', rendering a warn "provenance unknown" label beside an
  // empty cell (a caveat about a value that does not exist, the invented-caveat shape
  // `objectiveMetricSource` forbids). And the champion's extras value in the `best #N` column
  // carries no channel read of its own — only the ★ row reads `champObjective` — so a self-reported
  // champ value sits unlabeled beside this node's labeled one, the exact by-contrast misread the ★
  // cell's comment above warns about. Fix direction: label only cells that hold a value, each from
  // its own node's record (`extraMetricChannel(champ, k)` for the best column).
  const rows = [
    { k: 'objective', mine: n.confirmed_mean ?? n.metric, best: champ ? (champ.confirmed_mean ?? champ.metric) : null, star: true },
    ...extraKeys.map(k => ({
      k, mine: n.extra_metrics?.[k], best: champ?.extra_metrics?.[k],
      channel: extraMetricChannel(n, k),
    })),
  ]
  const anyUnverified = rows.some(r => r.channel && r.channel !== 'declared')
  return <>
    <div className="section-h">Reported metrics{champ ? ` · best = #${champ.id}` : ''}</div>
    <DataTable caption="Node metric comparison" card={false}><table className="tbl"><thead><tr><th>metric</th><th>source</th><th>this node</th>{showChamp && <th>best #{champ.id}</th>}</tr></thead>
      <tbody>{rows.map(r => <tr key={r.k} className={r.star ? 'chosen-row' : ''}>
        <td>{r.star ? '★ ' : ''}{r.k}</td>
        <td className="muted">{r.star
          ? <span className={objectiveCaveated ? 'warn' : ''}
            title={objectiveSourceHelp(objective)}>{OBJECTIVE_SOURCE_LABEL[objective.channel]}</span>
          : <span className={r.channel === 'declared' ? '' : 'warn'}
            title={EXTRA_METRIC_CHANNEL_HELP[r.channel]}>{EXTRA_METRIC_CHANNEL_LABEL[r.channel]}</span>}</td>
        <td>{fmt(r.mine)}</td>
        {showChamp && <td>{r.star && objectiveSourceCaveated(champObjective)
          ? <span className="warn" title={objectiveSourceHelp(champObjective)}>
            {fmt(r.best)} · {OBJECTIVE_SOURCE_LABEL[champObjective.channel]}</span>
          : fmt(r.best)}</td>}</tr>)}</tbody></table></DataTable>
    {/* The extras' footnote below exists because a tooltip is not discoverable — an operator
        scanning a table does not hover every cell. That argument is STRONGER for the ★ row, which
        is the number that drives selection, so the caveat is printed rather than only hovered. It
        renders the SAME sentence the tooltip carries, from the same call, so the two cannot drift. */}
    {objectiveCaveated && <div className="muted">
      The ★ objective is marked <b>{OBJECTIVE_SOURCE_LABEL[objective.channel]}</b>.{' '}
      {objectiveSourceHelp(objective)}
    </div>}
    {anyUnverified && <div className="muted">
      Rows marked <b>self-reported</b> were taken from the experiment's own stdout with nothing
      declaring or checking them — the code that produced the number also chose to print it. They
      are audit-only and never drive selection. <b>provenance unknown</b> means the run predates this
      record; treat it as self-reported.
    </div>}
    {n.confirmed_mean != null && <div className="kv confirmed-metric">
      {/* Same rule as the `|| 'Multiple'` above: `||` falls through on a real 0 and would quietly
          substitute the sample length for a recorded count of zero — a different number presented as
          the recorded one. Only an ABSENT count may fall back. */}
      <KV k="robust mean ± std" v={`${fmt(n.confirmed_mean)} ± ${fmt(n.confirmed_std)} over ${typeof n.confirmed_seeds === 'number' ? n.confirmed_seeds : vals.length} seeds`} /></div>}
    {vals.length > 0 && <>
      <div className="section-h">Per-seed confirmation</div>
      <DataTable caption="Per-seed confirmation metrics" card={false}><table className="tbl"><thead><tr><th>seed</th><th>metric</th></tr></thead>
        <tbody>{vals.map(x => <tr key={x.s}><td>{x.s}</td><td>{fmt(x.v)}</td></tr>)}</tbody></table></DataTable>
    </>}
    <div className="section-h metric-curves-heading">Metric curves
      <span className="muted metric-curves-note">· live logged scalars · grouped</span></div>
    <MetricCurves key={`${runId}:${n.id}:${n.attempt ?? 0}`} runId={runId} nodeId={n.id}
      attempt={n.attempt ?? 0} status={n.status} />
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
      // `|| 'Multiple'` swallowed a REAL zero: a node reporting `confirmed_seeds: 0` alongside a
      // `confirmed_mean` is a contradiction worth seeing, and rewriting it to the reassuring word
      // "Multiple" is the one rendering that hides it. Print the count whenever it is a number —
      // including 0 — and reserve the word for a genuinely absent count.
      // (A `{/* … */}` here is a syntax error: inside a ternary we are in JS, not in JSX children.)
      ? <><State tone="ok" label="Multi-seed confirmed" detail={`${typeof n.confirmed_seeds === 'number' ? n.confirmed_seeds : 'Multiple'} successful seeds are recorded for this node.`} /><div className="kv">
        <KV k="single" v={fmt(n.metric)} />
        <KV k="robust mean" v={fmt(n.confirmed_mean)} />
        <KV k="std" v={fmt(n.confirmed_std)} />
        <KV k="seeds" v={n.confirmed_seeds} />
      </div></>
      : <State tone="warn" label="Single-evaluation only" detail="This node is not multi-seed confirmed and could be seed-lucky." />}
    <div className="section-h">Feasibility</div>
    <State {...feasibility} />
    {/* A salvaged metric has no BOUND — it is not a constraint at all — so rendering it in this
        table produced a row reading "metric_salvaged | (blank) | (blank)". It gets its own row shape
        naming where the number came from, which is the only question that row can usefully answer. */}
    {n.violations?.length
      ? <DataTable caption="Feasibility record" card={false}><table className="tbl"><thead><tr><th>reason</th><th>value</th><th>bound</th></tr></thead>
        <tbody>{n.violations.map((v, i) => isSalvagedMetricViolation(v)
          ? <tr key={i}><td className="flag">metric salvaged</td><td>{fmt(n.metric)}</td>
            <td className="muted">recovered by {v.salvage?.source === 'declared_reader'
              ? 'the run’s own declared reader' : (v.salvage?.source || 'the run')}
              {v.salvage?.stage ? ` after stage “${v.salvage.stage}”` : ''}</td></tr>
          : <tr key={i}><td className="flag">{v.name}</td><td>{fmt(v.value)}</td><td>{v.max != null ? `≤ ${fmt(v.max)}` : `≥ ${fmt(v.min)}`}</td></tr>)}</tbody></table>
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

function Cost({ state }) {
  const c = state.llm_cost
  if (!c) return <div className="muted">No LLM cost recorded (offline/toy run, or run not finished).</div>
  // "$ spent" used to be `fmt(c.cost)`, which printed `0` for a run whose provider never priced a
  // single call — indistinguishable from a run that really cost nothing. `costPricing` owns that
  // distinction; the priced/total call split below is the evidence for whichever answer it gives.
  const pricing = costPricing(c)
  return <div className="kv">
    <KV k="$ spent" v={<span title={pricing.title}>{pricing.text}</span>} />
    <KV k="calls" v={fmtInt(c.calls)} />
    {/* `?? 0` here contradicted `costPricing` on the SAME object two lines up: that function treats an
        absent `priced_calls` as "this payload does not say" and prints the figure with a caveat,
        while this row rewrote the same absence into the hard claim "0 of N priced" — which reads as
        "the provider priced nothing", the one conclusion the absent key cannot support. The row is
        the EVIDENCE for the verdict above it, so it must not assert something the verdict declines
        to. `fmtInt` already renders null/undefined as an em dash. */}
    <KV k="priced by provider" v={<span title={pricing.title}>
      {fmtInt(typeof c.priced_calls === 'number' ? c.priced_calls : null)} of {fmtInt(c.calls)}</span>} />
    <KV k="prompt tokens" v={fmtInt(c.prompt_tokens)} />
    <KV k="completion tokens" v={fmtInt(c.completion_tokens)} />
    <KV k="total tokens" v={fmtInt(c.total_tokens)} />
  </div>
}
