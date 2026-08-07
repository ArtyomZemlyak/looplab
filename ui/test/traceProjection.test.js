import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import {
  NODE_TRACE_SPAN_WINDOW, NODE_TRACE_SPAN_WINDOW_MAX, TRACE_PARTIAL_NOTICE,
  conversationWindow, conversationWindowNotice, nextNodeSpanWindow,
  spansOmitted,
  traceDetailState, tracePartial, traceUnavailable, traceWindow, traceWindowNotice,
  unavailableTraceDetail,
} from '../src/traceProjection.js'

test('trace omission truth reconciles malformed server counters and local caps', () => {
  assert.equal(tracePartial({ total_spans: 20, visible_spans: 7, omitted_spans: -9 }), true)
  assert.equal(tracePartial({ total_spans: 1, visible_spans: 8 }), false)
  assert.equal(tracePartial({ truncated: true }), true)
})

test('trace detail transport failure stays distinct from a successful empty projection', () => {
  assert.deepEqual(traceDetailState({ attributes: {}, projection: {} }), {
    status: 'ready', attributes: {}, partial: false,
  })
  assert.deepEqual(traceDetailState({ attributes: [], projection: { detail_truncated: true } }), {
    status: 'ready', attributes: {}, partial: true,
  })
  assert.deepEqual(unavailableTraceDetail(), {
    status: 'unavailable', attributes: {}, partial: false,
  })
})

test('trace detail distinguishes its own truncation from elided siblings', () => {
  assert.deepEqual(traceDetailState({
    attributes: { output: 'complete' },
    projection: {
      truncated: true, detail_truncated: false, siblings_elided: true, omitted_trace_spans: 1,
    },
  }), { status: 'ready', attributes: { output: 'complete' }, partial: false })
  assert.deepEqual(traceDetailState({
    attributes: { output: 'bounded' },
    projection: { truncated: true, detail_truncated: true, siblings_elided: false },
  }), { status: 'ready', attributes: { output: 'bounded' }, partial: true })
  assert.equal(traceDetailState({ projection: { truncated: true } }).partial, false,
    'aggregate or legacy truncation must not claim that selected detail was cut')
})

test('HTTP-200 unavailable receipt takes precedence over empty or partial detail', () => {
  assert.equal(traceUnavailable({ unavailable: true }), true)
  for (const malformed of [{ unavailable: 'true' }, { unavailable: 1 }, null, []]) {
    assert.equal(traceUnavailable(malformed), false)
  }
  assert.deepEqual(traceDetailState({
    attributes: { output: 'must not masquerade as authoritative detail' },
    projection: { unavailable: true, truncated: true },
  }), { status: 'unavailable', attributes: {}, partial: false })
})

test('a bounded span window owes an ACTIONABLE control, or an honest count', () => {
  const omitting = { truncated: true, total_spans: 14507, visible_spans: 512, omitted_spans: 13995 }
  // The whole defect in one pair of assertions: the same payload is a pager where the surface can
  // page and a stated remainder where it cannot. Neither is ever the bare adjective.
  assert.equal(traceWindow(omitting, { canPage: true }).kind, 'pageable')
  assert.equal(traceWindow(omitting, { canPage: false }).kind, 'capped')
  assert.equal(traceWindowNotice(traceWindow(omitting)),
    'Showing 512 of 14507 spans; 13995 more are not displayed.')

  // `truncated` is a UNION. Per-span TEXT clamping sets it with nothing omitted, and no limit can
  // ever surface a clamped attribute — offering "load more" there is a button that cannot work.
  const clampedOnly = { truncated: true, total_spans: 40, visible_spans: 40, omitted_spans: 0,
    truncated_spans: 7 }
  assert.equal(tracePartial(clampedOnly), true, 'the raw union still reports partial…')
  assert.equal(traceWindow(clampedOnly, { canPage: true }).kind, 'complete',
    '…but nothing is REACHABLE, so no pager may be offered')

  // A payload that states no usable numbers must fail SAFE (assume spans remain), and say so
  // without inventing a count.
  assert.equal(spansOmitted({ truncated: true }), null)
  assert.equal(traceWindow({ truncated: true }, { canPage: true }).kind, 'pageable')
  assert.equal(traceWindowNotice(traceWindow({ truncated: true })), TRACE_PARTIAL_NOTICE)
})

test('the conversation states what IT hides — stages and turns, never spans', () => {
  // The live shape from runs/rubert-dr-0804 node 1: 13,995 spans omitted, and yet every one of the
  // 192 withheld stages was derivable from the 512 spans the response already held. Reading this
  // through the span rule would quote a number about a quantity the reader cannot see.
  const live = {
    truncated: true, total_spans: 14507, visible_spans: 512, omitted_spans: 13995,
    total_stages: 256, visible_stages: 64, omitted_stages: 192,
    total_turns: 425, visible_turns: 105, omitted_turns: 320,
  }
  const pageable = conversationWindow(live, { canPage: true })
  assert.equal(pageable.kind, 'pageable')
  assert.equal(pageable.omittedStages, 192)
  assert.equal(conversationWindowNotice(pageable),
    'Showing the most recent 105 of at least 425 steps.')
  assert.equal(conversationWindow(live, { canPage: false }).kind, 'capped')

  // With every span in hand the stage/turn totals are EXACT, so the notice drops the hedge. This is
  // the case the server's own test drives: nothing omitted by the span read, everything by the caps.
  const exact = {
    truncated: true, total_spans: 400, visible_spans: 400, omitted_spans: 0,
    total_stages: 200, visible_stages: 64, omitted_stages: 136,
    total_turns: 400, visible_turns: 128, omitted_turns: 272,
  }
  assert.equal(conversationWindow(exact, { canPage: true }).kind, 'pageable')
  assert.equal(conversationWindowNotice(conversationWindow(exact)),
    'Showing the most recent 128 of 400 steps.')

  // Fully paged: nothing hidden, no control, no notice — even though per-span text is still clamped.
  const whole = {
    truncated: true, truncated_spans: 3, total_spans: 400, visible_spans: 400, omitted_spans: 0,
    total_stages: 200, visible_stages: 200, omitted_stages: 0,
    total_turns: 400, visible_turns: 400, omitted_turns: 0,
  }
  assert.equal(conversationWindow(whole, { canPage: true }).kind, 'complete')

  // A payload predating the stage/turn counters must NOT read two absent fields as "nothing hidden";
  // it falls back to the span receipt.
  assert.equal(conversationWindow({ truncated: true, omitted_spans: 9 }, { canPage: true }).kind,
    'pageable')
  assert.equal(conversationWindow({ truncated: false, omitted_spans: 0 }).kind, 'complete')
})

test('one window rule: the doubling step stops exactly at the shared ceiling', () => {
  assert.equal(NODE_TRACE_SPAN_WINDOW, 512)
  assert.equal(nextNodeSpanWindow(NODE_TRACE_SPAN_WINDOW), 1024)
  // Climb from the default and confirm the walk terminates AT the ceiling, never past it. A pager
  // that overshoots asks the server for a window it will silently clamp, so the button stops
  // changing the response while still looking live.
  let window = NODE_TRACE_SPAN_WINDOW
  const walk = []
  for (let step = 0; step < 20; step += 1) {
    window = nextNodeSpanWindow(window)
    walk.push(window)
  }
  assert.deepEqual(walk.slice(0, 3), [1024, 2048, 4096])
  assert.equal(window, NODE_TRACE_SPAN_WINDOW_MAX)
  assert.ok(walk.every(value => value <= NODE_TRACE_SPAN_WINDOW_MAX))
  // A malformed current window restarts from the default rather than doubling nonsense.
  for (const malformed of [undefined, null, 0, -8, 1.5, '512', NaN]) {
    assert.equal(nextNodeSpanWindow(malformed), 1024)
  }
})

test('neither trace surface keeps a private copy of the node span window', async () => {
  const [dock, hooks] = await Promise.all([
    readFile(new URL('../src/Dock.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/hooks.js', import.meta.url), 'utf8'),
  ])
  // NEGATIVE pins stay substrings on purpose: what must not come back is the TEXT. Dock owned a
  // `NODE_TRACE_CAP_MAX = 4096` beside the Inspector's identical route with no pager at all, which
  // is precisely how the two drifted. The ceiling is now reachable only through traceProjection.js.
  assert.doesNotMatch(dock, /NODE_TRACE_CAP_MAX|4096/,
    'the node-trace ceiling must not be re-spelled in Dock')
  assert.match(dock, /useNodeSpanWindow\(\)/)
  assert.match(hooks, /NODE_TRACE_SPAN_WINDOW_MAX/,
    'the shared hook reads the ceiling from the model, it does not restate it')
  assert.doesNotMatch(hooks.slice(hooks.indexOf('export function useNodeSpanWindow')), /= 4096/)
})

test('Inspector and Dock preserve projection truth through every trace surface', async () => {
  const [inspector, dock] = await Promise.all([
    readFile(new URL('../src/Inspector.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/Dock.jsx', import.meta.url), 'utf8'),
  ])
  assert.match(inspector, /const unavailable = traceUnavailable\(conv\.projection\)[\s\S]*?if \(unavailable\) return <TraceUnavailable[\s\S]*?if \(!stages\.length\)/,
    'conversation unavailable must win over the ordinary empty state')
  // BOTH outcomes go through one `commit`. `Promise.allSettled` never rejects, so the `.catch` is
  // the DEADLINE — the failure a widened read actually hits (17.3 s measured at the ceiling) — and
  // a second settle path there is how it went back to saying nothing. The behaviour itself is
  // driven in inspectorTracePager.test.js; this only holds the single-path shape.
  assert.match(inspector,
    /const settled = settleTraceRead\(previous\?\.payload, \{ ok, payload \}\)[\s\S]*?if \(settled\.unavailable\) \{[\s\S]*?projection: \{ unavailable: true \}[\s\S]*?commit\(conversation\.status === 'fulfilled'[\s\S]*?\.catch\(\(\) => \{ if \(alive\(\)\) commit\(false, null\) \}\)/,
    'conversation failures route through the ONE settle rule: unavailable only with nothing in hand')
  assert.match(inspector, /if \(!spans\.length && !agent\)[\s\S]*?if \(unavailable\)[\s\S]*?<TraceUnavailable[\s\S]*?if \(spanWindow\.kind !== 'complete'\)[\s\S]*?TRACE_PARTIAL_EMPTY_NOTICE[\s\S]*?No execution spans/,
    'an empty node trace must check unavailable and the window before successful empty')
  assert.ok(inspector.indexOf("if (view === 'conversation')") < inspector.indexOf('if (!spans.length && !agent)'),
    'the selected conversation view must load before raw-tree empty/unavailable branches')
  assert.match(inspector, /\[open, io, runId, s\.span_id, kind\][\s\S]*?const retryIo = \(\) => setIo\(null\)[\s\S]*?<TraceUnavailable label="Trace detail unavailable\." onRetry=\{retryIo\}/,
    'span detail unavailable must be retryable even after the first expansion')
  assert.match(inspector, /function Conversation\(\{[\s\S]*?onRetry[\s\S]*?\[runId, n\.id, working, reloadNonce\][\s\S]*?<TraceUnavailable onRetry=\{onRetry\}/,
    'a finished conversation one-shot must expose an explicit retry')
  assert.match(inspector, /nodeConversation\(runId, n\.id, \{ signal, limit: spanLimit \}\)/,
    'the conversation must actually SEND the window it was given, or the control is decorative')
  assert.match(inspector, /export function NodeTrace\(\{ spans, runId, projection = \{\}, onRetry, onLoadMore,[\s\S]*?<TraceUnavailable onRetry=\{onRetry\}/)
  // The pager BUTTON is gone (2026-08-07): earlier steps arrive by scrolling. What must not be lost
  // with it is the receipt — a surface that genuinely cannot reach further still owes the COUNT.
  // That branch is `TRACE_SCROLL_BOUNDED` and it is DRIVEN in inspectorTracePager.test.js; these two
  // pins only hold the wiring that a rendered-text test cannot see: that both surfaces feed the ONE
  // window rule into the ONE scroll rule, rather than growing a second opinion about reachability.
  assert.match(inspector, /const spanWindow = traceWindow\(projection, \{ canPage: !!onLoadMore \}\)[\s\S]*?traceScrollState\(\{ view: spanWindow, window: spanLimit \}\)/,
    'the node trace must route its window through the shared scroll rule')
  assert.match(inspector, /<TraceReach state=\{scroll\} onReach=\{onLoadMore\}[\s\S]*?notice=\{traceWindowNotice\(spanWindow\)\}/,
    'a surface with nowhere to go still owes the COUNT, never a bare adjective')
  // The operator's own report: the CONVERSATION view is the default, and it had no control at all.
  // The `pending` EXPRESSION is matched loosely on purpose. This pin is about the conversation
  // deriving its OWN scroll state from its OWN window — not about the exact boolean. Pinned
  // exactly, it went red the moment the same change put a `!reachFailed &&` guard in front of it,
  // i.e. a stale pin failing on a strictly better implementation. That is the pin's fault.
  assert.match(inspector, /const convWindow = conversationWindow\(conv\.projection, \{ canPage: !!onLoadMore \}\)[\s\S]*?const scroll = traceScrollState\(\{[\s\S]*?view: convWindow,[\s\S]*?pending: [^,\n]*spanLimit > read\.window,/,
    'the conversation view must derive its own scroll state, not print a dead partial notice')
  assert.doesNotMatch(inspector, /load more spans|load more of this conversation|at its maximum/,
    'the removed pager text must not come back — scrolling is the only control now')
  assert.doesNotMatch(
    inspector.slice(inspector.indexOf('function Conversation('),
      inspector.indexOf('function RunSetupLog(')),
    /'Trace projection is partial\.'/,
    'the conversation must not re-inline the dead notice this change removed')
  assert.match(inspector, />span tree<\/button>/)
  assert.doesNotMatch(inspector, />raw spans<\/button>|every span's full I\/O|nothing is lost|WHOLE re-sent/)

  assert.match(dock, /setTrace\(\{[\s\S]*?spans:[\s\S]*?projection: d\?\.projection \|\| \{\}/,
    'operation trace must retain the server projection envelope')
  assert.match(dock, /<NodeTrace spans=\{nodeSpans\}[\s\S]*?projection=\{nodeTrace\.projection\}/,
    'node trace must pass its projection metadata to the shared renderer')
  assert.match(dock, /const unavailable = traceUnavailable\(current\.projection\)[\s\S]*?<TraceUnavailable label="Trace unavailable; retrying automatically\." \/>/,
    'live tail transport/unavailable state must not look like an empty waiting feed')
  assert.match(dock, /3000, \[scope, active, open, limit\], \{ enabled: active && open \}/,
    'the open live tail must automatically recover from a transient unavailable state (limit paging keeps enabled intact)')
  assert.match(dock, /!current\.loaded[\s\S]*?loading trace…[\s\S]*?partialControl[\s\S]*?!tail\.length && !partial[\s\S]*?waiting for the next agent step/,
    'an empty partial live tail must offer load-earlier / older-history, never a plain waiting feed')
  assert.match(dock, /const canLoadEarlier = partial && limit < TRACE_LIMIT_MAX[\s\S]*?setLimit\(value => Math\.min\(value \* 2, TRACE_LIMIT_MAX\)\)/,
    '"load earlier spans" must raise the tail limit toward the server ceiling')
  assert.match(dock, /function OpTrace[\s\S]*?\[runId, traceId, retryNonce\][\s\S]*?onRetry=\{retry\}/,
    'operation trace HTTP-200 unavailable must be retryable')
  assert.match(dock, /const retryNodeTrace = \(\) => \{[\s\S]*?setNodeTrace\(null\)[\s\S]*?setNodeTraceError\(false\)[\s\S]*?setNodeTraceNonce/,
    'node trace retry must enter a real loading state before issuing another request')
  assert.match(dock, /projection=\{nodeTrace\.projection\} runId=\{runId\} onRetry=\{retryNodeTrace\}/,
    'HTTP-200 unavailable node traces must share the same retry path as transport failures')
  assert.doesNotMatch(dock, /raw <think>|full I\/O of any observation|full, UNtruncated text|FULL content/)
})

test('an unstated conversation counter stays absent, and hidden-ness never depends on it', () => {
  // `conversationPagerLabel` lived here until the pager button was removed. Its RULE outlives it and
  // is what this now drives directly: an absent counter must stay `null` and never become `0`.
  // Every consumer of these records — the notice, the scroll state, anything later — reads the same
  // two fields, so absent-read-as-zero would go on inventing "0 earlier stages" wherever it lands.

  // The two omissions are computed independently by the server, so "hidden" does not imply the STAGE
  // counter is the non-zero one. A heavily repaired node keeps its whole thread in a handful of
  // stages: nothing is hidden by the stage cap, 144 turns are hidden by the turn cap.
  const turnsOnly = conversationWindow({
    truncated: true, total_spans: 400, visible_spans: 400, omitted_spans: 0,
    total_stages: 12, visible_stages: 12, omitted_stages: 0,
    total_turns: 400, visible_turns: 256, omitted_turns: 144,
  }, { canPage: true })
  assert.equal(turnsOnly.kind, 'pageable')
  assert.equal(turnsOnly.omittedStages, 0)
  assert.equal(turnsOnly.omittedTurns, 144)

  // A counter the payload does not state stays absent: a legacy projection carrying only the turn
  // count must not have a stage count invented for it.
  const legacy = conversationWindow({ omitted_turns: 320, total_turns: 425, visible_turns: 105 },
    { canPage: true })
  assert.equal(legacy.omittedStages, null)
  assert.equal(legacy.omittedTurns, 320)
  assert.equal(legacy.kind, 'pageable')

  // A payload stating NEITHER conversation counter defers to the span receipt rather than reading
  // two absent fields as proof that nothing is hidden.
  const deferred = conversationWindow({ truncated: true, total_spans: 900, visible_spans: 512,
    omitted_spans: 388 }, { canPage: true })
  assert.equal(deferred.kind, 'pageable')
  assert.equal(deferred.omittedStages, null)
  assert.equal(deferred.omittedTurns, null)
})
