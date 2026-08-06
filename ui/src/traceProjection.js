const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0

export const traceUnavailable = p => p?.unavailable === true

// This aggregate helper belongs to multi-span tree/tail envelopes. Never trust projection
// counters to hide visible data: reconcile malformed totals upward and include emergency render caps.
export const tracePartial = p => p?.truncated === true || Math.max(
  count(p?.omitted_spans), count(p?.total_spans) - count(p?.visible_spans)) > 0

// The server's per-node span window (mirrors events/traceview.py TRACE_NODE_SPAN_CAP and
// TRACE_NODE_SPAN_CAP_MAX). Shared by BOTH node-trace surfaces — the timeline row and the node
// Inspector — because the two drifting apart is exactly how one of them ended up with a pager and
// the other with a dead notice over the same route.
export const NODE_TRACE_SPAN_WINDOW = 512
export const NODE_TRACE_SPAN_WINDOW_MAX = 4096

// The doubling step both node surfaces page by. Kept here rather than at the two call sites so the
// Dock and the Inspector cannot end up climbing to the ceiling at different rates — which is how one
// of them would reach a window the other silently cannot.
export const nextNodeSpanWindow = current => Math.min(
  (Number.isSafeInteger(current) && current > 0 ? current : NODE_TRACE_SPAN_WINDOW) * 2,
  NODE_TRACE_SPAN_WINDOW_MAX)

export const TRACE_PARTIAL_NOTICE = 'Trace projection is partial.'
export const TRACE_PARTIAL_EMPTY_NOTICE
  = 'Trace projection is partial; no observations were included.'

// `projection.truncated` is a UNION of two unrelated facts and a pager driven off it is wrong in
// BOTH directions:
//   * spans were OMITTED from this window — a bigger `limit` genuinely surfaces them;
//   * per-span attribute text was CLAMPED (`truncated_spans`) — no limit can ever change that; the
//     full I/O lives behind /spans/{sid} and the span-detail view has its own truncation notice.
// Measured against the live server: 8 of the 13 real node traces in rubert-dr-0805 and
// rubertlite-dr-unified-v4 report truncated=true with omitted_spans=0 — every span already on
// screen, so "load more" could not add a row — while rubert-dr-0804 node 1 omits 13,995 of 14,507.
// Returns the omitted count, or null when the payload states none (assume spans remain: fail safe).
export const spansOmitted = projection => {
  const stated = projection?.omitted_spans
  const derived = count(projection?.total_spans) - count(projection?.visible_spans)
  // Reconcile UPWARD only, never downward: a malformed counter may not hide spans the totals prove
  // are gone. This is the same rule `tracePartial` applies, kept here so both read one definition.
  if (Number.isSafeInteger(stated) && stated >= 0) return Math.max(stated, derived, 0)
  if (derived > 0) return derived
  return projection?.truncated === true ? null : 0
}

// The ONE rule for what a bounded span window owes the operator. Every trace surface routes its
// partial handling through this, so a surface cannot silently regress into a dead notice by
// forgetting to wire its pager — with no pager the rule still states the remainder.
//   complete → nothing is missing; say nothing (attribute clamping is reported per span, not here)
//   pageable → spans are missing AND reachable from here → an ACTIONABLE control
//   capped   → spans are missing and NOT reachable from here → state how many, never an adjective
// `canPage` means "this surface has somewhere to go": a raise-the-window callback that is not
// already at NODE_TRACE_SPAN_WINDOW_MAX.
export const traceWindow = (projection, { canPage = false } = {}) => {
  const omitted = spansOmitted(projection)
  const visible = count(projection?.visible_spans)
  const total = count(projection?.total_spans)
  if (omitted === 0) return { kind: 'complete', omitted: 0, visible, total }
  return { kind: canPage ? 'pageable' : 'capped', omitted, visible, total }
}

// An operator whose pager just vanished under them needs the COUNT, not an adjective — the same
// lesson the attention feed's stale-cursor message learned. The bare notice survives only for a
// payload that states no usable numbers, where a count would be invented rather than reported.
export const traceWindowNotice = spanWindow =>
  (spanWindow.omitted == null || spanWindow.total <= 0 || spanWindow.visible <= 0
    ? TRACE_PARTIAL_NOTICE
    : `Showing ${spanWindow.visible} of ${spanWindow.total} spans; `
      + `${spanWindow.omitted} more are not displayed.`)

const stated = value => (Number.isSafeInteger(value) && value >= 0 ? value : null)

// The CONVERSATION's window rule, deliberately not `traceWindow`. The two receipts share a field name
// and mean different things, and reading the conversation through the span rule is wrong twice over.
// Measured on the live server (runs/rubert-dr-0804 node 1): the conversation reported 13,995 omitted
// SPANS, while what the operator could not read was 192 omitted STAGES / 320 omitted TURNS — and
// every one of those was already derivable from the 512 spans the response HAD. So the span count
// names a quantity the reader cannot see and does not care about, and the span cap is not what hides
// their steps.
//   * a control driven off span omission is right that "something is hidden" and wrong about what;
//   * a notice quoting 13,995 invites the operator to expect 13,995 more steps.
// Both hidden counts move under the SAME `limit`, because the server derives its stage/turn caps from
// the span window (traceview.conversation_render_caps) — so one control still raises all of it.
// `totalsArePartial`: when spans are also omitted, the stage/turn TOTALS were themselves computed
// over the windowed spans, so they are a floor, not the node's true count. Saying "of 425" flatly
// would understate the run; the notice says "at least".
export const conversationWindow = (projection, { canPage = false } = {}) => {
  const omittedStages = stated(projection?.omitted_stages)
  const omittedTurns = stated(projection?.omitted_turns)
  const visibleTurns = count(projection?.visible_turns)
  const totalTurns = count(projection?.total_turns)
  const omittedSpans = spansOmitted(projection)
  const base = {
    omittedStages: omittedStages || 0,
    omittedTurns: omittedTurns || 0,
    visibleTurns,
    totalTurns,
    totalsArePartial: omittedSpans == null || omittedSpans > 0,
  }
  // A payload stating NEITHER conversation counter predates them; defer to the span receipt rather
  // than reading two absent fields as proof that nothing is hidden.
  const hidden = (omittedStages == null && omittedTurns == null)
    ? omittedSpans !== 0
    : base.omittedStages > 0 || base.omittedTurns > 0
  if (!hidden) return { kind: 'complete', ...base }
  return { kind: canPage ? 'pageable' : 'capped', ...base }
}

// Steps, because "steps" is what the collapsed bands already count for the operator. Never spans:
// they asked why 50 steps were hidden, and a number about observations does not answer that.
export const conversationWindowNotice = conversationView =>
  (conversationView.visibleTurns <= 0 || conversationView.totalTurns <= 0
    ? TRACE_PARTIAL_NOTICE
    : `Showing the most recent ${conversationView.visibleTurns} of `
      + `${conversationView.totalsArePartial ? 'at least ' : ''}${conversationView.totalTurns} steps.`)

const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}

export const traceDetailState = detail => {
  const projection = record(detail?.projection)
  // An HTTP 200 unavailable receipt is still a failed observation, never proof that
  // the span had empty I/O. Unavailable therefore takes precedence over every empty/partial shape.
  if (traceUnavailable(projection)) return unavailableTraceDetail()
  return {
    status: 'ready',
    attributes: record(detail?.attributes),
    // Elided siblings make the trace envelope partial, not the selected span's I/O.
    // Only the server's pre-cardinality receipt may drive the detail-truncated notice.
    partial: projection.detail_truncated === true,
  }
}

// Transport failure is not evidence that a span recorded no I/O. Keep this state distinct from a
// successful empty projection and never carry network/provider text into UI state.
export const unavailableTraceDetail = () => ({ status: 'unavailable', attributes: {}, partial: false })
