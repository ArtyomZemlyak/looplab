// WHAT A TRACE SURFACE IS SHOWING — the pure half of the ONE trace surface (`Inspector.jsx`'s
// `TraceSurface`, which the node Trace tab, the card's Trace tab and the research disclosure all
// render).
//
// Why this exists. The node Inspector's Trace tab grew the whole reading apparatus — a
// conversation/span-tree switcher over ONE shared span window, in-tree search, the scroll-to-reach
// affordance for earlier steps, the honest partial receipts. The card then got a SECOND, poorer
// trace surface: rows that opened `/trace/by_trace/{tid}` span trees and nothing else. That was not
// merely thinner, it was WRONG about the Developer: a node's `node_created.trace_id` names the trace
// the node was AUTHORED in, which on the shipped corpus is two spans (`Author node` →
// `materialize_node`), while the node's actual build+eval trace lives across several traces and is
// only reachable per NODE. Measured 2026-08-12: `runs/rubertlite-dr-unified-v5` card-0 node 0 —
// 6 spans by node, 2 by creation trace; `vec-backups/rubertlite-dr-unified-v3.deleted-2026-08-12`
// card-0 node 0 — 61 by node, 2 by creation trace. The card printed the NODE's rollup ("1 gen ·
// 1 tools · 2,286 tok") above a tree that could not contain it, which is exactly how the operator
// noticed the Developer trace had disappeared.
//
// So a surface no longer decides how to read a trace: it is handed a SUBJECT, and every decision
// that differs between subjects is stated once, here.

export const TRACE_VIEW_CONVERSATION = 'conversation'
export const TRACE_VIEW_SPANS = 'raw'

// Both views for both subjects. That IS the operator's request (5): the research trace rendered as a
// span tree with no way to switch, in both places it appears. A subject-dependent view list would be
// the same defect wearing a table.
export const TRACE_SURFACE_VIEWS = Object.freeze([TRACE_VIEW_CONVERSATION, TRACE_VIEW_SPANS])

const safeInt = value => (Number.isSafeInteger(value) && value >= 0 ? value : null)

/**
 * ONE node's whole trace — build, eval, repairs — across every trace it ran in.
 *
 * `attempt` is the generation to read, or `null` for "whichever is current". Null is not a default
 * of zero: the routes settle an absent `attempt` to the node's current generation themselves, and a
 * caller that knows only the node id (the card board's section rows) must not assert attempt 0 —
 * that would 409 every repaired node.
 */
export const nodeTraceSubject = (nodeId, attempt = null) => ({
  kind: 'node',
  nodeId: nodeId == null ? null : String(nodeId),
  attempt: safeInt(attempt),
})

/** ONE operation's own trace, by trace id — a proposal, a strategy decision, a merge. */
export const opTraceSubject = traceId => ({
  kind: 'trace',
  traceId: traceId == null ? '' : String(traceId),
})

export const traceSubjectValid = subject => (subject?.kind === 'node'
  ? subject.nodeId != null && subject.nodeId !== ''
  : subject?.kind === 'trace' && !!subject.traceId)

/**
 * Stable identity for React keys, poll scopes and evidence lifecycles. Includes the attempt, because
 * two generations of one node are two different readings; `*` records "current, unpinned" as the
 * distinct thing it is rather than borrowing generation 0's key.
 */
export const traceSubjectKey = subject => (subject?.kind === 'node'
  ? `node:${subject.nodeId}:${subject.attempt == null ? '*' : subject.attempt}`
  : subject?.kind === 'trace' ? `trace:${subject.traceId}` : 'none')

/** The query `attempt` a read for this subject carries — `null` means "do not send one". */
export const traceSubjectAttempt = subject => (subject?.kind === 'node' ? subject.attempt : null)

/** Only a node has subprocess logs; a proposal's trace has no sandbox and no stage log. */
export const traceSubjectHasLogs = subject => subject?.kind === 'node'

/**
 * The path SUFFIX (under `/api/runs/{run}`) this subject's view reads from. Deliberately not a full
 * URL: the run id boundary belongs to `apiClient.js::runApiPath`, and the query belongs to
 * `api.js::traceReadQuery` — one builder each, so a surface cannot invent a third spelling of either.
 */
export const traceRequestPath = (subject, view) => {
  const conversation = view === TRACE_VIEW_CONVERSATION
  if (subject?.kind === 'node') {
    return `/nodes/${encodeURIComponent(String(subject.nodeId))}`
      + (conversation ? '/conversation' : '/trace')
  }
  if (subject?.kind === 'trace') {
    return `/trace/by_trace/${encodeURIComponent(String(subject.traceId))}`
      + (conversation ? '/conversation' : '')
  }
  return ''
}

/**
 * THE FENCE: may this payload render for this subject?
 *
 * A response that arrives for another node/attempt/trace is a stale in-flight read from the previous
 * scope, never this subject's trace — the Inspector has fenced its node reads this way all along and
 * the card surface had no fence at all. `attempt: null` accepts whatever generation the server
 * settled to, because that is precisely what was asked for; it still fences the NODE.
 */
export const traceSubjectMatches = (subject, payload) => {
  if (!payload || typeof payload !== 'object') return false
  if (subject?.kind === 'node') {
    if (String(payload.node_id) !== String(subject.nodeId)) return false
    return subject.attempt == null || payload.attempt === subject.attempt
  }
  if (subject?.kind === 'trace') return String(payload.trace_id || '') === String(subject.traceId)
  return false
}

/**
 * Where the span forest lives in a settled payload. `/nodes/{n}/trace` answers with `nodes` (that
 * node's roots) and `/trace/by_trace/{tid}` with `spans`; the node-DETAIL payload also uses `nodes`.
 */
export const traceSubjectSpans = (subject, payload) => {
  const spans = subject?.kind === 'trace' ? payload?.spans : payload?.nodes
  return Array.isArray(spans) ? spans : []
}

/**
 * The lead of the span tree's caption. It names the SUBJECT, because the same tree now renders in
 * three places and "Node #7 lifecycle" under a proposal would be a lie about whose work it is.
 */
export const traceSubjectLead = subject => (subject?.kind === 'node'
  ? `Experiment #${subject.nodeId} lifecycle`
  : 'Proposal lifecycle')

/** What an empty-but-successful read means, in the subject's own words. */
export const traceSubjectEmptyNotice = subject => (subject?.kind === 'node'
  ? 'No execution spans yet. Offline nodes may have none; active nodes update here as they run.'
  : 'No observations were recorded for this operation.')
