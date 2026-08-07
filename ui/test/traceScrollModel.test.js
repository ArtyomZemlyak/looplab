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
  TRACE_READ_DEADLINE_MAX_MS, TRACE_READ_DEADLINE_MS, TRACE_SCROLL_BOUNDED, TRACE_SCROLL_LOADING,
  TRACE_SCROLL_REACHABLE, TRACE_SCROLL_SETTLED, armTraceScroll, settleTraceRead, shouldWidenOnReach,
  traceReadDeadlineMs, traceScrollState, traceWidenProgressed, traceWidenStalled, traceWindowCanGrow,
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

test('the read deadline scales with the window, because the server cost does', () => {
  // Measured 2026-08-07 on runs/rubert-dr-0804 node 1: 2.2 s at 512, 4.3 s at 1024, 8.7 s at 2048,
  // 17.3 s at the 4096 ceiling. A flat 8 s deadline aborted every read above ~2048, which (before
  // settleTraceRead) blanked the conversation the operator was reading.
  assert.equal(traceReadDeadlineMs(NODE_TRACE_SPAN_WINDOW), TRACE_READ_DEADLINE_MS)
  assert.equal(traceReadDeadlineMs(1024), TRACE_READ_DEADLINE_MS * 2)
  assert.equal(traceReadDeadlineMs(NODE_TRACE_SPAN_WINDOW_MAX), TRACE_READ_DEADLINE_MS * 8)
  // Every rung stays comfortably above the measured server time, which is the property that matters.
  for (const [window, measuredMs] of [[512, 2200], [1024, 4340], [2048, 8730], [4096, 17340]]) {
    assert.ok(traceReadDeadlineMs(window) > measuredMs * 2,
      `deadline for window ${window} must leave headroom over the measured ${measuredMs} ms`)
  }
  // Never unbounded, and never BELOW the base for a nonsense window.
  assert.equal(traceReadDeadlineMs(1 << 20), TRACE_READ_DEADLINE_MAX_MS)
  assert.equal(traceReadDeadlineMs(0), TRACE_READ_DEADLINE_MS)
  assert.equal(traceReadDeadlineMs(undefined), TRACE_READ_DEADLINE_MS)
})
