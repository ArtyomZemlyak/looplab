const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0

export const traceUnavailable = p => p?.unavailable === true

// CODEX AGENT: This aggregate helper belongs to multi-span tree/tail envelopes. Never trust projection
// counters to hide visible data: reconcile malformed totals upward and include emergency render caps.
export const tracePartial = p => p?.truncated === true || Math.max(
  count(p?.omitted_spans), count(p?.total_spans) - count(p?.visible_spans)) > 0

const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}

export const traceDetailState = detail => {
  const projection = record(detail?.projection)
  // CODEX AGENT: An HTTP 200 unavailable receipt is still a failed observation, never proof that
  // the span had empty I/O. Unavailable therefore takes precedence over every empty/partial shape.
  if (traceUnavailable(projection)) return unavailableTraceDetail()
  return {
    status: 'ready',
    attributes: record(detail?.attributes),
    // CODEX AGENT: Elided siblings make the trace envelope partial, not the selected span's I/O.
    // Only the server's pre-cardinality receipt may drive the detail-truncated notice.
    partial: projection.detail_truncated === true,
  }
}

// Transport failure is not evidence that a span recorded no I/O. Keep this state distinct from a
// successful empty projection and never carry network/provider text into UI state.
export const unavailableTraceDetail = () => ({ status: 'unavailable', attributes: {}, partial: false })
