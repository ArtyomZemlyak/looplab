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
