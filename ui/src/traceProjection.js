const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0

// Never trust projection counters to hide visible data: reconcile malformed totals upward and derive
// omissions from both the server envelope and the client's own emergency render cap.
export const tracePartial = p => p?.truncated === true || Math.max(
  count(p?.omitted_spans), count(p?.total_spans) - count(p?.visible_spans)) > 0

const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}

export const traceDetailState = detail => ({
  status: 'ready',
  attributes: record(detail?.attributes),
  partial: tracePartial(detail?.projection),
})

// CODEX AGENT: Transport failure is not evidence that a span recorded no I/O. Keep this state
// distinct from a successful empty projection and never carry raw network/provider text into UI state.
export const unavailableTraceDetail = () => ({ status: 'unavailable', attributes: {}, partial: false })
