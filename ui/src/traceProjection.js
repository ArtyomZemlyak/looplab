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
// names a quantity the reader cannot see and does not care about.
//   * a control driven off span omission is right that "something is hidden" and wrong about what;
//   * a notice quoting 13,995 invites the operator to expect 13,995 more steps.
// Since 2026-08-13 the render caps ARE the span window's own bound (traceview.conversation_render_
// caps), so that particular 192/320 can no longer happen: what the window read is rendered. The rule
// below is unchanged and still load-bearing — a stage/turn omission is now proof that the SPAN window
// is what withholds, which is exactly what makes both remedies honest: widen it (`onLoadMore`) or
// move it (the episode control). Both hidden counts still move under the same window.
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
    // Carried as STATED — `null` when the payload does not say, never `0`. `|| 0` here made a
    // legacy/partial projection carrying `omitted_turns: 320` and no `omitted_stages` key report
    // "0 earlier stages" as a measured fact, which is the same absent-is-not-zero defect the
    // `stated()` helper exists to prevent one line above.
    omittedStages,
    omittedTurns,
    visibleTurns,
    totalTurns,
    totalsArePartial: omittedSpans == null || omittedSpans > 0,
  }
  // A payload stating NEITHER conversation counter predates them; defer to the span receipt rather
  // than reading two absent fields as proof that nothing is hidden.
  const hidden = (omittedStages == null && omittedTurns == null)
    ? omittedSpans !== 0
    : (omittedStages || 0) > 0 || (omittedTurns || 0) > 0
  if (!hidden) return { kind: 'complete', ...base }
  return { kind: canPage ? 'pageable' : 'capped', ...base }
}

// (`conversationPagerLabel` lived here until 2026-08-07. It named what a "load more" button offered,
// and the button is gone — earlier steps now arrive by scrolling, so nothing has an occasion to name
// a count the operator is about to be given anyway. The rule it existed to hold is not lost: it is
// the `stated()` treatment in `conversationWindow` above, which is what stopped the label rendering
// "(0 earlier stages not shown)" from an absent counter.)

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

// EARLIER ATTEMPTS OF A NODE. A repaired node has several generations, and until now only the last
// one was reachable: the routes have taken `?attempt=` all along, the Inspector just always sent the
// current number and REJECTED any response carrying an older one as stale. So the trace of the
// attempt that actually crashed — the one an operator opens the trace to read — was unreachable.
//
// Attempts are `0..current`, derivable with no extra request: `node.attempt` is the current lifecycle
// generation and generations are dense (each `node_reset` bumps it by one).
//
// NOT each inline repair, which this comment claimed until 2026-08-13 and which made this picker look
// like the answer to the whole of F6. `core/models.py::Node.attempt` is bumped by `node_reset` only;
// `node_repaired.attempt` is a separate, pre-existing INLINE-REPAIR ordinal that never reaches here.
// The difference is not academic: `runs/rubert-dr-0804` node 1 was repaired 2,345 times inside ONE
// generation, so this picker renders a single option on it and every one of those repairs is reached
// by the episode control instead (`traceEpisodeModel.js`).
export const nodeAttemptOptions = (currentAttempt) => {
  const current = Number.isSafeInteger(currentAttempt) && currentAttempt >= 0 ? currentAttempt : 0
  return Array.from({ length: current + 1 }, (_, attempt) => ({
    attempt,
    label: attempt === current ? `attempt ${attempt} (current)` : `attempt ${attempt}`,
    current: attempt === current,
  }))
}

// Which payload may render for the selected attempt AND anchor. The node-DETAIL payload always
// describes the CURRENT attempt at the NEWEST window, so it is a valid fallback only while that is
// what is being shown; falling back to it for a historical selection would silently show the newest
// trace under an older label — the one failure mode that is worse than showing nothing.
//
// `anchored` is the second half of the same rule and arrived with `?before=`: an operator who seeks
// to repair #1 must never be shown the last 512 spans of the node because the seek has not settled
// yet. Two different questions, one answer — what may stand in for a read that has not happened.
export const traceForAttempt = ({ selected, current, paged, detail, anchored = false }) =>
  (selected === current && !anchored ? (paged || detail || null) : (paged || null))

// A historical attempt — or an anchored window — has no detail payload to render, so its read is not
// optional the way the current attempt's tail pager is.
export const attemptReadRequired = ({ selected, current, canPageFurther, anchored = false }) =>
  selected !== current || anchored || canPageFurther

// ONE LOG FILE, SEVERAL ATTEMPTS — the stage log a repaired node's bands share.
//
// The operator's report is "в разных попытках логов стейджей один и тот же лог", and it is exactly
// true. A stage's subprocess log is opened in APPEND mode (`runtime/sandbox.py`: `open(log_path,
// "a", …)`), so every inline-repair attempt of `train` writes into the same `train.log`; the node
// route serves that file's TAIL by NAME (`serve/routers/runs.py::node_logs` → `_tail(f"{name}.log")`,
// with `attempt` used only as a compare-and-swap fence, never to select bytes); and the conversation
// renders one band per attempt, each keyed by the stage LABEL. So N bands showed one string.
//
// Measured on the live `runs/e5small-dr-unified-v2` node 0: four `train` stage rows and four `mine`
// rows, ONE `train.log` (83,217 bytes, three tracebacks = four concatenated attempts) and ONE
// `mine.log` — four bands, one text, four times. On node 1 `train.log` is 9,342,149 bytes against a
// ~200 KB response cap, so all four bands show the LAST attempt's tail and the earlier attempts'
// bytes are not in the response at all.
//
// THE ENGINE'S BOUNDARY CANNOT BE BORROWED HERE, and that is why this is a disclosure and not a
// slice. `engine/train_monitor.py::attempt_byte_floor` really does know where an attempt's bytes
// begin — but it derives that from a `TrainingLogSnapshot` taken in memory at eval start, and
// nothing about it is written to the event log. There is no durable record of an attempt boundary
// for a later read to seek to, so a client that split this text by attempt would be inventing the
// split. What it CAN do is stop presenting one file as N attempts' logs.
export function stageLogAttribution(stages = []) {
  const rows = Array.isArray(stages) ? stages : []
  const totals = new Map()
  for (const stage of rows) {
    const label = typeof stage?.label === 'string' && stage.label ? stage.label : null
    if (label) totals.set(label, (totals.get(label) || 0) + 1)
  }
  const seen = new Map()
  return rows.map(stage => {
    const label = typeof stage?.label === 'string' && stage.label ? stage.label : null
    const total = label ? totals.get(label) : 0
    if (!label || total < 2) return null
    const ordinal = (seen.get(label) || 0) + 1
    seen.set(label, ordinal)
    return {
      label, ordinal, total,
      // Says what the text IS, not what it is not: a band that only denied being this attempt's log
      // would leave the operator with no idea what they are reading.
      note: `This run wrote all ${total} ${label} attempts into one ${label}.log, and the engine`
        + ' keeps no durable record of where one attempt ends. The text below is that whole file’s'
        + ` tail — the same bytes on all ${total} bands, not this attempt’s (#${ordinal}) alone.`,
    }
  })
}
