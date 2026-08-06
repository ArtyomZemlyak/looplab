// Guard for the trace-clear recovery (doc 25 UI-09) — the confirm → busy → verify/blocked phases and
// their failure classification, which used to be ~350 lines inside `Inspector.jsx::Trace`'s single
// 510-line closure and had NO test of any kind: the whole decision was reachable only by mounting the
// Inspector with a stubbed `clearNodeTrace`, so nobody had.
//
// The decision worth guarding is a safety claim about a DELETE. "Nothing was cleared" and "the
// outcome is unknown, verify this same operation" are different claims about whether the operator's
// diagnostics still exist, and a wrong branch reads as a wording change rather than as a bug. So the
// rules get a truth table (traceClearModel.js is pure), and the end-to-end path is then DRIVEN
// through the real Inspector — which is also the only thing that proves `Trace` is still wired to
// `useTraceClear` at all, rather than carrying a second copy of the machine.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

import {
  TRACE_CLEAR_DEFINITELY_NOT_MUTATED_CODES, TRACE_CLEAR_RETRYABLE_STATUSES,
  classifyTraceClearFailure, initialTraceClearMessage, traceClearAvailability,
} from '../src/traceClearModel.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const GENERATION = 'a'.repeat(64)
const TRACE_REVISION = 'b'.repeat(64)
const OPERATION = {
  expectedGeneration: GENERATION, expectedTraceRevision: TRACE_REVISION, nodeGeneration: 2,
  operationId: `tc_${'c'.repeat(32)}`,
}
const failure = (overrides = {}) => Object.assign(new Error('boom'), overrides)

test('trace clear tells the operator nothing was deleted only when the server proved it', () => {
  // Every code on the pre-mutation list is a refusal the handler made BEFORE touching the file. The
  // safety claim is the pair {no verify affordance, no durable ambiguous record} — a message that
  // asked the operator to verify would be telling them a deletion might have happened.
  for (const code of TRACE_CLEAR_DEFINITELY_NOT_MUTATED_CODES) {
    const { message, recovery } = classifyTraceClearFailure(
      failure({ code, status: 409 }), { operation: OPERATION, nodeId: 7 })
    assert.equal(message.verifyOperation || false, false, code)
    assert.equal(recovery, null, `${code} must not leave a durable ambiguous record`)
    assert.doesNotMatch(message.text, /outcome is unknown|may already have/, code)
    assert.match(message.text, /(N|n)othing (was|new was) cleared|wasn’t cleared|No additional deletion/, code)
  }
  // A plain 4xx that is not one of the "try again" statuses is also pre-mutation — that rule is what
  // classifies `trace_clear_operation_superseded`, the one terminal code deliberately absent from the
  // list above (it can only arrive as a 409, and the list is for codes that carry no useful status).
  const rejected = classifyTraceClearFailure(failure({ status: 422 }), { operation: OPERATION })
  assert.equal(rejected.recovery, null)
  assert.match(rejected.message.text, /Nothing was confirmed as deleted/)
  const superseded = classifyTraceClearFailure(
    failure({ status: 409, code: 'trace_clear_operation_superseded' }), { operation: OPERATION })
  assert.equal(superseded.recovery, null)
  assert.match(superseded.message.text, /No additional deletion was attempted/)

  // Everything else leaves the outcome UNKNOWN and must keep the exact operation so the next click
  // verifies it rather than submitting a second destructive request.
  const unknown = [
    failure({ status: 500 }), failure({ status: 503 }), failure({ status: 504 }),
    ...TRACE_CLEAR_RETRYABLE_STATUSES.map(status => failure({ status })),
    failure({ name: 'TimeoutError' }), failure({}),
  ]
  for (const error of unknown) {
    const { message, recovery } = classifyTraceClearFailure(
      error, { operation: OPERATION, nodeId: 7 })
    assert.equal(message.verifyOperation, true, JSON.stringify(error.status ?? error.name))
    assert.deepEqual(recovery, { phase: 'ambiguous', message, operation: OPERATION })
    assert.match(message.text, /outcome is unknown\. Verify this same operation/)
  }
})

test('a VERIFY may only be closed by a durable terminal receipt, never by an ordinary refusal', () => {
  // The first request may already have escaped, so a normally pre-mutation answer to the VERIFY says
  // nothing about what the ORIGINAL handler did. Keep verifying.
  for (const error of [failure({ status: 409, code: 'engine_running' }), failure({ status: 503 }),
    failure({ code: 'trace_clear_pending', detail: {} }), failure({ status: 428 })]) {
    const { message, recovery } = classifyTraceClearFailure(
      error, { verifying: true, operation: OPERATION, nodeId: 7 })
    assert.equal(message.verifyOperation, true, error.code || String(error.status))
    assert.equal(recovery?.phase, 'ambiguous')
    assert.match(message.text, /check this same operation again|still being resolved/i)
  }
  // Only these close it: the lifecycle moved past any resolvable pending operation, or the server
  // says this operation belongs to a different one. Both stop the loop WITHOUT claiming a deletion.
  // Sent with the 409 they arrive as, because `trace_clear_operation_superseded` is classified
  // pre-mutation by the 4xx rule rather than by the code list.
  for (const code of ['run_generation_changed', 'node_generation_changed', 'trace_revision_changed',
    'trace_clear_operation_superseded', 'trace_clear_operation_conflict']) {
    const { message, recovery } = classifyTraceClearFailure(
      failure({ code, status: 409 }), { verifying: true, operation: OPERATION, nodeId: 7 })
    assert.equal(message.verifyOperation || false, false, code)
    assert.equal(recovery, null, code)
    assert.match(message.text, /No additional deletion was attempted|Nothing new was cleared/, code)
  }
})

test('a server-named pending operation replaces the one being carried, and only if it is well formed', () => {
  const pending = `tc_${'d'.repeat(32)}`
  const adopted = classifyTraceClearFailure(
    failure({ code: 'trace_clear_pending', detail: { operation_id: pending } }),
    { operation: OPERATION, nodeId: 7 })
  assert.equal(adopted.recovery.operation.operationId, pending)
  assert.equal(adopted.recovery.operation.expectedTraceRevision, TRACE_REVISION,
    'adopting the id must not drop the scope the confirmation belonged to')
  // A malformed id is refused rather than carried: verifying an unparseable operation forever is
  // worse than verifying the one this tab minted and knows the shape of.
  for (const bogus of ['tc_nope', '../../etc', `tc_${'D'.repeat(32)}`, '', undefined]) {
    const kept = classifyTraceClearFailure(
      failure({ code: 'trace_clear_pending', detail: { operation_id: bogus } }),
      { operation: OPERATION, nodeId: 7 })
    assert.equal(kept.recovery.operation, OPERATION, String(bogus))
  }
})

test('a changed node attempt names the attempt it moved to only when the server sent a usable one', () => {
  const withCurrent = classifyTraceClearFailure(
    failure({ code: 'node_generation_changed', detail: { current_node_generation: 5 } }),
    { operation: OPERATION, nodeId: 7 })
  assert.match(withCurrent.message.text, /Experiment #7 moved to attempt 5\./)
  for (const detail of [{ current_node_generation: 'five' }, { current_node_generation: 1.5 }, {}]) {
    const withoutCurrent = classifyTraceClearFailure(
      failure({ code: 'node_generation_changed', detail }), { operation: OPERATION, nodeId: 7 })
    assert.match(withoutCurrent.message.text, /Experiment #7 changed attempts\./, JSON.stringify(detail))
  }
  // Server free text never reaches the operator, whatever the code.
  const leaky = classifyTraceClearFailure(
    Object.assign(new Error('token=secret'), { status: 500, detail: 'password=secret' }),
    { operation: OPERATION, nodeId: 7 })
  assert.doesNotMatch(leaky.message.text, /secret/)
})

test('the clear control is offered only with an exact scope and a run that is provably not writing', () => {
  const ready = {
    nodeStatus: 'done', nodeGeneration: 2, expectedGeneration: GENERATION,
    expectedTraceRevision: TRACE_REVISION, detailStatus: 'ready', reloadPending: false,
    unavailable: false, live: { engine_running: false },
  }
  assert.equal(traceClearAvailability(ready).available, true)

  // `engine_running` UNKNOWN is not the same as false. A missing/absent live state means run write
  // ownership was never established, and clearing under it would race the engine's own appends.
  for (const live of [undefined, null, {}, { engine_running: true }, { engine_running: 'false' }]) {
    assert.equal(traceClearAvailability({ ...ready, live }).available, false, JSON.stringify(live))
  }
  // The branch ORDER is the order of causes an operator can act on, and each one names itself.
  const reasons = [
    [{ nodeStatus: 'building' }, /Experiment creation is incomplete/],
    [{ live: { engine_running: true } }, /The run is active/],
    [{ live: {} }, /Run write ownership is being verified/],
    [{ reloadPending: true }, /Experiment details are refreshing/],
    [{ detailStatus: 'loading' }, /Experiment details are loading/],
    [{ detailStatus: 'stale' }, /must refresh successfully/],
    [{ unavailable: true }, /Trace diagnostics could not be loaded/],
    [{ expectedTraceRevision: '' }, /Trace identity is loading/],
    [{ expectedGeneration: 'short' }, /Trace identity is loading/],
    [{ nodeGeneration: null }, /Trace identity is loading/],
  ]
  for (const [overrides, expected] of reasons) {
    const verdict = traceClearAvailability({ ...ready, ...overrides })
    assert.equal(verdict.available, false, JSON.stringify(overrides))
    assert.match(verdict.unavailableText, expected, JSON.stringify(overrides))
  }
  // A run that is actively writing outranks every other cause: it is the one the operator must wait
  // out rather than fix, and reporting a refresh instead would send them in circles.
  assert.match(
    traceClearAvailability({ ...ready, live: { engine_running: true }, reloadPending: true,
      detailStatus: 'loading' }).unavailableText,
    /The run is active/)
})

test('a restored recovery record decides what the operator is told before anything is requested', () => {
  assert.equal(initialTraceClearMessage(null), null)
  // A POST left this tab and its answer never came back. The experiment stays fenced.
  const busy = initialTraceClearMessage({ phase: 'busy' })
  assert.deepEqual([busy.kind, busy.blocking, busy.pending, busy.verifyOperation],
    ['status', true, true, false])
  assert.match(busy.text, /still pending/)
  const verifying = initialTraceClearMessage({ phase: 'busy', mode: 'verify' })
  assert.equal(verifying.verifyOperation, true)
  assert.match(verifying.text, /whether the original trace clear completed/)
  // These carry their own message; substituting one here would lose the classification above.
  for (const phase of ['blocked', 'reconcile', 'ambiguous']) {
    const message = { kind: 'error', blocking: true, text: `carried:${phase}` }
    assert.equal(initialTraceClearMessage({ phase, message }), message)
  }
  // A two-click confirmation is NOT a durable intent — restoring it would arm a destructive control
  // the operator never re-confirmed for the view they are now looking at.
  const confirm = initialTraceClearMessage({ phase: 'confirm' })
  assert.deepEqual([confirm.kind, confirm.blocking], ['success', false])
  assert.match(confirm.text, /Nothing was submitted/)
  // Anything unrecognised fails CLOSED.
  const unknown = initialTraceClearMessage({ phase: 'who-knows' })
  assert.deepEqual([unknown.kind, unknown.blocking], ['error', true])
  assert.match(unknown.text, /could not be recovered/)
})

// ---------------------------------------------------------------------------------------------
// End to end through the real Inspector. This is the part no source pin can stand in for: it proves
// the control is wired to `useTraceClear`, that an unknown outcome fences the experiment, and — the
// property the whole operation-id protocol exists for — that the recovery button re-sends the SAME
// operation instead of submitting a second destructive clear.
// ---------------------------------------------------------------------------------------------
async function withInspector(body) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    localStorage: dom.window.localStorage, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    // Abort-aware, like the sibling Inspector harnesses: a stub that ignores the signal leaves the
    // unmount path (which aborts the in-flight detail read) pending forever, turning any failed
    // assertion in this file into a harness timeout instead of a fast red.
    fetch: (url, options = {}) => new Promise((resolve, reject) => {
      requests.push({ url: String(url), method: options.method || 'GET', body: options.body, resolve })
      options.signal?.addEventListener('abort',
        () => reject(new DOMException('The operation was aborted.', 'AbortError')))
    }),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: Inspector }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/Inspector.jsx'),
    ])
    root = createRoot(dom.window.document.getElementById('root'))
    const reply = async (request, payload, status = 200) => act(async () => {
      request.resolve({
        ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
      })
      await Promise.resolve()
    })
    const click = async node => act(async () =>
      node.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })))
    const button = text => [...dom.window.document.querySelectorAll('button')]
      .find(node => node.textContent.trim().startsWith(text))
    // EVERY status line the clear control is showing, joined. The unavailability line and the
    // outcome notice share a class and both render while the post-clear refresh is in flight, so a
    // `querySelector` would silently read whichever one happened to be first in the DOM.
    const notice = () => [...dom.window.document.querySelectorAll('.trace-clear-status')]
      .map(node => node.textContent).join(' | ')

    await act(async () => root.render(React.createElement(Inspector, {
      runId: 'demo', nodeId: 7, tab: 'Trace', setTab() {}, onToast() {},
      expectedGeneration: GENERATION,
      state: { nodes: { 7: { id: 7, status: 'done', attempt: 2 } }, drifts: [], direction: 'max' },
      live: { engine_running: false, finished: true, nodes: {} },
    })))
    // The node-detail read is what carries the exact trace snapshot the confirmation is scoped to.
    // Pick it BY URL: the Trace tab's conversation view issues its own `/conversation` and `/logs`
    // reads first, and answering one of those with a node payload would leave the detail resource
    // loading and the clear control disabled for a reason that has nothing to do with the machine.
    const detail = requests.find(request => /\/nodes\/7\?/.test(request.url))
    assert.ok(detail, 'the Inspector must read the node detail that carries trace_revision')
    await reply(detail, {
      id: 7, status: 'done', attempt: 2, run_generation: GENERATION,
      trace_revision: TRACE_REVISION, trace: { nodes: [], projection: {} }, agent_report: null,
    })
    // The clear POSTs, in order. Indexed by URL rather than by position because the conversation
    // view keeps its own reads in flight beside them.
    const clears = () => requests.filter(request => /\/clear_trace$/.test(request.url))
    await body({ dom, requests, clears, reply, click, button, notice })
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
}

test('an unknown clear outcome fences the experiment and its recovery verifies the SAME operation',
  async () => {
    await withInspector(async ({ clears, reply, click, button, notice }) => {
      const trigger = button('✕ clear trace')
      assert.ok(trigger, 'the Trace tab must offer a clear control for a settled run')
      assert.equal(trigger.disabled, false)

      // Two clicks, on purpose: arming and confirming are separate, and only the second sends.
      await click(trigger)
      assert.ok(button('✕ confirm clear'), 'the first click must only arm the confirmation')
      assert.equal(clears().length, 0, 'arming must not send anything')

      await click(button('✕ confirm clear'))
      assert.equal(clears().length, 1)
      const post = clears()[0]
      assert.equal(post.method, 'POST')
      assert.match(post.url, /\/api\/runs\/demo\/nodes\/7\/clear_trace$/)
      const sent = JSON.parse(post.body)
      assert.equal(sent.expected_generation, GENERATION)
      assert.equal(sent.expected_trace_revision, TRACE_REVISION)
      assert.equal(sent.node_generation, 2)
      assert.match(sent.operation_id, /^tc_[0-9a-f]{32}$/)

      // A 503 cannot prove the handler refused before deleting anything.
      await reply(post, { detail: { code: 'unavailable', message: 'password=must-not-render' } }, 503)
      assert.match(notice(), /outcome is unknown\. Verify this same operation/)
      assert.doesNotMatch(notice(), /password/)
      assert.equal(button('✕ clear trace')?.disabled, true,
        'an unknown outcome must fence the control rather than invite a second clear')

      // The recovery control VERIFIES. Re-submitting would be a second destructive request against a
      // trace whose first delete may already have landed — which is the entire reason the operation
      // id is minted client-side and carried in the durable record.
      const verify = button('↻ verify clear outcome')
      assert.ok(verify, 'an unknown outcome must offer verification, not a plain refresh')
      await click(verify)
      assert.equal(clears().length, 2)
      const second = clears()[1]
      assert.equal(second.method, 'POST')
      assert.equal(JSON.parse(second.body).operation_id, sent.operation_id,
        'verification must re-send the original operation id, never a fresh one')

      // The server settles it: the original clear did land.
      await reply(second, { status: 'succeeded', operation_id: sent.operation_id })
      assert.match(notice(), /Trace cleared for #7 · attempt 2/)
    })
  })

test('a clear the server refused before mutating says so and leaves the control usable', async () => {
  await withInspector(async ({ clears, reply, click, button, notice }) => {
    await click(button('✕ clear trace'))
    await click(button('✕ confirm clear'))
    await reply(clears()[0], { detail: { code: 'engine_running' } }, 409)
    assert.match(notice(), /Trace wasn’t cleared because the run is active/)
    assert.equal(button('↻ verify clear outcome'), undefined,
      'a proven pre-mutation refusal must not ask the operator to verify a deletion')
    assert.ok(button('↻ refresh experiment'), 'it offers the ordinary refresh instead')
  })
})
