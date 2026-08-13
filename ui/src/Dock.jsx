import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, workingId, getRunCommand, retryRunCommand, runCommand,
  commandFeedback, commandErrorMessage, commandFailureRecord, commandCanRetry, createIdempotencyKey,
  commandActionForEvent, commandRecordMatchesAction, commandEventForAction,
  loadRunTransport, saveRunTransport, clearRunTransport,
  clearRunCommandLock, loadRunCommandLock, saveRunCommandLock, subscribeRunCommandLock,
  COMMAND_SUCCEEDED, COMMAND_FAILED, storageGet, storageSet, runApiPath, runNodeApiPath,
  normalizeRunGeneration, traceDeadlineGet, traceGenerationMatches } from './util.js'
import { useCommandStatusPoll, useNodeSpanWindow, usePoll, useTraceRetry } from './hooks.js'
import {
  commandIntentPreserved, commandLockIdentity, commandLockMismatch, commandStorageUnavailableRecord,
  foreignCommandLock, interruptedCommandRecovery, observeCommandError, pendingCommandRemedy,
  protocolCommandRecord, restoredCommandRecord, settledCommandFailure,
} from './runCommandMachine.js'
import Markdown from './markdown.jsx'
import { NodeTrace, TraceUnavailable } from './Inspector.jsx'
import { OpIcon } from './icons.jsx'
import { runLifecycle } from './runIndex.js'
import VirtualTimeline from './VirtualTimeline.jsx'
import { timelineEventKey } from './timelineModel.js'
import { DataTable } from './accessibility.jsx'
import { tracePartial, traceUnavailable } from './traceProjection.js'
import {
  TRACE_FAILURE_SUPERSEDED, TRACE_FAILURE_UNREADABLE, traceFailureLabel, traceReadDeadlineMs,
  traceRetryMs,
} from './traceScrollModel.js'
import { NARR, GROUPS, GROUP_GLYPH, STATUS_NOISE, TYPE2GROUP, kindOf, isCuratedType,
  eventNarration, liveStatusAgeLabel } from './narration.js'
import { buildingGenerations, buildingMarkers, livePhase, phaseLabel } from './buildingModel.js'
import { DIALOG_PRIORITY, useDialogFocus } from './useDialogFocus.js'

// The timeline is on the hot run route; memo rendering is needed only after a research event is
// expanded. Keep the full evidence/Markdown presentation behind that interaction boundary.
const LazyResearchMemoBody = React.lazy(() => import('./ResearchMemoCard.jsx')
  .then(module => ({ default: module.ResearchMemoBody })))


// The run's EVENTS window (round-9): one scrubbable, filterable feed that renders every run event
// as a differentiated message. The per-run "boss" chat moved to the single persistent assistant, so
// there is no composer here — just the timeline, scrubber, filters, and transport.

// The node an event refers to, if any — lets a feed click drill into that node.
function eventNode(e) {
  const d = e.data || {}
  return d.node_id ?? d.parent_id ?? null
}

function eventNodeAttempt(e) {
  const data = e?.data || {}
  const raw = Object.hasOwn(data, 'generation')
    ? data.generation : (e?.type === 'node_repaired' ? data.attempt : 0)
  return Number.isSafeInteger(raw) && raw >= 0 ? raw : null
}

function verifiableCreatedAttempt(e, currentAttempt) {
  const attempt = eventNodeAttempt(e)
  if (attempt == null) return null
  // An unstamped legacy row is definitely attempt zero only while the current node is still attempt
  // zero. Older servers also left rebuild rows unstamped, so after a reset the prefix snapshot must
  // resolve that row instead of guessing which lifecycle it represents.
  return (Object.hasOwn(e?.data || {}, 'generation') || currentAttempt === 0) ? attempt : null
}

// Events whose row expands to a "why" detail card (reasoning, considered alternatives, context).
const REASONING_TYPES = new Set(['node_created', 'policy_decision', 'strategy_decision', 'research_completed'])
// Events that OWN a node's agent trace — only these expand to the node's span tree (create_node =
// propose+implement, evaluate, repair). Every other event that happens to carry a node_id (foresight/
// hypothesis/strategy/coverage/lessons) shows only its OWN detail, never the node's whole trace.
const TRACE_OWNER_TYPES = new Set(['node_created', 'node_evaluated', 'node_failed', 'node_repaired', 'node_building', 'setup_started'])
// Events the engine wraps in their OWN new_trace op-span (so their trace_id isolates just that
// operation). ONLY these get the per-op trace expansion — an allow-list, because eventstore stamps
// EVERY event with the ambient span's trace_id, so an incidental event appended inside evaluate/
// create_node (spec_drift, novelty_rejected) would otherwise dump that whole node/eval trace.
const OP_TRACE_TYPES = new Set(['strategy_decision', 'hypothesis_merged', 'research_completed',
  'report_generated', 'hypothesis_ranked', 'foresight_selected', 'lessons_distilled', 'lessons_refreshed'])
const CLOSED_EXPANSION = Object.freeze({ open: false, touched: false })

// Pull retained, bounded thinking text for a node out of the trace projection so the feed can surface
// "what was the Researcher thinking" inline. Returns [{op, text}] for the node.
function collectThinking(trace, nid) {
  if (nid == null) return []
  // `trace` here is the PER-NODE trace (/nodes/{nid}/trace), whose `nodes` is already this node's tree
  // LIST; tolerate the old whole-run shape (a {nid: [...]} map) too so nothing breaks mid-transition.
  const spans = Array.isArray(trace?.nodes) ? trace.nodes : ((trace?.nodes || {})[String(nid)] || [])
  const out = []
  const walk = (arr) => (arr || []).forEach(s => {
    (s.events || []).forEach(ev => { if (ev.name === 'llm_call' && ev.thinking) out.push({ op: s.name, text: ev.thinking }) })
    walk(s.children)
  })
  walk(spans)
  return out
}

// A short, honest "what's the agent doing now" line, derived purely from the live state + the latest
// event (no backend signal needed). Drives the animated status strip at the foot of the feed.
// Live agent trace: a collapsed disclosure under the "Thinking…/Planning…" status that streams the
// most recent LLM thoughts + tool calls (with args), so you can see WHAT the agent is doing, not just
// a coarse label. Polls /trace/tail only while OPEN + live (cheap when collapsed). Both this feed and
// per-observation detail are bounded/redacted projections with explicit omission receipts.
// Live-trace paging: the Dock polls a small window; "load earlier spans" raises the requested tail
// limit toward the server ceiling so a user can page back through history on demand instead of just
// reading a dead "projection is partial" notice. TRACE_LIMIT_MAX matches the /trace/tail server cap.
const TRACE_LIMIT_DEFAULT = 40
const TRACE_LIMIT_MAX = 400

// A response that describes another run generation / node / attempt is refused before it renders —
// that fence has always been here. What is new is that refusing it is reported as its own fact:
// these three reads used to `throw 0` into the same `catch` as a dropped connection, so "the run was
// replaced under you" and "we could not read the spans" printed one sentence. See
// traceScrollModel.js for why those two need different words. The tag rides on the Error rather than
// on a second promise channel so the existing single `catch` per read still settles everything.
const supersededTraceRead = () =>
  Object.assign(new Error('trace superseded'), { traceFailure: TRACE_FAILURE_SUPERSEDED })
const traceFailureKind = error => (error?.traceFailure === TRACE_FAILURE_SUPERSEDED
  ? TRACE_FAILURE_SUPERSEDED : TRACE_FAILURE_UNREADABLE)

export function LiveTrace({ runId, generation, active }) {
  const expectedGeneration = normalizeRunGeneration(generation)
  const scope = expectedGeneration || runId
  const [tailState, setTailState] = useState({ scope, items: [], projection: null })
  const [open, setOpen] = useState(false)
  const [limit, setLimit] = useState(TRACE_LIMIT_DEFAULT)
  const bodyRef = useRef(null)
  // Keep one scalar bottom offset while paging upward. Storing both height and top
  // duplicated the same invariant and made this hot owner-route code larger than its bundle budget.
  const stickRef = useRef(true)
  const preserveRef = useRef(null)
  // A new run/generation resets the paging window so a long prior scope doesn't over-fetch here.
  useEffect(() => {
    setLimit(TRACE_LIMIT_DEFAULT); stickRef.current = true; preserveRef.current = null
  }, [scope])
  usePoll((alive) => {
    // The shared, MEASURED deadline (traceScrollModel.js), not `deadlineGet`'s flat 8 s default.
    // The tail's own cost is small — 2.2 s at limit=200, 3.4 s at 400 on the live server — but it
    // pays the same fixed per-request fence cost as every other trace route, which is what put
    // these reads past 8 s and printed "Trace unavailable" over a feed that was merely slow.
    const request = traceDeadlineGet(runApiPath(runId, '/trace/tail'),
      expectedGeneration, null, limit, traceReadDeadlineMs(limit))
    request.promise.then(r => {
      // A 200 envelope is still stale evidence when an old/proxied server serves another run
      // generation. Refuse it before state commit just like the backend's pre/post reset fence.
      if (!traceGenerationMatches(r, expectedGeneration)) throw supersededTraceRead()
      if (alive()) setTailState({
        scope, items: Array.isArray(r?.tail) ? r.tail : [], projection: r?.projection || {},
      })
    }).catch(error => { const failure = traceFailureKind(error); if (alive()) setTailState(previous =>
      previous.scope === scope && previous.projection != null
        && !traceUnavailable(previous.projection)
        ? { ...previous, stale: true, failure }
        : { scope, items: [], projection: { unavailable: true }, failure }) })
    // usePoll owns this handle: dependency changes/unmount abort the old scope, and the deadline
    // settles even a transport that ignores AbortSignal so polling cannot remain wedged forever.
    return request
  },
    3000, [scope, active, open, limit], { enabled: active && open })
  const current = tailState.scope === scope ? tailState : { items: [], projection: null }
  const tail = current.items
  const loaded = current.projection != null
  const unavailable = traceUnavailable(current.projection)
  const partial = tracePartial(current.projection)
  const canLoadEarlier = partial && limit < TRACE_LIMIT_MAX
  const loadEarlier = () => {
    const el = bodyRef.current
    preserveRef.current = el ? el.scrollHeight - el.scrollTop : null
    setLimit(value => Math.min(value * 2, TRACE_LIMIT_MAX))
  }
  // A "load earlier" button at the TOP of the feed (replaces the dead partial notice); a terminal note
  // only when partial but the server ceiling is reached (older spans live in the node's full trace).
  const partialControl = !partial ? null : canLoadEarlier
    ? <button type="button" className="lt-loadmore disclosure-button" onClick={loadEarlier}>↑ load earlier spans</button>
    : <div className="lt-note" role="status">Earlier history is in the node's full trace.</div>
  useLayoutEffect(() => {
    const el = bodyRef.current
    if (!el || !open) return
    if (preserveRef.current != null) {    // just loaded earlier: keep the viewport on the same rows
      el.scrollTop = el.scrollHeight - preserveRef.current
      preserveRef.current = null
    } else if (stickRef.current) {        // parked at the bottom: follow the newest span
      el.scrollTop = el.scrollHeight
    }
  }, [tail, open])
  return (
    <div className={'live-trace' + (open ? ' open' : '')}>
      {/* Standard inline disclosure — caret ▸ left of the label, expands IN PLACE (not a popup). */}
      <button type="button" className="lt-toggle disclosure-button" aria-expanded={open}
           onClick={() => setOpen(o => !o)} title="stream the agent's thoughts + tool calls">
        <span className="lt-caret">{open ? '▾' : '▸'}</span>trace
      </button>
      {open && <div className="lt-body" ref={bodyRef} onScroll={event => {
        const el = event.currentTarget
        stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8
      }}>
        {current.stale && <TraceUnavailable
          label={current.failure === TRACE_FAILURE_SUPERSEDED
            ? traceFailureLabel(TRACE_FAILURE_SUPERSEDED)
            : 'Trace refresh failed; showing confirmed spans while retrying.'} />}
        {!loaded
          ? <div className="muted lt-empty" role="status">loading trace…</div>
          : unavailable
          // This poll re-reads every 3 s, so an unreadable tail really is retrying; a SUPERSEDED one
          // is not, and saying "retrying automatically" about it would promise a recovery that
          // cannot arrive on this scope. Both sentences come from the one vocabulary.
          ? <TraceUnavailable label={traceFailureLabel(current.failure, { retrying: true })} />
          : <>{partialControl}
            {!tail.length && !partial
              ? <div className="muted lt-empty">waiting for the next agent step…</div>
              : tail.map((it, i) => it.kind === 'generation'
            ? <div key={it.span_id || i} className="lt-row lt-gen">
                <span className="lt-ic">🧠</span>
                <span className="lt-txt">{it.text || <span className="muted">({it.model})</span>}</span>
              </div>
            : <div key={it.span_id || i} className="lt-row lt-tool">
                <span className="lt-ic">🔧</span>
                <span className="lt-tool-name">{it.tool}</span>
                {it.arg && <span className="lt-arg">{it.arg}</span>}
              </div>)}</>}
      </div>}
    </div>
  )
}

// Bookkeeping events that do NOT reflect what the agent is DOING — skip them when inferring the
// between-experiments status, else the strip flickers to "Thinking…" every time one of these lands
// right after a node (coverage/cost/lessons/reflection all fire post-eval). Defined in narration.js
// because the status CLOCK reads the same filter: the label and its age must never be able to
// describe different moments.

function agentStatus(live, log) {
  if (!live) return null
  const lifecycle = runLifecycle(live)
  if (lifecycle.mode === 'finished') return null
  if (lifecycle.mode === 'finishing') return 'Finishing write-out…'
  if (lifecycle.mode === 'finalization-stalled') return 'Finalization stalled — recovery required.'
  if (lifecycle.mode === 'finalizing') return 'Finalizing report, memory, and cost…'
  if (live.paused) return 'Paused'
  // Zombie guard: the run isn't finished but no engine process holds the lock (engine_running===false,
  // server-probed). Without this the strip would pulse "Thinking about the next step…" forever even
  // though nothing is running — the exact symptom of a resume that died without emitting run_finished.
  if (live.engine_running === false) return 'Engine stopped — resume to continue'
  const phase = live.phase
  if (phase === 'grounding' || phase === 'onboarding') return 'Setting up task and data…'
  if (phase === 'approval') return 'Waiting for approval…'
  if (phase === 'spec_approval') return 'Waiting for eval-spec approval…'
  // WRITING vs RUNNING are distinct and were conflated before (both said "Running experiment"):
  //   • `building` is set from node_building until node_created folds → the Developer is WRITING code;
  //   • a node with status 'pending' → its code is written and the sandbox is TRAINING it.
  // Parallel builds (parallel_build>1): several Developers write at once. Show the count — mirroring the
  // parallel-eval strip below — instead of naming only the last-appended build. Derive the label from the
  // `buildings` marker LIST (node_id->marker object) so the single-build label is right even after the
  // last-appended build (the singular `live.building`) finishes but a sibling survives. Fall back to the
  // singular `building` for a serial-build run or an old server that doesn't send `buildings`.
  // The PHASE beacon first, and above the marker check on purpose. It is strictly more specific than
  // everything below it: it names the step inside the build ("Writing code for experiment #7…"
  // rather than "Writing experiment #7…"), and — the part that actually closes the operator's
  // complaint — it is the ONLY thing that can speak during the proposal, which runs BEFORE
  // `node_building` is appended and therefore before any marker below exists. Without this the strip
  // fell through past every branch to "Planning next experiment…" and sat there for the whole
  // Researcher call. A resume beacon reaches here for the same structural reason: the prologue runs
  // before the loop's first turn, so no marker, node or pending count has moved yet.
  // NOT `phase` — that name is already the run-level `live.phase` string ten lines up, and shadowing
  // it here is a parse error rather than a subtle bug only because they share a scope.
  const stepLabel = phaseLabel(livePhase(live, log))
  if (stepLabel) {
    // Parallel builds still need the COUNT, which the phase alone cannot carry: one beacon describes
    // one build. Keep the marker-derived fan-out and let the phase say what they are all doing.
    const parallel = buildingMarkers(live)
    return parallel.length > 1 ? `${stepLabel} (${parallel.length} in parallel)` : stepLabel
  }
  const buildMarkers = buildingMarkers(live)
  if (buildMarkers.length > 1) {
    return `Writing ${buildMarkers.length} experiments in parallel…`
  }
  if (buildMarkers.length === 1) {
    const op = buildMarkers[0].operator || ''
    const id = buildMarkers[0].node_id
    const action = /repair|debug/.test(op) ? 'Repairing' : /merge/.test(op) ? 'Merging into' : 'Writing'
    return `${action} experiment #${id}…`
  }
  const pend = Object.values(live.nodes || {}).filter(n => n.status === 'pending')
  // Surface eval_parallel fan-out. Each node owns its admitted reservation, which may be CPU-only or
  // span one or more GPUs; the strip used to name only the highest id and hide concurrent work.
  if (pend.length > 1) return `Running ${pend.length} experiments in parallel…`
  if (pend.length) return `Running experiment #${pend[0].id}… (training)`
  // Between experiments: infer from the last MEANINGFUL event (skip the bookkeeping noise above), so the
  // label stays put on "Planning…" instead of blinking every time a coverage/cost event lands.
  let last = null
  for (let i = log.length - 1; i >= 0; i--) { if (!STATUS_NOISE.has(log[i].type)) { last = log[i].type; break } }
  if (last === 'setup_started' || last === 'setup_step' || last === 'workspace_seeded') return 'Setting up task and data…'
  if (last === 'run_setup_started') return 'Installing dependencies…'
  if (last === 'strategy_decision' || last === 'set_strategy') return 'Choosing a strategy…'
  if (last === 'research_completed' || last === 'deep_research') return 'Reading the literature…'
  if (last === 'node_created') return 'Writing and running experiment…'
  // node_evaluated / node_failed / policy_decision / agent_decision → the loop is picking what's next.
  return 'Planning next experiment…'
}

const Disclosure = ({ label, children }) => {
  const [open, setOpen] = useState(false)
  return <div className="think-debug trace-disclosure">
    <button type="button" className="role-think disclosure-button trace-disclosure-toggle" aria-expanded={open}
         onClick={() => setOpen(v => !v)}>
      {open ? '▾' : '▸'} {label}</button>
    {open && children}
  </div>
}

function NodeCreatedDetail({ d, trace }) {
  const idea = d.idea || {}
  const think = collectThinking(trace, d.node_id)
  const params = idea.params || {}
  const space = idea.space || {}
  return (
    <div className="ev-detail">
      <div className="section-h">Conclusion — why this experiment next</div>
      {idea.rationale ? <Markdown className="rationale-md" text={idea.rationale} /> : <div className="v">—</div>}
      <div className="ev-meta">
        <span>operator <b>{idea.operator || d.operator}</b></span>
        {(d.parent_ids || []).length > 0 && <span>built from {d.parent_ids.map(p => '#' + p).join(', ')}</span>}
        {Object.keys(params).length > 0 && <span>params {Object.entries(params).map(([k, v]) => `${k}=${fmt(v, 3)}`).join(', ')}</span>}
        {Object.keys(space).length > 0 && <span>sweep {Object.entries(space).map(([k, v]) => `${k}∈[${(Array.isArray(v) ? v : [v]).join(', ')}]`).join('; ')}</span>}
      </div>
      {think.length > 0 && <Disclosure label="Researcher thinking (debug)">
        {think.map((t, i) => <Markdown key={i} className="think-body" text={t.text} />)}
      </Disclosure>}
    </div>
  )
}

function PolicyDetail({ d }) {
  const scores = d.scores || {}
  const entries = Object.entries(scores).sort((a, b) => b[1] - a[1])
  return (
    <div className="ev-detail">
      <div className="section-h">Why this node{d.reason ? ` — ${d.reason}` : ''}</div>
      {entries.length === 0
        ? <div className="v muted">chose #{d.chosen} (no candidate scores recorded)</div>
        : <DataTable caption="Candidate scores for node selection" card={false}><table className="tbl"><thead><tr><th>node</th><th>score</th></tr></thead>
            <tbody>{entries.map(([nid, sc]) =>
              <tr key={nid} className={String(nid) === String(d.chosen) ? 'chosen-row' : ''}>
                <td>#{nid}{String(nid) === String(d.chosen) ? ' ✓ chosen' : ''}</td><td>{fmt(sc, 4)}</td></tr>)}
            </tbody></table></DataTable>}
    </div>
  )
}

function StrategyDetail({ d }) {
  const s = d.strategy || {}
  const ctx = d.ctx || {}
  const ctxRows = Object.entries(ctx).filter(([, v]) => v != null && typeof v !== 'object')
  return (
    <div className="ev-detail">
      <div className="section-h">Why this strategy</div>
      <div className="v">{s.rationale || '—'}</div>
      <div className="ev-meta">
        <span>policy <b>{s.policy || '?'}</b></span>
        {s.fidelity && <span>fidelity {s.fidelity}</span>}
        {s.developer && <span>developer {s.developer}</span>}
        {s.eval_parallel != null && <span>eval parallel {s.eval_parallel}</span>}
        {s.llm_parallel != null && <span>LLM total {s.llm_parallel}</span>}
        {s.llm_lane_limits && typeof s.llm_lane_limits === 'object'
          && !Array.isArray(s.llm_lane_limits)
          && <span>LLM lanes {Object.entries(s.llm_lane_limits).slice(0, 5)
            .map(([lane, width]) => `${lane}:${width}`).join(', ')}</span>}
        {s.card_scoring && typeof s.card_scoring === 'object' && !Array.isArray(s.card_scoring)
          && <span>Card scoring {s.card_scoring.stance || 'balanced'} · novelty {s.card_scoring.novelty_weight}
            {' · '}coverage {s.card_scoring.coverage_weight}</span>}
        {s.source && <span>source {s.source}</span>}
      </div>
      {ctxRows.length > 0 && <>
        <div className="section-h">Decision context</div>
        <div className="ev-meta">{ctxRows.map(([k, v]) =>
          <span key={k} className="ev-ctx"><b>{k}</b> {String(v)}</span>)}</div>
      </>}
    </div>
  )
}

export function ResearchDetail({ d, MemoBody = LazyResearchMemoBody }) {
  return <div className="ev-detail research-event-detail">
    <React.Suspense fallback={<div className="muted" role="status">Loading research memo…</div>}>
      <MemoBody memo={d.memo} showSummary compact />
    </React.Suspense>
  </div>
}

function reasoningDetail(e, trace) {
  const d = e.data || {}
  if (e.type === 'node_created') return <NodeCreatedDetail d={d} trace={trace} />
  if (e.type === 'policy_decision') return <PolicyDetail d={d} />
  if (e.type === 'strategy_decision') return <StrategyDetail d={d} />
  if (e.type === 'research_completed') return <ResearchDetail d={d} />
  return null
}

// The available projected text behind a feed row whose one-line narration clamped it (node_failed's
// triage, node_repaired's rationale, the report headline, a hint, …). The page-level omission receipt
// remains authoritative when a source event exceeded the response cap. Returns [] when absent.
function genericRows(e) {
  const d = e.data || {}
  const rows = []
  const add = (label, v) => { if (v != null && String(v).trim()) rows.push([label, String(v)]) }
  add('rationale', d.rationale)
  add('triage', d.triage_rationale)
  add('reason', d.reason)
  add('error', d.error)
  add('headline', d.content?.headline)
  add('summary', d.memo?.summary || d.summary)
  add('hint', d.text)
  return rows
}

function GenericDetail({ e }) {
  const rows = genericRows(e)
  if (!rows.length) return <div className="ev-detail"><pre className="code event-json">{JSON.stringify(e.data || {}, null, 2)}</pre></div>
  return <div className="ev-detail">{rows.map(([k, v], i) =>
    <React.Fragment key={i}><div className="section-h">{k}</div><div className="v">{v}</div></React.Fragment>)}</div>
}

// The trace of ONE sub-operation (strategy_consult / hypothesis_merge …), fetched lazily by the
// event's own trace_id — so a strategy_decision row shows only the strategist's reasoning, not the
// whole node. Rendered with the same span-tree component as a node's trace.
export function OpTrace({ runId, traceId, expectedGeneration }) {
  const scope = `${expectedGeneration || runId}:${traceId}`
  const [traceState, setTraceState] = useState(null)
  const [retryNonce, setRetryNonce] = useState(0)
  const trace = traceState?.scope === scope ? traceState : null
  // A ONE-SHOT read that failed used to stay failed until the operator clicked Retry — against a
  // route measured at seconds per call, that is how a slow moment becomes a permanent "Trace
  // unavailable". The budget, and the rule that a superseded read is never re-asked, are in
  // traceScrollModel.js.
  const autoRetryMs = traceRetryMs(trace?.failures, trace?.failure)
  const failures = trace?.failures || 0
  // Deliberately its own timer rather than a poll interval: `usePoll` reads IMMEDIATELY whenever its
  // dependencies change, and arming a retry is the one restart that has to wait. It also clears
  // itself on the first fire, so exactly one re-read is issued per observed failure — a read that is
  // merely slow is never cut short and re-issued underneath itself.
  useTraceRetry(autoRetryMs, failures, setRetryNonce)
  usePoll((alive) => {
    const request = traceDeadlineGet(runApiPath(
      runId, `/trace/by_trace/${encodeURIComponent(traceId)}`),
      expectedGeneration, null, 0, traceReadDeadlineMs(0))
    request.promise.then(d => {
      if (!traceGenerationMatches(d, expectedGeneration)) throw supersededTraceRead()
      if (alive()) setTraceState({
        scope, spans: Array.isArray(d?.spans) ? d.spans : [], projection: d?.projection || {},
      })
    }).catch(error => { const failure = traceFailureKind(error); if (alive()) setTraceState(previous => {
      const failures = (previous?.scope === scope ? previous.failures || 0 : 0) + 1
      return previous?.scope === scope && !traceUnavailable(previous.projection)
        ? { ...previous, stale: true, failure, failures }
        : { scope, spans: [], projection: { unavailable: true }, failure, failures }
    }) })
    return request
  }, null, [scope, retryNonce])
  // Retry REFILLS the budget: the operator asking again is a fresh gesture, exactly as it is for the
  // scroll loader's `armTraceScroll('focus')`. It clears the COUNTER, never the payload — blanking
  // last-good spans back to "loading…" while their replacement is in flight is the same defect the
  // node-trace retry below is pinned against.
  const retry = () => {
    setTraceState(previous => (previous?.scope === scope ? { ...previous, failures: 0 } : previous))
    setRetryNonce(value => value + 1)
  }
  if (trace === null)
    return <div className="muted trace-loading" role="status">loading trace…</div>
  const retrying = autoRetryMs != null
  // The receipt is rendered HERE rather than delegated to NodeTrace's own default: only this
  // component knows WHICH failure it was, and a surface that cannot name it prints one sentence for
  // two facts. The Retry button stays enabled while a re-read is scheduled — the wait is in the
  // label, and the operator must keep the option of asking now.
  if (traceUnavailable(trace.projection)) return <TraceUnavailable
    label={traceFailureLabel(trace.failure, { retrying })} onRetry={retry} />
  return <>{trace.stale && <TraceUnavailable
    label={trace.failure === TRACE_FAILURE_SUPERSEDED
      ? traceFailureLabel(TRACE_FAILURE_SUPERSEDED)
      : 'Trace refresh failed; showing confirmed spans.'} onRetry={retry} />}
    <NodeTrace spans={trace.spans} projection={trace.projection} runId={runId}
      expectedGeneration={expectedGeneration} treeKey={scope} onRetry={retry} /></>
}

// One feed row, chat-message styled: an icon/color by kind, the narration, an expandable "why" card.
export function EventRow({ e, onFocusEvent, focusLabel, nodeCreatedAttempt, autoOpen, runId,
  runGeneration, readOnly, liveBuilding, expansion, onExpansionChange }) {
  const [localOpen, setLocalOpen] = useState(autoOpen)
  const localTouched = useRef(false)
  const controlled = expansion != null
  const open = controlled ? expansion.open === true : localOpen
  const touched = controlled ? expansion.touched === true : localTouched.current
  const changeOpen = (next, wasTouched = touched) => {
    if (controlled) onExpansionChange?.({ open: next, touched: wasTouched })
    else { localTouched.current = wasTouched; setLocalOpen(next) }
  }
  // collapse-when-done: follow autoOpen (expand while live, collapse when the node resolves) UNLESS
  // the user manually toggled this card — then their choice wins.
  useEffect(() => { if (!touched && open !== autoOpen) changeOpen(autoOpen, false) }, [autoOpen])
  const nid = eventNode(e)
  const hasReason = REASONING_TYPES.has(e.type)
  // The node's span trace (create_node = propose+implement, or evaluate) belongs ONLY to the events
  // that ARE that node's work — its lifecycle. Incidental sub-operation events (foresight_ranked/
  // _selected, hypothesis_ranked/_merged, strategy_decision, coverage_snapshot, …) merely carry a
  // node_id for CONTEXT; expanding them must NOT dump the node's whole Researcher+Developer trace —
  // that isn't their work, and it's the exact "why is the Researcher+Developer trace under a foresight
  // row" bug. They fall through to their OWN reasoning/data detail below. (Per-operation LLM traces
  // for these would need a named span + an event span_id — a backend change; TODO.) `setup_started`
  // surfaces the SETUP phase's tree (pseudo-node -1). OWN node_id only — never the parent_id fallback.
  const traceNid = TRACE_OWNER_TYPES.has(e.type)
    ? (e.data?.node_id ?? (e.type === 'setup_started' ? -1 : null))
    : null
  // LAZY: fetch only THIS node's bounded/redacted trace projection (/nodes/{nid}/trace — reads just
  // the node's spans via the index, O(node)), and only when the row is expanded. Per-observation
  // bounded/redacted detail is fetched on demand via /spans/{sid}.
  const [nodeTraceState, setNodeTrace] = useState(null)
  const [nodeTraceNonce, setNodeTraceNonce] = useState(0)
  // Use the server's documented default explicitly, then double to its bounded ceiling.
  // This removes the special zero/default request path without changing the first response window.
  // The window rule itself is the shared one (hooks.js::useNodeSpanWindow) — this surface used to own
  // a private copy of the ceiling, and the Inspector's copy of the same route had no pager at all.
  const { limit: nodeTraceLimit, loadMore: loadMoreNodeTrace } = useNodeSpanWindow()
  // Keep the inline evidence on the same attempt as the row destination. An unstamped legacy
  // node_created after a reset is ambiguous, so it keeps its rationale but does not guess a trace.
  const traceGeneration = e.type === 'node_created' ? nodeCreatedAttempt : eventNodeAttempt(e)
  const expectedTraceGeneration = normalizeRunGeneration(runGeneration)
  const nodeTraceScope = `${expectedTraceGeneration || runId}:${traceNid}:${traceGeneration}`
  const currentNodeTrace = nodeTraceState?.scope === nodeTraceScope ? nodeTraceState : null
  const nodeTrace = currentNodeTrace?.payload
  const nodeTraceError = currentNodeTrace?.failed
  const nodeTraceFailure = currentNodeTrace?.failure
  // `liveBuilding` is a plain object {nodeId: generation} of every concurrent build
  // (`buildingGenerations()` builds it with `const generations = {}`), so it is read with
  // BRACKETS, never `.get()`. This row live-polls its trace only when it IS one of those
  // exact building lifecycles (right node AND right generation).
  const exactBuilding = liveBuilding != null && traceNid != null && traceGeneration != null
    && liveBuilding[traceNid] === traceGeneration
  // A node that is NOT currently building reads its trace ONCE (`ms = null`), so before the budget
  // below one failure left this row on a dead receipt for as long as it stayed expanded. A live one
  // already recovers on its own 4 s tick and must keep that cadence, so the budget only ever
  // supplies the interval a one-shot read had none of.
  const nodeAutoRetryMs = exactBuilding ? null
    : traceRetryMs(currentNodeTrace?.failures, nodeTraceFailure)
  useTraceRetry(nodeAutoRetryMs, currentNodeTrace?.failures || 0, setNodeTraceNonce)
  // Clear the error flag only on a SUCCESSFUL exact-attempt load (not eagerly at each poll tick).
  usePoll((alive) => {
    const request = traceDeadlineGet(runNodeApiPath(runId, traceNid, '/trace'),
      expectedTraceGeneration, traceGeneration, nodeTraceLimit,
      // The shared measured deadline, not the flat 8 s default this read used to inherit: on the
      // live server `/nodes/{n}/trace?limit=512` answered in 4.3-15.6 s for a SIX-span node, so the
      // old bound aborted reads whose payload was 1.4 KB (see traceScrollModel.js).
      traceReadDeadlineMs(nodeTraceLimit))
    request.promise.then(d => {
      if (d?.node_id !== traceNid || d?.attempt !== traceGeneration
          || !traceGenerationMatches(d, expectedTraceGeneration)) throw supersededTraceRead()
      if (alive()) setNodeTrace({ scope: nodeTraceScope, payload: d })
    })
      .catch(error => { const failure = traceFailureKind(error)
        if (alive()) setNodeTrace(previous => {
          const failures = (previous?.scope === nodeTraceScope ? previous.failures || 0 : 0) + 1
          return previous?.scope === nodeTraceScope
            ? { ...previous, failed: true, failure, failures }
            : { scope: nodeTraceScope, failed: true, failure, failures }
        }) })
    return request
  }, exactBuilding ? 4000 : null,
    [open, readOnly, runId, expectedTraceGeneration, traceNid, traceGeneration, exactBuilding,
      nodeTraceNonce, nodeTraceLimit],
    { enabled: open && !readOnly && traceNid != null && traceGeneration != null })
  // Refills the retry budget without touching last-good spans — see OpTrace's `retry`.
  const retryNodeTrace = () => {
    setNodeTrace(previous => (previous?.scope === nodeTraceScope
      ? { ...previous, failures: 0 } : previous))
    setNodeTraceNonce(value => value + 1)
  }
  const nodeSpans = Array.isArray(nodeTrace?.nodes) ? nodeTrace.nodes : []
  const hasTrace = !readOnly && traceNid != null && traceGeneration != null
  // A sub-operation event the engine wrapped in its OWN named trace (strategy_decision, hypothesis_
  // merged) carries a trace_id — expand to ONLY that operation's trace (lazily fetched by trace_id),
  // never the node's whole Researcher+Developer trace. Old events (no trace_id) fall through to detail.
  const opTraceId = (!readOnly && OP_TRACE_TYPES.has(e.type) && e.trace_id) ? e.trace_id : null
  // A row whose one-line narration clamped text (or used the projected JSON fallback) remains
  // expandable to the detail retained in this bounded event page.
  const isRawFallback = !hasReason && !NARR[e.type]
  const hasGeneric = !hasReason && (genericRows(e).length > 0 || isRawFallback)
  const omittedBytes = e?._log_page?.truncated ? Number(e._log_page.raw_bytes || 0) : 0
  const hasOmittedDetail = e?._log_page?.truncated === true
  const expandable = hasReason || hasTrace || !!opTraceId || hasGeneric || hasOmittedDetail
  const { group, glyph } = kindOf(e.type)
  const narr = eventNarration(e)
  const detailsId = `timeline-event-${e.seq}-details`
  return (
    <div className={'feed-msg k-' + group}>
      <div className="fm-ic" title={group}><OpIcon name={glyph} size={14} className="fm-ic-svg" /></div>
      <div className="fm-body">
        <div className="fm-line">
          {expandable && <button type="button" className="fm-tw" aria-expanded={open}
            aria-controls={detailsId}
            aria-label={`${open ? 'Collapse' : 'Expand'} details for event ${e.seq}`}
            onClick={() => changeOpen(!open, true)}>{open ? '▾' : '▸'}</button>}
          <button type="button" className="fm-main" onClick={() => onFocusEvent(e)}
            aria-label={`${narr}. ${focusLabel}`}
            title={focusLabel}>
            <span className="fm-narr">{narr}</span>
            {nid != null && <span className="ev-go">↗</span>}
          </button>
        </div>
        {open && expandable && <div className="ev-detail-wrap" id={detailsId}>
          {hasOmittedDetail && <div className="notice" role="note">
            Event details were not transferred: {omittedBytes.toLocaleString()} source bytes exceed the bounded page response.
          </div>}
          {hasReason && reasoningDetail(e, nodeTrace)}
          {hasGeneric && <GenericDetail e={e} />}
          {hasTrace && nodeTrace == null && !nodeTraceError && <div className="muted" role="status">loading node trace…</div>}
          {/* The Retry button stays ENABLED while an automatic re-read is scheduled: the wait is
              announced in the label, and disabling the one control the operator has in order to
              display a busy state would take away the ability to ask now. */}
          {hasTrace && nodeTraceError && <TraceUnavailable
            label={nodeTraceFailure === TRACE_FAILURE_SUPERSEDED
              ? traceFailureLabel(TRACE_FAILURE_SUPERSEDED)
              : nodeTrace == null
              ? (nodeAutoRetryMs != null ? 'Could not load node trace; retrying automatically.'
                : 'Could not load node trace.')
              : 'Node trace refresh failed; showing confirmed spans.'}
            onRetry={retryNodeTrace} />}
          {hasTrace && nodeTrace != null && <NodeTrace spans={nodeSpans}
            projection={nodeTrace.projection} runId={runId} onRetry={retryNodeTrace}
            onLoadMore={loadMoreNodeTrace} spanLimit={nodeTraceLimit}
            expectedGeneration={expectedTraceGeneration} treeKey={nodeTraceScope} />}
          {opTraceId && (e.type === 'research_completed'
            ? <Disclosure label="research process & tool activity">
                <OpTrace runId={runId} traceId={opTraceId}
                  expectedGeneration={expectedTraceGeneration} />
              </Disclosure>
            : <OpTrace runId={runId} traceId={opTraceId}
                expectedGeneration={expectedTraceGeneration} />)}
        </div>}
      </div>
    </div>
  )
}

const TRANSPORT_INTENTS = {
  stop: { type: 'pause', data: {} },
  finalize: { type: 'run_abort', data: { reason: 'finalized' } },
  resume: { type: 'resume', data: {} },
}

const recoveryForRun = (runId) => {
  const saved = loadRunTransport(runId)
  if (!saved) return { pending: null, failure: null }
  if (saved.protocolInvalid) return { pending: {
    action: saved.action, idempotencyKey: saved.idempotencyKey, record: saved.record,
    expectedGeneration: saved.expectedGeneration,
    statusUnavailable: true, observationKind: 'protocol', protocolInvalid: true,
    canResubmit: false, lastError: 'Stored command recovery data is invalid.',
  }, failure: null }
  const record = restoredCommandRecord(saved)
  if (COMMAND_SUCCEEDED.has(record.status)) {
    clearRunTransport(runId)
    return { pending: null, failure: null }
  }
  const knownStatus = record.status === 'accepted' || record.status === 'executing'
    || COMMAND_FAILED.has(record.status)
  const needsObservation = !knownStatus || !record.id || interruptedCommandRecovery(saved)
  const entry = {
    action: saved.action, idempotencyKey: saved.idempotencyKey, record,
    expectedGeneration: saved.expectedGeneration,
    statusUnavailable: !!saved.statusUnavailable || needsObservation,
    observationKind: saved.observationKind || (!knownStatus && record.id ? 'protocol'
      : (needsObservation ? 'transport' : null)),
    lastError: interruptedCommandRecovery(saved)
      ? 'The page reloaded while recovery was in progress; check the durable command status.'
      : !knownStatus ? 'Stored command state needs to be checked against the server.' : '',
  }
  return settledCommandFailure(saved, record)
    ? { pending: null, failure: entry }
    : { pending: entry, failure: null }
}

// Round-9: the per-run "boss" chat moved to the single persistent assistant, so the Dock is purely
// the run's EVENTS window — the timeline feed + scrubber + filters + transport.
export default function Dock({ runId, live, liveSeq, expectedGeneration, timeline, viewSeq, setViewSeq,
  onReturnToLive, onFocus, collapsed, onToggleCollapse, height = 230, onToast, readOnly = false,
  publishTransport = null, filter = '', onFilterChange = null, kindFilters = [],
  onKindFiltersChange = null, focusOnMount = false, onInitialFocus = null,
  collapseControlRef = null, startOverState = null, onStartOver = null }) {
  const log = timeline.rows
  const collapseButtonRef = useRef(null)
  const startOverDialogRef = useRef(null)
  const [startOverDialogIntent, setStartOverDialogIntent] = useState(null)
  const closeStartOverDialog = () => setStartOverDialogIntent(null)
  const restoreDockFocus = () => requestAnimationFrame(() => {
    const target = collapseButtonRef.current || document.querySelector('[data-route-main]')
    target?.focus?.({ preventScroll: true })
  })
  useDialogFocus(startOverDialogRef, closeStartOverDialog, !!startOverDialogIntent,
    { priority: DIALOG_PRIORITY.START_OVER })
  const setCollapseButtonRef = element => {
    collapseButtonRef.current = element
    if (typeof collapseControlRef === 'function') collapseControlRef(element)
    else if (collapseControlRef) collapseControlRef.current = element
  }
  useEffect(() => {
    if (!focusOnMount) return
    collapseButtonRef.current?.focus({ preventScroll: true })
    onInitialFocus?.()
  }, [focusOnMount])
  // URL-owned diagnostic filters: Dock renders them, while RunView commits the canonical fragment
  // state. This lets reload/Back/Forward restore the exact event lens without a second store.
  const kinds = useMemo(() => new Set(kindFilters), [kindFilters])
  const setKinds = (value) => {
    const next = typeof value === 'function' ? value(new Set(kinds)) : value
    onKindFiltersChange?.([...next])
  }
  const restoredRef = useRef(null)
  if (!restoredRef.current || restoredRef.current.runId !== runId) {
    restoredRef.current = { runId, ...recoveryForRun(runId) }
  }
  const [transportPending, setTransportPending] = useState(() => restoredRef.current.pending)
  const [transportFailure, setTransportFailure] = useState(() => restoredRef.current.failure)
  const [runCommandLock, setRunCommandLock] = useState(() => loadRunCommandLock(runId))
  const externalTransportPending = runCommandLock?.source === 'assistant' ? runCommandLock : null
  const transportBusy = !!transportPending || !!externalTransportPending
  const runActionBusy = transportBusy || !!startOverState?.lifecycleBlocked
  const startOverDisabled = transportBusy || !!startOverState?.blocked
  // Expansion is view-owned rather than row-owned: virtual rows may unmount offscreen, but a user's
  // open reasoning/trace card must still be open when that retained event comes back into view.
  const [eventExpansion, setEventExpansion] = useState(() => new Map())
  useEffect(() => setEventExpansion(new Map()), [runId, timeline.generation])
  useEffect(() => {
    const retained = new Set(log.map(timelineEventKey))
    setEventExpansion(current => {
      if ([...current.keys()].every(key => retained.has(key))) return current
      return new Map([...current].filter(([key]) => retained.has(key)))
    })
  }, [log])
  useEffect(() => {
    const restored = recoveryForRun(runId)
    const lock = loadRunCommandLock(runId)
    if (foreignCommandLock(lock, 'dock') && (restored.pending || restored.failure)) {
      clearRunTransport(runId)
      setTransportPending(null); setTransportFailure(null)
    } else {
      let pending = restored.pending
      const entry = restored.pending || restored.failure
      const lockMismatch = commandLockMismatch(lock, 'dock', entry && {
        idempotencyKey: entry.idempotencyKey, action: entry.action,
        expectedGeneration: entry.expectedGeneration, commandId: entry.record?.id,
      })
      if (lockMismatch) pending = { ...entry, statusUnavailable: true, observationKind: 'protocol',
        protocolInvalid: true, canResubmit: false, lockIdentity: lock,
        lastError: 'Stored command identity does not match the active recovery lock.' }
      setTransportPending(pending); setTransportFailure(lockMismatch ? null : restored.failure)
      if (pending?.protocolInvalid) {
        saveRunCommandLock(runId, { ...pending, source: 'dock' })
      } else if (pending) saveRunTransport(runId, pending)
      else if (!restored.failure && lock?.source === 'dock') {
        clearRunCommandLock(runId, { source: 'dock', idempotencyKey: lock.idempotencyKey,
          action: lock.action, expectedGeneration: lock.expectedGeneration,
          commandId: lock.commandId })
      }
    }
  }, [runId])
  useEffect(() => {
    setRunCommandLock(loadRunCommandLock(runId))
    return subscribeRunCommandLock(runId, setRunCommandLock)
  }, [runId])
  // round-7: scrubber + filter chips collapse into one block to save space; default hidden, remembered.
  const [showControls, setShowControls] = useState(() => storageGet('ll.dock.controls') === '1')
  const filtersActive = !!(filter.trim() || kinds.size > 0)
  const toggleControls = () => {
    // "Controls" is also a reveal action: never announce an expanded disclosure whose controlled
    // region is still hidden inside a collapsed timeline.
    if (collapsed) {
      if (!showControls) {
        storageSet('ll.dock.controls', '1')
        setShowControls(true)
      }
      onToggleCollapse?.()
      return
    }
    setShowControls(v => { const n = !v; storageSet('ll.dock.controls', n ? '1' : '0'); return n })
  }
  useEffect(() => {
    if (filter.trim() || kinds.size > 0) setShowControls(true)
  }, [filter, kinds.size])
  // Trace details are fetched per node, only when that row is expanded. This keeps the virtualized,
  // paged timeline O(visible events) and avoids folding or transferring the whole run trace.
  const atLiveView = viewSeq == null || viewSeq >= liveSeq
  const visiblyLive = atLiveView && timeline.followingTail && timeline.windowAtTail

  // The live frontier: the highest-id node still pending while the run runs — its proposal card stays
  // expanded ("thinking") until it resolves. null on a finished/replayed run — AND on a STALLED/zombie
  // run (engine_running===false): a run whose engine died mid-eval leaves a node stuck 'pending', and
  // without this guard its node_created row would auto-expand the retained span projection forever.
  // A plain object {nodeId: generation} of EVERY node building right now (parallel_build>1 builds
  // several at once), so each concurrent build's feed row live-polls its own trace — not just the
  // singular
  // last-appended one. `buildings` is a node_id->marker object; fall back to the singular `building`
  // for a serial-build / old server. null when nothing is live-building (keeps the poll disabled).
  // The marker bag is bounded by parallel_build; projecting it directly is cheaper than memo state
  // and guarantees the generation fence is evaluated on every live render.
  const liveBuilding = readOnly || !atLiveView || timeline.generation !== expectedGeneration
    ? null : buildingGenerations(live)

  // Scrubber: pointer/key movement is a LOCAL preview. Commit only on pointer-up/key-up/blur so a
  // 50k-event drag cannot queue a series of expensive historical state folds on the server.
  const [drag, setDrag] = useState(null)
  const dragRef = useRef(null)
  useEffect(() => {
    if (viewSeq == null) {
      dragRef.current = null
      setDrag(null)
    }
  }, [viewSeq])
  const sliderVal = drag != null ? drag : (atLiveView ? liveSeq : viewSeq)
  const returnToLive = () => {
    dragRef.current = null
    setDrag(null)
    if (onReturnToLive) onReturnToLive()
    else { setViewSeq(null); timeline.jumpToLive() }
  }
  const commit = (v) => v >= liveSeq ? returnToLive() : setViewSeq(v)
  const onScrub = (v) => {
    dragRef.current = v
    setDrag(v)
  }
  const endScrub = () => {
    if (dragRef.current == null) return
    const value = dragRef.current
    dragRef.current = null
    commit(value); setDrag(null)
  }

  const eventDestination = (e) => {
    const nid = eventNode(e)
    if (nid == null) return { nodeId: null, sequence: e.seq,
      opensHistoricalSnapshot: e.seq < liveSeq }
    const nodeId = Number(nid)
    const currentAttempt = live?.nodes?.[nodeId]?.attempt
    const nodeGeneration = e.type === 'node_created'
      ? verifiableCreatedAttempt(e, currentAttempt) : null
    const opensLiveTrace = !readOnly && e.type === 'node_created' && atLiveView
      && timeline.generation === expectedGeneration && nodeGeneration != null
      && nodeGeneration === currentAttempt
    // Trace is a live sidecar. A replay row, or a stale attempt still present in the live feed, must
    // keep its exact historical sequence and use snapshot-safe Overview.
    const preserveExactSequence = e.type === 'node_created' && !opensLiveTrace
    return { nodeId, nodeGeneration, opensLiveTrace, preserveExactSequence,
      opensHistoricalSnapshot: preserveExactSequence || e.seq < liveSeq,
      tab: opensLiveTrace ? 'Trace' : 'Overview', sequence: opensLiveTrace ? null : e.seq }
  }
  const focusEvent = (e) => {
    const destination = eventDestination(e)
    if (destination.nodeId == null) { setViewSeq(destination.sequence); return }
    onFocus?.(destination.nodeId, destination.tab, destination.sequence,
      { nodeGeneration: destination.nodeGeneration,
        preserveExactSequence: destination.preserveExactSequence })
  }
  const eventFocusLabel = (e, destination = eventDestination(e)) => {
    if (destination.nodeId == null) {
      return destination.opensHistoricalSnapshot
        ? `Open event ${e.seq} in a read-only snapshot` : `Jump to event ${e.seq}`
    }
    if (e.type !== 'node_created') {
      return destination.opensHistoricalSnapshot
        ? `Open experiment #${destination.nodeId} from event ${e.seq} in a read-only snapshot`
        : `Open experiment #${destination.nodeId} from event ${e.seq}`
    }
    if (destination.opensLiveTrace) {
      return `Open current attempt ${destination.nodeGeneration} trace for experiment #${destination.nodeId}`
    }
    const attempt = destination.nodeGeneration == null
      ? 'recorded attempt' : `attempt ${destination.nodeGeneration}`
    return `Open ${attempt} for experiment #${destination.nodeId} at event ${e.seq} in a read-only snapshot`
  }
  const toggleKind = (g) => setKinds(s => { const n = new Set(s); n.has(g) ? n.delete(g) : n.add(g); return n })
  const searchableLog = useMemo(() => log.map(event => {
    const narration = eventNarration(event)
    // Unknown/forward-compatible event types are rendered from their projected JSON fallback. Include the
    // same bounded source in search so text the user can plainly see is not reported as "0 matching".
    // Keep the projection capped: Event Explorer owns deeper projected payload inspection, and Timeline
    // may retain 5,000 rows.
    let rawPreview = ''
    try { rawPreview = JSON.stringify(event.data ?? {}).slice(0, 500) } catch { /* cyclic/malformed data */ }
    return { event, search: `${event.type || ''} ${narration} ${rawPreview}`.toLowerCase() }
  }), [log])
  const filterQuery = filter.trim().toLowerCase()
  const kindMatch = (e) => kinds.size === 0 || kinds.has(TYPE2GROUP[e.type] || 'lifecycle')

  // The chronological feed: events, filtered + time-scrubbed.
  const feed = useMemo(() =>
    searchableLog.filter(({ event, search }) => isCuratedType(event.type)
      && (atLiveView || event.seq <= viewSeq)
      && kindMatch(event) && (!filterQuery || search.includes(filterQuery))).map(item => item.event),
    [searchableLog, atLiveView, viewSeq, filterQuery, kinds])
  // Non-curated bookkeeping (per-call cost, finalize gates, concept-cadence sidecars, …) is kept out of
  // the feed but still counted in `timeline.totalEvents`/`timeline.unread` (server counts the raw log).
  // When any such row is in the loaded window, those counts over-state what actually renders — so the
  // pagebar shows a separate "shown" figure and the unread badge falls back to the numberless "new
  // activity" affordance rather than promising a precise count the feed can't honor (allow-list desync).
  const hiddenPresent = useMemo(() => log.some((e) => !isCuratedType(e.type)), [log])
  useEffect(() => {
    if (!atLiveView && viewSeq != null) timeline.ensureSeq(viewSeq)
  }, [atLiveView, viewSeq, timeline.revision, timeline.ensureSeq])
  // Observable run truth comes from the same pure lifecycle used by the run list/header. Local command
  // state only hides duplicate controls; it never promotes a run to finished by itself.
  const lifecycle = runLifecycle(live || {})
  const mode = lifecycle.mode
  useEffect(() => {
    if (!startOverDialogIntent) return
    if (startOverDisabled || mode !== 'finished'
        || startOverDialogIntent.runId !== String(runId)
        || startOverDialogIntent.expectedGeneration !== expectedGeneration) {
      setStartOverDialogIntent(null)
      restoreDockFocus()
    }
  }, [startOverDialogIntent, startOverDisabled, mode, runId, expectedGeneration])
  const transportLabels = (action) => ({
    stop: { success: 'Stopped — frozen, not finalized', noop: 'Run was already stopped',
      executing: 'Stop requested — waiting for the run to freeze', failure: 'Stop failed' },
    finalize: { success: 'Finalized — report and wrap-up complete', noop: 'Run was already finalized',
      executing: 'Finalize requested — wrapping up', failure: 'Finalize failed' },
    resume: { success: 'Run resumed', noop: 'Run was already running',
      executing: 'Resume requested — waiting for the engine', failure: 'Resume failed' },
  })[action] || { success: 'Run command completed', noop: 'Run command was already satisfied',
    executing: 'Run command is pending', failure: 'Run command failed' }
  const persistTransport = (entry) => saveRunTransport(runId, {
    ...entry, commandId: entry?.record?.id || '',
  })
  const verifiedTransportAction = (action, record, protocolInvalid = false) => {
    const actual = protocolInvalid || !TRANSPORT_INTENTS[action]
      ? commandActionForEvent(record?.event_type) : action
    return actual && TRANSPORT_INTENTS[actual] && commandRecordMatchesAction(record, actual, 'dock')
      ? actual : null
  }
  const protocolTransportState = (action, idempotencyKey, record, message, lockIdentity = null,
    boundGeneration = transportPending?.expectedGeneration || lockIdentity?.expectedGeneration || '') => {
    const entry = { action: action || 'unknown', idempotencyKey,
      expectedGeneration: boundGeneration,
      record: protocolCommandRecord(record).record,
      statusUnavailable: true, observationKind: 'protocol', protocolInvalid: true,
      canResubmit: false, lastError: message, lockIdentity }
    saveRunCommandLock(runId, { ...entry, source: 'dock' })
    setTransportPending(entry); setTransportFailure(null)
    return entry
  }
  const storageTransportFailure = (action, idempotencyKey, boundGeneration) => {
    const record = commandStorageUnavailableRecord()
    const entry = { action, idempotencyKey, expectedGeneration: boundGeneration, record }
    setTransportPending(null); setTransportFailure(entry)
    onToast?.('Command not sent — durable recovery storage is unavailable')
    return entry
  }
  const acceptTransportRecord = (action, record, idempotencyKey, boundGeneration) => {
    const pendingState = transportPending
    const actualAction = verifiedTransportAction(action, record, pendingState?.protocolInvalid)
    if (!actualAction) return protocolTransportState(action, idempotencyKey, record,
      'Command identity does not match the requested action', pendingState?.lockIdentity,
      boundGeneration)
    if (pendingState?.protocolInvalid) {
      const identity = pendingState.lockIdentity
        || commandLockIdentity('dock', pendingState.action, pendingState)
      clearRunCommandLock(runId, identity)
    }
    const feedback = commandFeedback(record, transportLabels(actualAction))
    onToast?.(feedback.message)
    if (feedback.kind === 'pending') {
      const entry = { action: actualAction, idempotencyKey, expectedGeneration: boundGeneration,
        record, statusUnavailable: false }
      if (!persistTransport(entry)) {
        return protocolTransportState(actualAction, idempotencyKey, record,
          'Command accepted, but its updated durable status could not be stored', null,
          boundGeneration)
      }
      setTransportPending(entry)
      setTransportFailure(null)
    } else {
      setTransportPending(null)
      if (feedback.kind === 'error') {
        const entry = { action: actualAction, idempotencyKey, expectedGeneration: boundGeneration, record }
        if (!persistTransport(entry)) clearRunTransport(runId)
        setTransportFailure(entry)
      } else {
        clearRunTransport(runId); setTransportFailure(null)
      }
    }
  }
  const unavailableTransport = (action, idempotencyKey, boundGeneration, record, error, extra = {}) => {
    const kind = observeCommandError(error)
    let recoveryRecord = record || { status: 'submitting' }
    if (recoveryRecord.id && !recoveryRecord.event_type && TRANSPORT_INTENTS[action]) {
      recoveryRecord = { ...recoveryRecord, event_type: commandEventForAction(action, 'dock') }
    }
    const entry = {
      action, idempotencyKey, expectedGeneration: boundGeneration, record: recoveryRecord,
      statusUnavailable: true, observationKind: kind,
      lastError: error?.message || String(error), ...extra,
    }
    if (!persistTransport(entry)) saveRunCommandLock(runId, { ...entry, source: 'dock' })
    setTransportPending(entry); setTransportFailure(null)
    return entry
  }
  const failTransport = (action, idempotencyKey, boundGeneration, error, previous = null) => {
    const record = commandFailureRecord(error, previous)
    const entry = { action, idempotencyKey, expectedGeneration: boundGeneration, record }
    if (!persistTransport(entry)) clearRunTransport(runId)
    setTransportPending(null); setTransportFailure(entry)
    onToast?.(commandFeedback(record, transportLabels(action)).message)
  }
  const runTransport = async (action, idempotencyKey = createIdempotencyKey(), {
    allowPending = false, boundGeneration = null,
  } = {}) => {
    if (!allowPending && (transportPending || loadRunCommandLock(runId))) return
    const intent = TRANSPORT_INTENTS[action]
    if (!intent) {
      protocolTransportState(action, idempotencyKey, transportPending?.record,
        'Stored command identity cannot be safely replayed', transportPending?.lockIdentity)
      return
    }
    const generation = allowPending ? boundGeneration : expectedGeneration
    if (!/^[0-9a-f]{64}$/.test(generation || '')) {
      const error = new Error('The displayed run generation is unavailable.')
      error.code = 'run_generation_unavailable'
      error.remediation = 'Refresh the run and wait for its current state before submitting another action.'
      failTransport(action, idempotencyKey, generation || '', error)
      return
    }
    const start = { action, idempotencyKey, expectedGeneration: generation,
      record: { status: 'submitting' } }
    if (!persistTransport(start)) { storageTransportFailure(action, idempotencyKey, generation); return }
    setTransportPending(start)
    setTransportFailure(null)
    try {
      const record = await runCommand(runId, intent.type, intent.data, {
        idempotencyKey, expectedGeneration: generation, waitMs: 0,
        onRecord: next => {
          const visible = { action, idempotencyKey, expectedGeneration: generation,
            record: next, statusUnavailable: false }
          if (!persistTransport(visible)) return
          setTransportPending(current => current?.action === action && current?.idempotencyKey === idempotencyKey
            ? visible : current)
        },
      })
      acceptTransportRecord(action, record, idempotencyKey, generation)
    } catch (error) {
      const record = error?.commandRecord || (error?.commandId
        ? { id: error.commandId, status: 'accepted' } : null)
      const kind = observeCommandError(error)
      if (error?.commandUnknown || (record?.id && commandIntentPreserved(kind))) {
        unavailableTransport(action, idempotencyKey, generation, record, error)
        onToast?.(`${transportLabels(action).failure}: command status unavailable; the same intent was preserved`)
      } else failTransport(action, idempotencyKey, generation, error, record)
    }
  }
  const onStop = () => runTransport('stop')
  const onFinalize = () => runTransport('finalize')
  const onResume = () => runTransport('resume')
  const onRetryTransport = async () => {
    const failure = transportFailure
    if (transportBusy || loadRunCommandLock(runId) || !commandCanRetry(failure?.record)) return
    const { action, record, idempotencyKey, expectedGeneration: boundGeneration } = failure
    const retrying = { action, idempotencyKey, expectedGeneration: boundGeneration,
      record, retrying: true }
    if (!persistTransport(retrying)) {
      onToast?.('Retry not sent — durable recovery storage is unavailable')
      return
    }
    setTransportFailure(null)
    setTransportPending(retrying)
    try {
      const next = await retryRunCommand(runId, record.id, {
        waitMs: 0,
        onRecord: value => {
          const visible = { action, idempotencyKey, expectedGeneration: boundGeneration,
            record: value, retrying: true }
          persistTransport(visible)
          setTransportPending(current => current?.action === action && current?.idempotencyKey === idempotencyKey
            ? visible : current)
        },
      })
      acceptTransportRecord(action, next, idempotencyKey, boundGeneration)
    } catch (error) {
      const kind = observeCommandError(error)
      if (commandIntentPreserved(kind)) {
        unavailableTransport(action, idempotencyKey, boundGeneration,
          error?.commandRecord || record, error)
      } else failTransport(action, idempotencyKey, boundGeneration,
        error, error?.commandRecord || record)
    }
  }
  const onCheckTransport = async () => {
    const pending = transportPending
    if (!pending || pending.checking) return
    const checking = { ...pending, checking: true }
    if (!pending.protocolInvalid) persistTransport(checking)
    setTransportPending(checking)
    if (!pending.record?.id) {
      if (pending.protocolInvalid || pending.canResubmit === false) {
        setTransportPending({ ...pending, checking: false })
        onToast?.('Stored command identity is invalid and cannot be safely replayed; dismiss it to continue')
        return
      }
      // The POST response was lost before the command id arrived. Re-submit the exact stored key and
      // deterministic action payload; the server returns the same command record.
      await runTransport(pending.action, pending.idempotencyKey, {
        allowPending: true, boundGeneration: pending.expectedGeneration,
      })
      return
    }
    try {
      const record = await getRunCommand(runId, pending.record.id)
      acceptTransportRecord(pending.action, record, pending.idempotencyKey,
        pending.expectedGeneration)
    } catch (error) {
      const kind = observeCommandError(error)
      if (pending.protocolInvalid) {
        protocolTransportState(pending.action, pending.idempotencyKey, pending.record,
          error?.message || 'Stored command could not be verified', pending.lockIdentity,
          pending.expectedGeneration)
      } else if (commandIntentPreserved(kind)) {
        unavailableTransport(pending.action, pending.idempotencyKey, pending.expectedGeneration,
          pending.record, error)
      } else failTransport(pending.action, pending.idempotencyKey, pending.expectedGeneration,
        error, pending.record)
    }
  }
  const { action: polledAction, idempotencyKey: polledKey,
    expectedGeneration: polledGeneration } = transportPending || {}
  useCommandStatusPoll({
    runId, command: transportPending?.record,
    paused: transportPending?.statusUnavailable || transportPending?.retrying
      || transportPending?.checking,
    observe: command => getRunCommand(runId, command.id),
    onRecord: record => {
      if (COMMAND_SUCCEEDED.has(record.status) || COMMAND_FAILED.has(record.status)) {
        acceptTransportRecord(polledAction, record, polledKey, polledGeneration)
        return false
      }
      const entry = { action: polledAction, idempotencyKey: polledKey,
        expectedGeneration: polledGeneration, record, statusUnavailable: false }
      persistTransport(entry); setTransportPending(entry)
      return true
    },
    onUnobservable: (error, kind, command) =>
      unavailableTransport(polledAction, polledKey, polledGeneration, command, error),
    onFailed: (error, kind, command) =>
      failTransport(polledAction, polledKey, polledGeneration, error, command),
  })
  // A pending command that has stopped looking instantaneous owes the operator an account of itself.
  // Recomputed on the status poll's own cadence (each poll replaces `transportPending`), so the chip
  // stops being a bare label without a second timer of its own.
  const pendingRemedy = pendingCommandRemedy(transportPending?.record, transportPending?.action)
  const canRetryTransport = commandCanRetry(transportFailure?.record)
  const failedCommandId = transportFailure?.record?.id
  const conflictingCommandId = transportFailure?.record?.error?.existing_command_id
  const failureCode = transportFailure?.record?.error?.code
  const failureHeading = !transportFailure ? ''
    : failureCode === 'owner_access_required' ? 'Owner access required'
      : failureCode === 'command_protocol_error' ? 'Invalid command response'
        : transportFailure.record?.status === 'rejected' ? `${transportLabels(transportFailure.action).failure} — rejected`
          : transportFailure.action === 'finalize' && canRetryTransport ? 'Finalization stalled'
            : transportLabels(transportFailure.action).failure
  const dismissTransportFailure = () => {
    clearRunTransport(runId); setTransportFailure(null)
  }
  const dismissProtocolTransport = () => {
    const pending = transportPending
    if (!pending?.protocolInvalid) return
    clearRunTransport(runId)
    const identity = pending.lockIdentity || commandLockIdentity('dock', pending.action, pending)
    clearRunCommandLock(runId, identity)
    setTransportPending(null)
  }
  const submitStartOver = () => {
    const intent = startOverDialogIntent
    if (!intent || startOverDisabled || mode !== 'finished' || !onStartOver
        || intent.runId !== String(runId)
        || intent.expectedGeneration !== expectedGeneration) {
      closeStartOverDialog()
      onToast?.('Start over was not submitted because the run changed before confirmation.')
      return
    }
    closeStartOverDialog()
    onStartOver(intent.expectedGeneration)
    // Confirmation immediately removes the trigger as the run enters recovery. The dialog hook can
    // restore only to a still-connected node, so provide a stable Dock/workspace fallback.
    restoreDockFocus()
  }
  const startOverLabel = String(
    startOverDialogIntent?.label || live?.label || live?.run_id || runId)
  const startOverIdentity = startOverLabel === String(runId)
    ? <code>{runId}</code>
    : <><b>“{startOverLabel}”</b> <span className="muted">(run <code>{runId}</code>)</span></>
  // Publish only from the committed layout and bind the callable to this exact run generation. A
  // functional identity cleanup prevents an old StrictMode/unmount cleanup from erasing a newer
  // controller. The parent also receives busy/failure reactively, so a prominent canvas recovery CTA
  // cannot look enabled while Dock is preserving or observing a command.
  useLayoutEffect(() => {
    if (!publishTransport || readOnly) return undefined
    const controller = Object.freeze({
      runId, expectedGeneration, busy: transportBusy,
      pendingAction: transportPending?.action || externalTransportPending?.action || null,
      failure: !!transportFailure,
      invoke: action => {
        if (action !== 'resume' && action !== 'finalize') return undefined
        return runTransport(action)
      },
    })
    publishTransport(controller)
    return () => publishTransport(current => current === controller ? null : current)
  }, [publishTransport, readOnly, runId, expectedGeneration, transportBusy,
    transportPending?.action, externalTransportPending?.action, !!transportFailure])
  return (
    <div className="dock chat-dock">
      <div className="dock-tabs">
        <span className="chat-label"><OpIcon name="flag" size={14} /> events &amp; timeline</span>
        {/* clickable so the user can return to live even when the controls (with the Live button) are hidden */}
        <button type="button" className={'hist-tag-mini ' + (visiblyLive ? 'live' : 'hist')}
              onClick={returnToLive} disabled={visiblyLive}
              title={visiblyLive ? '' : 'Jump to latest verified event'}>
          {atLiveView
            ? visiblyLive ? `live · ${liveSeq}` : 'reading · jump latest'
            : `replay ${sliderVal}/${liveSeq} → live`}</button>
        <span className="spacer" />
        <button className={'btn sm ghost' + (!collapsed && showControls ? ' on' : '')
                  + (filtersActive ? ' filters-active' : '')}
                title={filtersActive ? 'Timeline filters — active filters applied' : 'Timeline filters'}
                aria-label={filtersActive ? 'Timeline filters, active filters applied' : 'Timeline filters'}
                aria-expanded={!collapsed && showControls} aria-controls="run-timeline-controls"
                onClick={toggleControls}><OpIcon name="sliders" size={13} /> controls</button>
        <button ref={setCollapseButtonRef} className="btn sm ghost dock-collapse" title={collapsed ? 'expand' : 'collapse'}
                aria-label={collapsed ? 'Expand events and timeline' : 'Collapse events and timeline'}
                aria-expanded={!collapsed} aria-controls="run-events-timeline"
                onClick={onToggleCollapse}><OpIcon name={collapsed ? 'chevron-up' : 'chevron-down'} size={13} /></button>
      </div>
      {!collapsed && <div id="run-events-timeline" className="dock-body chat-body" style={{ height }}>
        {showControls && <div id="run-timeline-controls" className="dock-controls">
          <div className="scrubber inline">
            <button className="btn sm" onClick={returnToLive} disabled={drag == null && visiblyLive}><OpIcon name="play" size={11} /> Live</button>
            <input type="range" min={0} max={Math.max(0, liveSeq)} value={sliderVal}
                   aria-label="Timeline sequence" aria-valuetext={sliderVal >= liveSeq ? `live at ${liveSeq}` : `replay ${sliderVal} of ${liveSeq}`}
                   onChange={e => onScrub(Number(e.target.value))}
                   onPointerUp={endScrub} onMouseUp={endScrub} onKeyUp={endScrub} onBlur={endScrub} />
            <span className={(sliderVal >= liveSeq) ? 'live-tag' : 'hist-tag'}>{(sliderVal >= liveSeq) ? `live · ${liveSeq}` : `replay · ${sliderVal}/${liveSeq}`}</span>
          </div>
          <div className="kind-chips">
            <input className="text feed-filter" aria-label="Filter loaded events" placeholder="filter events…" value={filter} onChange={e => onFilterChange?.(e.target.value)} />
            <div className="kind-chip-strip">
              {GROUPS.map(([g, label]) => <button key={g}
                className={'kind-chip k-' + g + (kinds.has(g) ? ' on' : '')} aria-pressed={kinds.has(g)}
                onClick={() => toggleKind(g)}>
                <OpIcon name={GROUP_GLYPH[g]} size={12} /> {label}</button>)}
              {kinds.size > 0 && <button className="kind-chip clear" onClick={() => setKinds(new Set())}>clear</button>}
            </div>
          </div>
        </div>}
        <div className="timeline-pagebar">
          <button type="button" className="btn sm ghost" disabled={!timeline.hasMore.older || timeline.loading.older}
            onClick={timeline.loadOlder}>{timeline.loading.older ? 'Loading…' : 'Load older'}</button>
          <span className="muted">
            {timeline.totalEvents != null ? `${log.length} loaded of ${timeline.totalEvents}` : `${log.length} loaded`}
            {feed.length !== log.length
              ? ` · ${feed.length} ${(filter.trim() || kinds.size) ? 'matching' : 'shown'}`
              : ''}
          </span>
          {timeline.hasMore.newer && <button type="button" className="btn sm ghost"
            disabled={timeline.loading.newer} onClick={timeline.loadNewer}>
            {timeline.loading.newer ? 'Loading…' : 'Load newer'}</button>}
        </div>
        {(filter.trim() !== '' || kinds.size > 0) && timeline.totalEvents != null && timeline.totalEvents > log.length &&
          <div className="timeline-window-note" role="note">Filters search loaded events only; page for more.</div>}
        {timeline.status === 'loading' && log.length === 0 && <div className="timeline-resource muted" role="status">Loading timeline…</div>}
        {timeline.loading.around && <div className="timeline-resource muted" role="status">Loading around seq {viewSeq}…</div>}
        {timeline.errors.tail && <div className="notice resource-error" role="alert">
          <span>{log.length ? 'Refresh failed; window unchanged.' : timeline.errors.tail}</span>
          <button className="btn sm" onClick={() => timeline.retry('tail')}>Retry</button></div>}
        {timeline.errors.older && <div className="notice resource-error compact" role="alert">
          <span>Older events unavailable.</span><button className="btn sm" onClick={timeline.loadOlder}>Retry</button></div>}
        {timeline.errors.newer && <div className="notice resource-error compact" role="alert">
          <span>Live refresh failed; events may lag.</span><button className="btn sm" onClick={timeline.loadNewer}>Retry</button></div>}
        {timeline.errors.around && <div className="notice resource-error compact" role="alert">
          <span>Replay seq {viewSeq} unavailable.</span>
          <button className="btn sm" onClick={() => timeline.retry('around')}>Retry</button></div>}
        {timeline.tornTail && <div className="timeline-window-note warning" role="status">
          {timeline.sourceTailLimited
            ? 'The raw log tail exceeds the safety limit; showing the last verified canonical prefix.'
            : 'The final source row is incomplete or non-canonical; showing the last verified event prefix.'}
        </div>}
        {feed.length === 0 && timeline.status === 'ready' && !timeline.loading.around
          ? <div className="timeline-resource muted">{(filter.trim() || kinds.size)
              ? 'nothing matches the loaded window'
              : log.length > 0 ? 'only background bookkeeping in this window — page older for run events' : 'no events yet'}</div>
          : <VirtualTimeline rows={feed} getKey={timelineEventKey}
              identity={`${runId}:${timeline.generation || 'pending'}`}
              className="feed chat-feed" ariaLabel="Run events"
              followingTail={atLiveView && timeline.followingTail}
              windowAtTail={atLiveView && timeline.windowAtTail}
              unread={atLiveView && !hiddenPresent ? timeline.unread : 0}
              unreadUnknown={atLiveView && (timeline.unreadUnknown || (hiddenPresent && timeline.unread > 0))}
              busy={Object.values(timeline.loading).some(Boolean)}
              onFollowingTailChange={value => { if (atLiveView) timeline.setFollowingTail(value) }}
              onJumpToLive={returnToLive}
              renderRow={event => {
                const key = timelineEventKey(event)
                const destination = eventDestination(event)
                return <EventRow e={event} onFocusEvent={focusEvent}
                  focusLabel={eventFocusLabel(event, destination)}
                  nodeCreatedAttempt={destination.nodeGeneration} runId={runId}
                  runGeneration={timeline.generation}
                  readOnly={readOnly} liveBuilding={liveBuilding} autoOpen={false}
                  expansion={eventExpansion.get(key) || CLOSED_EXPANSION}
                  onExpansionChange={next => setEventExpansion(current => {
                    const updated = new Map(current); updated.set(key, next); return updated
                  })} />
              }} />}
        {!showControls && (() => {
          const pipeline = atLiveView ? agentStatus(live, log) : null
          if (!pipeline) return null
          // HOW LONG. The label named the phase but never its age, so a build silent for forty
          // minutes looked exactly like one that started two seconds ago — the operator's "it hangs
          // for a very long time with no logs". Suppressed under 20 s, where a ticking number is
          // churn rather than information.
          const age = liveStatusAgeLabel(live, log)
          return <div className="agent-status dock-agent-status">
            <div className="as-line"><span className="as-dot" /><span className="as-seg">{pipeline}</span>
              {age && <span className="muted as-age" title="how long this phase has been running">
                {age}</span>}</div>
            <LiveTrace runId={runId} generation={timeline.generation} active={atLiveView} />
          </div>
        })()}
        <div className="dock-foot">
          <span className="muted dock-foot-hint">
            {readOnly ? 'Historical timeline — live controls and sidecar trace details are disabled.' : <>
              Steer below by chat or <code className="cmd-hint">/stop · /finalize · /resume · /approve #id</code>.
            </>}
          </span>
          {!readOnly && <div className="transport">
            {transportPending && <div className={'transport-message' + (transportPending.statusUnavailable ? ' warning' : '')}
              role={transportPending.statusUnavailable ? 'alert' : 'status'}>
              <span>
                {transportPending.statusUnavailable
                  ? transportPending.observationKind === 'access' ? 'Owner access required to check command status'
                    : transportPending.observationKind === 'protocol' ? 'Invalid command status response'
                      : 'Command status unavailable — the same intent is preserved'
                  : transportPending.checking ? 'Checking the same command…'
                    : transportPending.retrying ? 'Retrying the same command…'
                      : transportPending.record?.status === 'submitting'
                        ? `Submitting ${transportPending.action}…`
                        : transportPending.action === 'finalize' ? 'Finalizing…'
                          : transportPending.action === 'stop' ? 'Stop requested…' : 'Resume requested…'}
                {transportPending.record?.id
                  ? <span className="transport-command-id" title={`Command ${transportPending.record.id}`}>
                      {' · '}{String(transportPending.record.id).slice(0, 12)}…</span> : null}
              </span>
              {transportPending.statusUnavailable && <>
                <span className="transport-detail">{transportPending.observationKind === 'access'
                  ? 'Verify owner access, then check again.'
                  : transportPending.observationKind === 'protocol'
                    ? 'Saved command response unverifiable.'
                    : 'Reconnect and check before another action.'}</span>
                <button className="btn sm" onClick={onCheckTransport}
                  aria-label={`Check preserved ${transportPending.action} command`}>
                  Check command</button>
                {transportPending.protocolInvalid && <button className="btn sm ghost"
                  onClick={dismissProtocolTransport}>Dismiss</button>}
              </>}
              {!transportPending.statusUnavailable && pendingRemedy && <>
                <span className="transport-detail">
                  {`Waiting ${Math.round(pendingRemedy.elapsedMs / 1000)}s. ${pendingRemedy.waitingFor} `}
                  {pendingRemedy.boundedMs == null
                    ? 'This server does not report when the command stops waiting.'
                    : `It stops waiting on its own in about ${Math.max(1, Math.round(pendingRemedy.boundedMs / 1000))}s, and then says why.`}
                </span>
                {pendingRemedy.canCheck && <button className="btn sm ghost" onClick={onCheckTransport}
                  aria-label={`Check the pending ${transportPending.action} command now`}>
                  Check now</button>}
              </>}
            </div>}
            {!transportPending && externalTransportPending && <div className="transport-message" role="status"
              aria-live="polite" aria-atomic="true">
              <span>/{externalTransportPending.action} is pending in Assistant</span>
              {externalTransportPending.commandId && <span className="transport-command-id"
                title={`Command ${externalTransportPending.commandId}`}>
                {' · '}{String(externalTransportPending.commandId).slice(0, 12)}…</span>}
            </div>}
            {!transportBusy && transportFailure && <>
              <div className="transport-message error" role="alert">
                <span>{failureHeading}{failedCommandId
                  ? <span className="transport-command-id" title={`Command ${failedCommandId}`}>
                      {' · '}{String(failedCommandId).slice(0, 12)}…</span> : null}
                  {!failedCommandId && conflictingCommandId
                    ? <span className="transport-command-id" title={`Conflicting active command ${conflictingCommandId}`}>
                        {' · active '}{String(conflictingCommandId).slice(0, 12)}…</span> : null}</span>
                <span className="transport-detail">{commandErrorMessage(transportFailure.record)}</span>
              </div>
              {canRetryTransport && <button className="btn sm" onClick={onRetryTransport}
                title="Retry the same durable command">Retry command</button>}
              <button className="btn sm ghost" onClick={dismissTransportFailure}
                title="Dismiss result">Dismiss</button>
            </>}
            {!transportBusy && !transportFailure && mode === 'finalizing' && <span className="muted" role="status">Finalizing…</span>}
            {!transportBusy && !transportFailure && mode === 'finishing' && <span className="muted" role="status">Finishing write-out…</span>}
            {!transportBusy && !transportFailure && mode === 'finalization-stalled' && <>
              <span className="muted" role="alert">Finalization stalled</span>
              <button className="btn sm" onClick={onFinalize}
                title="Resume pending finalization">Reattach finalization</button>
            </>}
            {!transportBusy && !transportFailure && mode === 'running' && <>
              <button className="btn sm" aria-label="Stop run without finalizing"
                title="Stop now; resume or finalize later" onClick={onStop}><OpIcon name="pause" size={13} /></button>
              <button className="btn sm danger" aria-label="Finalize run"
                title="Finalize: stop, report, lessons and cost" onClick={onFinalize}><OpIcon name="stop" size={13} /></button></>}
            {!transportBusy && !transportFailure && (mode === 'paused' || mode === 'stalled') && <>
              <button className="btn sm primary" aria-label="Resume run" title="Continue run" onClick={onResume}><OpIcon name="play" size={13} /></button>
              <button className="btn sm danger" aria-label="Finalize run"
                title="Finalize: stop, report, lessons and cost" onClick={onFinalize}><OpIcon name="stop" size={13} /></button></>}
            {!transportBusy && !transportFailure && mode === 'finished' && <>
              <button className="btn sm primary" aria-label="Resume finished run"
                title={runActionBusy ? 'Another run lifecycle action must be resolved first' : 'Reopen and continue'}
                disabled={runActionBusy} onClick={onResume}><OpIcon name="play" size={13} /></button>
              <button className="btn sm danger start-over-trigger" aria-haspopup="dialog"
                aria-label="Start run over" title={startOverDisabled
                  ? startOverState?.disabledReason || 'Resolve the existing Start over outcome first'
                  : 'Archive this generation and start again from the saved task and settings'}
                disabled={startOverDisabled} onClick={() => setStartOverDialogIntent({
                  runId: String(runId),
                  expectedGeneration: String(expectedGeneration || ''),
                  label: String(live?.label || live?.run_id || runId),
                })}>
                <OpIcon name="replay" size={13} /> Start over…</button></>}
          </div>}
        </div>
      </div>}
      {startOverDialogIntent && <div className="overlay start-over-overlay"
        onMouseDown={event => { if (event.target === event.currentTarget) closeStartOverDialog() }}>
        <section ref={startOverDialogRef} className="modal start-over-dialog" role="alertdialog"
          aria-modal="true" aria-labelledby="start-over-title"
          aria-describedby="start-over-description start-over-cost-note"
          tabIndex={-1}>
          <div className="modal-h">
            <b id="start-over-title">Start this run over?</b>
          </div>
          <div className="modal-b">
            <p id="start-over-description" className="start-over-copy">
              {startOverIdentity} will start again from its saved task and settings. Current events,
              nodes, traces, and chat leave the live view and remain archived on disk.
            </p>
            <p id="start-over-cost-note" className="start-over-cost-note">
              The engine starts immediately and may use provider and evaluation budget.
            </p>
            <div className="modal-actions">
              <button type="button" className="btn sm" data-dialog-initial-focus
                onClick={closeStartOverDialog}>Keep current run</button>
              <button type="button" className="btn sm danger"
                disabled={startOverDisabled || mode !== 'finished'
                  || startOverDialogIntent.expectedGeneration !== expectedGeneration}
                onClick={submitStartOver}>Archive &amp; start over</button>
            </div>
          </div>
        </section>
      </div>}
    </div>
  )
}
