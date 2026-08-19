// The rules behind "earlier trace steps arrive by scrolling", stated so they can be driven without a
// React root. The React half (hooks.js::useTraceScroll, Inspector.jsx::TraceReach) is choreography
// only, and inspectorTracePager.test.js drives it end to end; what is here is every decision that
// must be right whether or not anyone can see a sentinel.
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  NODE_TRACE_SPAN_WINDOW, NODE_TRACE_SPAN_WINDOW_MAX, conversationWindow, traceWindow,
} from '../src/traceProjection.js'
import {
  TRACE_FAILURE_SUPERSEDED, TRACE_FAILURE_UNREADABLE, TRACE_READ_DEADLINE_MAX_MS,
  TRACE_READ_DEADLINE_MS, TRACE_READ_FIXED_MS, TRACE_READ_WINDOW_MS, TRACE_RETRY_MAX, TRACE_RETRY_MS,
  TRACE_SCROLL_BOUNDED, TRACE_SCROLL_LOADING, TRACE_SCROLL_REACHABLE, TRACE_SCROLL_SETTLED,
  armTraceScroll, settleTraceRead, shouldWidenOnReach, traceFailureIsRetryable, traceFailureLabel,
  traceReadSameAnchor,
  traceReadDeadlineMs, traceRetryMs, traceScrollState, traceWidenProgressed, traceWidenStalled,
  traceWindowCanGrow,
} from '../src/traceScrollModel.js'

test('the four states, over the SAME window records the two trace surfaces already build', () => {
  // Nothing hidden: no sentinel, no notice, nothing to announce.
  const complete = traceWindow({ total_spans: 40, visible_spans: 40, omitted_spans: 0 })
  assert.equal(complete.kind, 'complete')
  assert.equal(traceScrollState({ view: complete, window: NODE_TRACE_SPAN_WINDOW }),
    TRACE_SCROLL_SETTLED)

  // The operator's own case, measured on runs/rubert-dr-0807 node 2: the whole 258-span trace is in
  // hand and the TURN cap alone hides 52 steps. Reachable — one doubling shows all of them.
  const turnCapped = conversationWindow({
    truncated: true, total_spans: 258, visible_spans: 258, omitted_spans: 0,
    total_stages: 34, visible_stages: 27, omitted_stages: 7,
    total_turns: 308, visible_turns: 256, omitted_turns: 52,
  }, { canPage: true })
  assert.equal(traceScrollState({ view: turnCapped, window: NODE_TRACE_SPAN_WINDOW }),
    TRACE_SCROLL_REACHABLE)

  // A read in flight outranks reachability — the sentinel must not issue a second widen while the
  // first is still costing the server seconds.
  assert.equal(
    traceScrollState({ view: turnCapped, window: NODE_TRACE_SPAN_WINDOW, pending: true }),
    TRACE_SCROLL_LOADING)

  // At the ceiling there is genuinely nowhere to go, and THAT is when the count is owed.
  assert.equal(traceScrollState({ view: turnCapped, window: NODE_TRACE_SPAN_WINDOW_MAX }),
    TRACE_SCROLL_BOUNDED)
  // …and so is a surface that wired no raise-the-window callback at all. The window rules already
  // say this with `capped`, and re-deriving reachability from the window NUMBER alone armed a
  // sentinel whose `onReach` was `undefined` — a dead control, and one that swallowed the count that
  // surface still owed. (Caught by traceRecoveryContract.test.js, which renders NodeTrace both ways.)
  const noCallback = traceWindow({ truncated: true }, { canPage: false })
  assert.equal(noCallback.kind, 'capped')
  assert.equal(traceScrollState({ view: noCallback, window: NODE_TRACE_SPAN_WINDOW }),
    TRACE_SCROLL_BOUNDED)
  // …and so is a stall: a server that answers a wider window with no more rows.
  assert.equal(
    traceScrollState({ view: turnCapped, window: NODE_TRACE_SPAN_WINDOW, stalled: true }),
    TRACE_SCROLL_BOUNDED)
})

test('an unavailable projection never becomes a scroll state', () => {
  // The one confusion this whole vocabulary exists to prevent: "we could not read it" must not be
  // re-labelled "keep scrolling". The model refuses to speak for a missing view at all, so a caller
  // that forgets to render TraceUnavailable first gets SETTLED (no sentinel, no notice) rather than
  // an affordance inviting the operator to scroll for data nobody has.
  assert.equal(traceScrollState({ view: null, window: NODE_TRACE_SPAN_WINDOW }),
    TRACE_SCROLL_SETTLED)
  assert.equal(traceScrollState(), TRACE_SCROLL_SETTLED)
})

test('the window growth gate is the hook’s own gate, not a second opinion', () => {
  assert.equal(traceWindowCanGrow(NODE_TRACE_SPAN_WINDOW), true)
  assert.equal(traceWindowCanGrow(NODE_TRACE_SPAN_WINDOW_MAX), false)
  assert.equal(traceWindowCanGrow(NODE_TRACE_SPAN_WINDOW_MAX + 1), false)
  // An absent/garbage window is treated as growable: a surface that cannot say where it is must not
  // silently present itself as exhausted.
  assert.equal(traceWindowCanGrow(undefined), true)
  assert.equal(traceWindowCanGrow('512'), true)
})

test('the auto-loader terminates: a widen that buys nothing stops it', () => {
  // A widen that returns more is progress.
  assert.equal(traceWidenProgressed({ window: 512, visible: 256 }, { window: 1024, visible: 308 }),
    true)
  // A widen that returns the SAME rows is not, and this is the stop the numeric ceiling cannot give:
  // a server that ignores ?limit= would otherwise be asked forever, seconds per call.
  assert.equal(traceWidenProgressed({ window: 512, visible: 256 }, { window: 1024, visible: 256 }),
    false)
  // A same-window read is a poll tick, not a widen — never evidence of a stall.
  assert.equal(traceWidenProgressed({ window: 512, visible: 256 }, { window: 512, visible: 256 }),
    true)
  // Nothing to compare yet must never read as a stall on the very first response.
  assert.equal(traceWidenProgressed(null, { window: 512, visible: 256 }), true)

  // …and the carry rule: a widen DECIDES, a poll tick CARRIES the previous answer rather than
  // clearing it, so a 4 s live refresh cannot silently re-arm an auto-loader that already proved it
  // has nowhere to go.
  const stalled = { window: 1024, visible: 256, stalled: true }
  assert.equal(traceWidenStalled(stalled, { window: 1024, visible: 256 }), true)
  assert.equal(traceWidenStalled({ window: 512, visible: 256 }, { window: 1024, visible: 256 }),
    true)
  assert.equal(traceWidenStalled({ window: 512, visible: 256 }, { window: 1024, visible: 999 }),
    false, 'a widen that DID buy rows clears the stall')
  assert.equal(traceWidenStalled(null, { window: 512, visible: 5 }), false)
})

test('a stall belongs to ONE anchor, so seeking to another episode restores the control', () => {
  // The operator's "кнопка ушла ОПЯТЬ": TRACE_SCROLL_BOUNDED is the only state that renders no
  // control at all, `stalled` forces it, and nothing but a node/attempt/generation change cleared
  // it — so one unproductive widen retired the affordance for the rest of the visit.
  const stalledAtTail = { window: 1024, visible: 256, before: null, stalled: true }
  // Same place, poll tick: the finding still stands. (The termination property is untouched.)
  assert.equal(traceWidenStalled(stalledAtTail, { window: 1024, visible: 256, before: null }), true)
  // A DIFFERENT anchor is a different question. An anchored window selects the `limit` rows ENDING
  // at the anchor, so a widen near the beginning of a node legitimately buys nothing — that is a
  // short episode, not a surface with nowhere to go, and it must not answer for the next one.
  assert.equal(traceWidenStalled(stalledAtTail, { window: 1024, visible: 256, before: 'span-42' }),
    false, 'seeking to an episode must not inherit the tail read’s stall')
  // …and back again: leaving an anchor for the tail is equally a new question.
  const stalledAtEpisode = { window: 1024, visible: 12, before: 'span-42', stalled: true }
  assert.equal(traceWidenStalled(stalledAtEpisode, { window: 1024, visible: 12, before: null }),
    false)
  // Within ONE anchor the auto-loader still terminates — this is the rule the stall exists for.
  assert.equal(
    traceWidenStalled({ window: 512, visible: 12, before: 'span-42' },
      { window: 1024, visible: 12, before: 'span-42' }), true)
  assert.equal(
    traceWidenStalled({ window: 512, visible: 12, before: 'span-42' },
      { window: 1024, visible: 30, before: 'span-42' }), false)
})

test('the anchor comparison treats every spelling of "the tail" as one place', () => {
  // The record is built from `traceSubjectBefore`, which yields undefined for an unanchored subject
  // and a span id otherwise; a blank string must never read as a third place.
  assert.equal(traceReadSameAnchor({}, { before: null }), true)
  assert.equal(traceReadSameAnchor({ before: undefined }, { before: '' }), true)
  assert.equal(traceReadSameAnchor({ before: 'a' }, { before: 'a' }), true)
  assert.equal(traceReadSameAnchor({ before: 'a' }, { before: 'b' }), false)
  assert.equal(traceReadSameAnchor(null, { before: null }), true)
  // A non-string anchor is not an anchor: it must degrade to the tail, never to a distinct place.
  assert.equal(traceReadSameAnchor({ before: 7 }, { before: null }), true)
})

test('one automatic widen per operator gesture', () => {
  // Without this the observer chains: a thread of collapsed bands shorter than the viewport leaves
  // the sentinel visible after the widen lands, so it fires again, and opening one node walks the
  // whole ladder unasked (512->4096 on the measured stress node is 32 s of server time and 2.7 MB).
  assert.equal(shouldWidenOnReach({ state: TRACE_SCROLL_REACHABLE, armed: true }), true)
  assert.equal(shouldWidenOnReach({ state: TRACE_SCROLL_REACHABLE, armed: false }), false)
  // Only REACHABLE spends the budget: a loading read must not be doubled, and a bounded or settled
  // surface has nothing to ask for.
  for (const state of [TRACE_SCROLL_LOADING, TRACE_SCROLL_BOUNDED, TRACE_SCROLL_SETTLED]) {
    assert.equal(shouldWidenOnReach({ state, armed: true }), false)
  }

  assert.equal(armTraceScroll(true, 'reach'), false, 'a widen spends the budget')
  assert.equal(armTraceScroll(false, 'scroll'), true, 'the operator moving refills it')
  assert.equal(armTraceScroll(false, 'focus'), true,
    'focus refills it too — a virtual-cursor user never fires the scroll listener')
  assert.equal(armTraceScroll(true, 'fail'), false,
    'a failed widen spends the budget rather than hammering a broken route every tick')
  assert.equal(armTraceScroll(true, 'anything-else'), true)
  assert.equal(armTraceScroll(false, 'anything-else'), false)
})

test('a failed widen keeps what the operator had; only a first failure is unavailable', () => {
  const held = { stages: [{ label: 'implement' }], projection: { truncated: true } }

  const ok = settleTraceRead(held, { ok: true, payload: { stages: [1, 2, 3] } })
  assert.deepEqual(ok.payload, { stages: [1, 2, 3] })
  assert.equal(ok.reachFailed, false)

  // The defect this rule removes: asking for MORE cost the operator what they already had. A widen
  // that fails leaves the bounded payload exactly as partial as it honestly is, and reports the
  // failure as its own thing — never by turning `partial` into `unavailable`.
  const failedWiden = settleTraceRead(held, { ok: false })
  assert.equal(failedWiden.payload, held)
  assert.equal(failedWiden.reachFailed, true)
  assert.equal(failedWiden.unavailable, undefined)

  // A FIRST read that fails IS a failed observation and must keep saying so.
  const firstFailure = settleTraceRead(null, { ok: false })
  assert.equal(firstFailure.payload, null)
  assert.equal(firstFailure.unavailable, true)
  assert.equal(firstFailure.reachFailed, false)
})

test('the read deadline is a FIXED term plus a window term, because the server cost is', () => {
  // A trace read costs two things and only one of them scales with the window.
  //
  // The WINDOW term, measured 2026-08-07 on runs/rubert-dr-0804 node 1: 2.2 s at 512, 4.3 s at 1024,
  // 8.7 s at 2048, 17.3 s at the 4096 ceiling — one random read per span, near-linear.
  //
  // The FIXED term, measured 2026-08-12 against the live server on runs/rubertlite-dr-unified-v5
  // (engine running, run root on geesefs): `/nodes/1/trace?limit=512` took 4.3-15.6 s to return a
  // 1.4 KB payload describing SIX spans, and `/nodes/{n}/conversation?limit=512` 20.4-22.3 s. None
  // of that is span-bound — it is five absent-fence probes per request on a mount where a negative
  // lookup is a round trip. The first version of this rule modelled ONLY the window term, so the
  // default-window read of a six-span node was aborted at 8 s while four doublings of window
  // headroom went unused, and every such abort is a "Trace unavailable" panel.
  assert.equal(traceReadDeadlineMs(NODE_TRACE_SPAN_WINDOW), TRACE_READ_DEADLINE_MS)
  assert.equal(traceReadDeadlineMs(1024), TRACE_READ_FIXED_MS + TRACE_READ_WINDOW_MS * 2)
  assert.equal(traceReadDeadlineMs(2048), TRACE_READ_FIXED_MS + TRACE_READ_WINDOW_MS * 4)
  // The fixed term is what a small window buys, and it must not be swallowed by the window term:
  // doubling the window may not double the deadline, or the base rung goes back under the measured
  // per-request cost the moment anyone re-tunes the slope.
  assert.ok(traceReadDeadlineMs(1024) < traceReadDeadlineMs(NODE_TRACE_SPAN_WINDOW) * 2)
  // Every rung stays above BOTH measured terms — the window curve and the fixed per-request cost —
  // which is the property that matters.
  for (const [window, measuredMs] of [[512, 2200], [1024, 4340], [2048, 8730], [4096, 17340]]) {
    assert.ok(traceReadDeadlineMs(window) > measuredMs * 2,
      `deadline for window ${window} must leave headroom over the measured ${measuredMs} ms`)
    assert.ok(traceReadDeadlineMs(window) > 23600,
      `deadline for window ${window} must clear the measured 23.6 s fixed per-request cost`)
  }
  // Never unbounded, and never BELOW the base for a nonsense window.
  assert.equal(traceReadDeadlineMs(NODE_TRACE_SPAN_WINDOW_MAX), TRACE_READ_DEADLINE_MAX_MS)
  assert.equal(traceReadDeadlineMs(1 << 20), TRACE_READ_DEADLINE_MAX_MS)
  assert.equal(traceReadDeadlineMs(0), TRACE_READ_DEADLINE_MS)
  assert.equal(traceReadDeadlineMs(undefined), TRACE_READ_DEADLINE_MS)
})

test('a superseded read and an unreadable one are different facts with different words', () => {
  // Both mean "we do not have it" and they were printed with one sentence. The operator's next move
  // is opposite: wait/retry versus reload the run, because retrying the same scope will keep
  // answering about the generation that replaced theirs.
  assert.notEqual(traceFailureLabel(TRACE_FAILURE_SUPERSEDED),
    traceFailureLabel(TRACE_FAILURE_UNREADABLE))
  assert.match(traceFailureLabel(TRACE_FAILURE_SUPERSEDED), /reload the run/i)
  assert.match(traceFailureLabel(TRACE_FAILURE_UNREADABLE), /unavailable/i)
  // The retrying variant may only ever be claimed for a failure that IS being retried.
  assert.match(traceFailureLabel(TRACE_FAILURE_UNREADABLE, { retrying: true }),
    /retrying automatically/i)
  assert.doesNotMatch(traceFailureLabel(TRACE_FAILURE_SUPERSEDED, { retrying: true }),
    /retrying automatically/i)
  // An unclassified failure degrades to the readable one — never to "the run was replaced", which
  // would tell the operator to throw away a screen that is merely slow.
  assert.equal(traceFailureLabel(undefined), traceFailureLabel(TRACE_FAILURE_UNREADABLE))
  assert.equal(traceFailureIsRetryable(TRACE_FAILURE_SUPERSEDED), false)
  assert.equal(traceFailureIsRetryable(TRACE_FAILURE_UNREADABLE), true)
})

test('the one-shot retry budget is bounded, refillable, and never spent on a superseded read', () => {
  // The surfaces that poll recover on their own tick; the one-shot reads (an expanded event row's
  // node trace, an operation trace by id) did not, so ONE failure parked them on a receipt with a
  // manual Retry for the life of that row. Against a route measured at 4-15 s per call behind an
  // 8 s deadline that is the operator's complaint verbatim.
  assert.equal(traceRetryMs(0), null, 'a read that has not failed schedules nothing')
  assert.equal(traceRetryMs(1), TRACE_RETRY_MS)
  assert.equal(traceRetryMs(TRACE_RETRY_MAX), TRACE_RETRY_MS)
  // Bounded, for the same reason `armTraceScroll` spends the budget on a FAILED widen: a route that
  // is genuinely down must not be re-asked forever at seconds per call.
  assert.equal(traceRetryMs(TRACE_RETRY_MAX + 1), null)
  assert.equal(traceRetryMs(999), null)
  // A superseded read is never retryable at all — this scope cannot produce the trace they wanted.
  assert.equal(traceRetryMs(1, TRACE_FAILURE_SUPERSEDED), null)
  // Malformed counters schedule nothing rather than defaulting into a loop.
  for (const bad of [undefined, null, -1, 1.5, NaN, '1']) assert.equal(traceRetryMs(bad), null)
})
