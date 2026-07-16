const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0

export const traceUnavailable = p => p?.unavailable === true

// Never trust projection counters to hide visible data: reconcile malformed totals upward and derive
// omissions from both the server envelope and the client's own emergency render cap.
// REVIEW(2026-07-16): `truncated === true` here conflates two different server signals. span_io
// (serve/routers/runs.py) sets truncated=True whenever trace_total > visible, and for every
// NON-hydrated span (all tool spans; generations without input_carry) visible is hardwired to 1 —
// i.e. "this span has siblings", not "this span's detail was cut". Expanding ANY tool observation in
// a trace with >= 2 spans therefore shows "Trace detail truncated." even though its input/output was
// fully retained, and real truncation (the span's own projection budget) becomes indistinguishable
// from the constant false positive — defeating the omission-receipt design. The server needs to
// split the flag (siblings_elided vs detail_truncated) or the client must not surface this one.
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
    partial: tracePartial(projection),
  }
}

// Transport failure is not evidence that a span recorded no I/O. Keep this state distinct from a
// successful empty projection and never carry network/provider text into UI state.
export const unavailableTraceDetail = () => ({ status: 'unavailable', attributes: {}, partial: false })
