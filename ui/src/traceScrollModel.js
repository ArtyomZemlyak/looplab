// The DECISIONS behind "reveal earlier steps by scrolling" — the trace surfaces' half of doc 25's
// pure-model-beside-its-React-half rule. No React, no I/O, no DOM: `node --test` drives every rule
// here directly, and `hooks.js::useTraceScroll` + `Inspector.jsx` keep only the choreography (the
// IntersectionObserver, the scroll re-arm listener, the focus affordance, setState ordering).
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
// The label on the affordance, which is VISIBLE to every user (styles.css `.trace-reach`). It was
// visually-hidden-until-focused, on the reasoning that a pointer user gets the same effect from
// scrolling and must not see a control that announces a limit. That was measured against the
// operator and abandoned: they reported the control missing — twice — because one that only exists
// once you tab to it does not exist to a pointer user. The screen-reader half of the rationale still
// holds and is why it is a real focusable button rather than only a sentinel: a virtual cursor never
// scrolls the container, so an IntersectionObserver alone leaves earlier steps unreachable.
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

// The client deadline for ONE windowed read. A trace read costs TWO things and only one of them
// scales with the window, so the deadline is a sum, not a product:
//
//  * the WINDOW term. Measured 2026-08-07 on runs/rubert-dr-0804 node 1, the conversation route
//    answers in 2.2 s at the 512 default, 4.3 s at 1024, 8.7 s at 2048 and 17.3 s at the 4096
//    ceiling — almost exactly linear, because the dominant term there is one random read per span.
//  * the FIXED term, which the first version of this rule did not model at all and which is what the
//    operator has actually been hitting. Measured 2026-08-12 against the live server on
//    runs/rubertlite-dr-unified-v5 (engine running, run root on a geesefs/S3 mount):
//    `/nodes/1/trace?limit=512` answered in 4.3-15.6 s — for a 1.4 KB payload describing SIX spans.
//    Nothing about that is span-bound: `light_spans_for_node` served it in 0.0 ms and the warm index
//    cost 1-3 ms. The time went on FIVE absent-fence probes per request (`AppState.run_dir`'s
//    deletion fence, plus the reset marker in `_assert_trace_reset_clear` and `_state_payload`,
//    once each for the before- and after-read lifecycle CAS), which cost 721-2,923 ms together
//    because a negative lookup on that mount is a round trip. `/nodes/{n}/conversation?limit=512`
//    measured 20.4-22.3 s the same way, and a cold node trace on a FINISHED run 23.6 s.
//
// So a flat 8 s deadline was not "tight", it was WRONG about the shape of the cost: it aborted the
// default-window read of a six-span node while leaving four doublings of genuine window headroom
// unused. Every such abort is one of the "Trace unavailable" panels this rule exists to prevent, and
// on a first read there is no last-good payload for `settleTraceRead` to keep.
// (`looplab/core/fence.py` now warms the directory listing before each of those probes, which took
// the five of them to an 11 ms median — but a browser cannot know which server it is talking to, so
// the deadline stays honest about the slowest one that ships.)
export const TRACE_READ_FIXED_MS = 24000
export const TRACE_READ_WINDOW_MS = 8000
// What the deadline is at the default window — the name every caller and test already used.
export const TRACE_READ_DEADLINE_MS = TRACE_READ_FIXED_MS + TRACE_READ_WINDOW_MS
// Hard-capped so a pathological node cannot leave a request outstanding indefinitely.
export const TRACE_READ_DEADLINE_MAX_MS = 64000
export const traceReadDeadlineMs = window => {
  const factor = positive(window) ? Math.max(1, window / NODE_TRACE_SPAN_WINDOW) : 1
  return Math.min(
    Math.round(TRACE_READ_FIXED_MS + TRACE_READ_WINDOW_MS * factor), TRACE_READ_DEADLINE_MAX_MS)
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
  // `capped` is the window rules' own word for "this surface has nowhere to go" — a caller that
  // passed `canPage: false`, which in practice means it wired no raise-the-window callback at all.
  // Honouring it rather than re-deriving reachability from the window number is what stops a
  // sentinel being armed on a surface whose `onReach` is `undefined`: an affordance that cannot do
  // anything is exactly the dead control this change exists to remove, and it would silently swallow
  // the count that surface still owes. (The chat feed's inline waterfall renders NodeTrace both
  // ways.)
  if (view.kind === 'capped') return TRACE_SCROLL_BOUNDED
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

// WHY a trace read did not produce a trace. Both mean "we do not have it", and that is exactly why
// they were being printed with one sentence — but the operator's next move is opposite:
//   * UNREADABLE — a deadline, a transport failure, a 5xx, an envelope that did not parse. The
//     evidence still exists; waiting or retrying is the answer, and this is the one worth retrying
//     automatically.
//   * SUPERSEDED — the server answered about a DIFFERENT run generation / node / attempt than the
//     one on screen. Every trace surface refuses such a payload before render (that fence is the
//     point), but refusing it is not a read failure: the trace the operator asked for was replaced
//     under them, and retrying this same scope will keep answering about the new one. Reloading the
//     run is what actually helps, and a "Retry" button that cannot help is the dead control the
//     bounded-projection vocabulary already exists to remove.
// Distinct because "there is no evidence here" and "we could not read the evidence" being one word
// is the confusion this whole module was written against; a superseded read is a third fact and had
// been folded into the second.
export const TRACE_FAILURE_UNREADABLE = 'unreadable'
export const TRACE_FAILURE_SUPERSEDED = 'superseded'

export const traceFailureLabel = (kind, { retrying = false } = {}) => {
  if (kind === TRACE_FAILURE_SUPERSEDED) {
    return 'This trace belongs to a run generation that was replaced; reload the run.'
  }
  return retrying ? 'Trace unavailable; retrying automatically.' : 'Trace unavailable.'
}

// Is this failure worth asking the same question again? Only an unreadable one — see above.
export const traceFailureIsRetryable = kind => kind !== TRACE_FAILURE_SUPERSEDED

// THE AUTOMATIC RETRY BUDGET for a ONE-SHOT trace read.
//
// The surfaces that poll (the live tail, a node that is currently building) recover on their own
// tick. The one-shot reads — an expanded event row's node trace, an operation's trace by trace id —
// did not: one failure parked them on a receipt with a manual Retry, for the rest of that row's
// life. Against a route measured at 4-15 s per call behind an 8 s deadline that was not a rare
// event, and it is precisely the shape of the operator's complaint: the trace is "unavailable"
// until they click, and it works when they do.
//
// Bounded, because unbounded is the other failure: a route that is genuinely down must not be
// re-asked forever at seconds per call (the same rule `armTraceScroll` holds for the scroll
// loader). After the budget, the manual Retry is the honest control — and it refills the budget.
export const TRACE_RETRY_MS = 5000
export const TRACE_RETRY_MAX = 2
export const traceRetryMs = (failures, kind = TRACE_FAILURE_UNREADABLE) =>
  (traceFailureIsRetryable(kind) && Number.isSafeInteger(failures)
    && failures > 0 && failures <= TRACE_RETRY_MAX ? TRACE_RETRY_MS : null)
