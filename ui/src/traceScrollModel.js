// The DECISIONS behind "reveal earlier steps by scrolling" — the trace surfaces' half of doc 25's
// pure-model-beside-its-React-half rule. No React, no I/O, no DOM: `node --test` drives every rule
// here directly, and `hooks.js::useTraceScroll` + `Inspector.jsx` keep only the choreography (the
// IntersectionObserver, the focus affordance, the scroll anchor, setState ordering).
//
// WHY this exists at all. The Inspector used to print a bounded projection's receipt and a
// `↧ load more` button, and the button climbed a window that stopped at a ceiling — at which point
// the notice said "The window is at its maximum" and the operator was simply refused. The ceiling
// itself is NOT the defect and is not removable: measured 2026-08-07 against the live server on
// runs/rubert-dr-0804 node 1 (14,507 spans), the conversation route costs 3.4 ms per span in
// `SpanIndex.full_spans_for_node` (one random read each, on a geesefs/S3 mount) plus 0.9 ms per span
// in `build_conversation` — 2.2 s at the 512 default, 17.3 s at the 4096 ceiling, and ~64 s / 5.6 MB
// with no ceiling at all, all of it on the request thread. What was wrong is that the operator had to
// ASK, one click at a time, and was then told a number and refused. So the window still climbs — it
// just climbs because they scrolled toward the older end, and the only thing they are ever told is
// what genuinely cannot be reached from here.
import { NODE_TRACE_SPAN_WINDOW, NODE_TRACE_SPAN_WINDOW_MAX } from './traceProjection.js'

// What this surface owes the operator right now. One of exactly four, because each demands a
// different thing of the component: nothing, a sentinel, a live region, or a final receipt.
export const TRACE_SCROLL_SETTLED = 'settled'      // nothing is hidden — no sentinel, no notice
export const TRACE_SCROLL_REACHABLE = 'reachable'  // more exists AND scrolling can reach it
export const TRACE_SCROLL_LOADING = 'loading'      // a wider read is in flight — announce it
export const TRACE_SCROLL_BOUNDED = 'bounded'      // more exists, this surface cannot reach it

// Announced in a `role="status"` live region, never as a count: while the read is in flight the only
// honest number is the one we do not have yet.
export const TRACE_SCROLL_LOADING_LABEL = 'Loading earlier steps…'
// The label on the focusable affordance. Visually hidden until focused (styles.css `.trace-reach`),
// because a pointer user gets the same effect from scrolling and must not see a control that
// announces a limit — but a screen-reader user navigating by virtual cursor never scrolls the
// container, so removing every focusable path is how infinite scroll makes earlier steps
// unreachable without a mouse.
export const TRACE_SCROLL_REACH_LABEL = 'Load earlier steps'
// Only ever shown for TRACE_SCROLL_BOUNDED, and deliberately NOT the old sentence about the window
// being maximal. That one was printed whenever there was no pager — including at the 512 default,
// where a node whose whole 258-span trace the server had already read was told its window was
// maximal while eight doublings were still available (measured on runs/rubert-dr-0807 node 2:
// 256 of 308 steps at the default, all 308 at ONE doubling).
export const traceScrollBoundedSuffix = 'No more of it can be loaded here.'

const positive = value => Number.isSafeInteger(value) && value > 0
const count = value => (Number.isSafeInteger(value) && value >= 0 ? value : 0)

// Can the shared window still grow? Mirrors `useNodeSpanWindow`'s own gate rather than re-deriving
// it, so the sentinel cannot be armed for a window the hook will refuse to raise.
export const traceWindowCanGrow = window =>
  !Number.isSafeInteger(window) || window < NODE_TRACE_SPAN_WINDOW_MAX

// Did the last widen BUY anything? The auto-loader must be provably terminating: an observer that
// re-fires while the response never changes is an infinite request loop against a route that costs
// seconds per call. The numeric ceiling alone is not enough of a stop — a legacy server that ignores
// `?limit=`, or one whose render caps do not scale, answers a wider request with a byte-identical
// payload and `omitted_* > 0` forever. `previous`/`next` are `{window, visible}` records of two
// SETTLED reads; a widen progresses when a strictly larger window returned strictly more.
export const traceWidenProgressed = (previous, next) => {
  if (!previous || !next) return true          // nothing to compare yet: never stall on the first read
  if (!positive(previous.window) || !positive(next.window)) return true
  if (next.window <= previous.window) return true   // not a widen at all (a poll tick, a re-mount)
  return count(next.visible) > count(previous.visible)
}

// Carry the stall finding across reads. A WIDEN decides it (did the bigger window buy anything?); a
// same-window read — the 4 s live poll, a re-mount — carries the previous answer rather than
// clearing it, because a poll tick is not evidence that the server would now answer a wider request
// differently. Stated here rather than inline in the component so the "who may clear a stall" rule
// has a truth table instead of living inside a setState updater nothing can call.
export const traceWidenStalled = (previous, next) => {
  const widened = positive(previous?.window) && positive(next?.window)
    && next.window > previous.window
  if (!widened) return previous?.stalled === true
  return !traceWidenProgressed(previous, next)
}

// The client deadline for ONE windowed read. It has to scale with the window, because the server's
// cost does: measured 2026-08-07 on runs/rubert-dr-0804 node 1, the conversation route answers in
// 2.2 s at the 512 default, 4.3 s at 1024, 8.7 s at 2048 and 17.3 s at the 4096 ceiling — almost
// exactly linear, because the dominant term is one random read per span. The Inspector's flat 8 s
// deadline therefore aborted EVERY read above ~2048, and (before `settleTraceRead` below) an aborted
// widen replaced the conversation the operator was reading with an "unavailable" receipt: asking for
// more cost them what they already had. 4x headroom over the measured curve, hard-capped so a
// pathological node cannot leave a request outstanding indefinitely.
export const TRACE_READ_DEADLINE_MS = 8000
export const TRACE_READ_DEADLINE_MAX_MS = 64000
export const traceReadDeadlineMs = window => {
  const factor = positive(window) ? Math.max(1, window / NODE_TRACE_SPAN_WINDOW) : 1
  return Math.min(Math.round(TRACE_READ_DEADLINE_MS * factor), TRACE_READ_DEADLINE_MAX_MS)
}

// The ONE state rule both trace surfaces read. `view` is a `traceProjection.js` window record
// (`traceWindow` for the span tree, `conversationWindow` for the conversation) — this module never
// re-derives what is hidden, it only decides what can be done about it.
//
// `unavailable` deliberately has NO state here. A failed observation is not a bounded projection,
// and it is the caller's job to render `TraceUnavailable` before it ever reaches this function:
// giving "we could not read it" a scroll sentinel would quietly re-label a read failure as
// "keep scrolling", which is the one confusion the whole projection vocabulary exists to prevent.
export const traceScrollState = ({ view, window, pending = false, stalled = false } = {}) => {
  if (!view || view.kind === 'complete') return TRACE_SCROLL_SETTLED
  if (pending) return TRACE_SCROLL_LOADING
  if (stalled || !traceWindowCanGrow(window)) return TRACE_SCROLL_BOUNDED
  return TRACE_SCROLL_REACHABLE
}

// May an intersection/focus issue a widen right now? `armed` is the operator-gesture budget below.
export const shouldWidenOnReach = ({ state, armed } = {}) =>
  state === TRACE_SCROLL_REACHABLE && armed === true

// The gesture budget: at most ONE automatic widen per operator scroll. Without it the observer
// chains — a thread whose collapsed bands are shorter than the viewport leaves the sentinel visible
// after the widen lands, so it fires again, and again, and opening one node walks the whole ladder
// unasked (512→1024→2048→4096 on rubert-dr-0804 node 1 is 32 s of server time and 2.7 MB nobody
// requested). Refilled by a real scroll, so "scroll up for more" keeps working indefinitely, and by
// a focus on the affordance, so a keyboard user is never budget-starved.
//   reach  — a widen was just issued: spend the budget
//   fail   — the widen failed: spend it too, so a broken route is retried on the next gesture
//            rather than hammered on every observer tick
//   scroll — the operator moved: refill
//   focus  — the affordance was focused: refill (the keyboard equivalent of moving)
export const armTraceScroll = (armed, event) => {
  if (event === 'scroll' || event === 'focus') return true
  if (event === 'reach' || event === 'fail') return false
  return armed !== false
}

// What a completed read does to the rendered payload. Split out because the failure branch is where
// the partial/unavailable distinction is easiest to lose: a widen that fails must NOT replace a good
// bounded payload with an unavailable receipt — the operator would watch the steps they were reading
// vanish because they scrolled. It also must not silently look like success.
//   ok, payload            → render it
//   failed, nothing yet    → the unavailable receipt (a first read that fails IS a failed observation)
//   failed, payload in hand → keep what they had; the failure is reported as its own alert, and the
//                             projection stays exactly as partial as it honestly is
export const settleTraceRead = (previous, outcome) => {
  if (outcome && outcome.ok) return { payload: outcome.payload, reachFailed: false }
  if (previous == null) return { payload: null, reachFailed: false, unavailable: true }
  return { payload: previous, reachFailed: true }
}
