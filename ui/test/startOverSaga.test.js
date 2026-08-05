import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import { loadRunStartOverIntent, saveRunStartOverIntent } from '../src/runStartOverRecovery.js'
import {
  START_OVER_AUTO_RETRY_LIMIT, START_OVER_REQUEST_TIMEOUT_MS, useStartOverCoordination,
  useStartOverRecovery,
} from '../src/useStartOverRecovery.js'

// Doc 25 UI-03. Start over ARCHIVES a finished run generation. The saga moved out of RunView.jsx
// into useStartOverRecovery.js, and this file drives it through a REAL React render, the REAL
// sessionStorage envelope and the REAL api.js request path, because the properties that matter here
// are all about what survives a lost response:
//
//   * "rejected" and "outcome unknown" must never collapse into each other.
//   * a response may commit only while it is still the request this component is waiting for.
//   * a phase write may only land on the operation id it was staged against.
//
// None of those are visible in a unit fixture over the pure model — they are properties of the
// component's own request lifecycle, which is exactly what had no coverage before this change.

const GEN_A = 'a'.repeat(64)
const GEN_B = 'b'.repeat(64)
const GEN_C = 'c'.repeat(64)
const FOREIGN_OP = '11111111-2222-4333-8444-555555555555'
const KEY = runId => `ll.run-start-over.${encodeURIComponent(runId)}`

// One harness for every scenario: jsdom + a real createRoot, a fetch stub whose responses the test
// hands out one at a time, and a timer table so the auto-retry cadence can be fired on demand.
const withSaga = async (fn) => {
  const dom = new JSDOM('<!doctype html><div id="root"></div><div data-route-main tabindex="-1"></div>',
    { url: 'https://looplab.test/', pretendToBeVisual: true })
  const timers = []
  const fetchCalls = []
  const responders = []
  const toasts = []
  const fetchStub = async (url, options = {}) => {
    const call = { url: String(url), body: JSON.parse(options.body || '{}'), signal: options.signal }
    fetchCalls.push(call)
    const responder = responders.shift()
    assert.ok(responder, `no response staged for request ${fetchCalls.length}: ${call.url}`)
    return responder(call)
  }
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    localStorage: dom.window.localStorage, Storage: dom.window.Storage,
    HTMLElement: dom.window.HTMLElement, Element: dom.window.Element, Node: dom.window.Node,
    CustomEvent: dom.window.CustomEvent,
    requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
    cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
    fetch: fetchStub,
    IS_REACT_ACT_ENVIRONMENT: true,
    setTimeout: (callback, delay) => {
      timers.push({ callback, delay, cleared: false })
      return timers.length
    },
    clearTimeout: id => { if (timers[id - 1]) timers[id - 1].cleared = true },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  // The envelope's `updatedAt` is what re-arms the automatic re-check, so wall-clock granularity is
  // load-bearing: three real persists inside one millisecond would silently stop the cadence.
  const realNow = Date.now
  let clock = 1_700_000_000_000
  Date.now = () => clock
  const armed = () => timers.filter(timer => !timer.cleared)
  const fireDelay = async (delay) => {
    const timer = armed().find(entry => entry.delay === delay)
    assert.ok(timer, `expected an armed timer at ${delay}ms, saw ${armed().map(t => t.delay)}`)
    timer.cleared = true
    clock += delay
    await React.act(async () => { await timer.callback() })
  }
  const exposed = { current: null }
  const seen = { openCurrentGeneration: 0, resetWorkspace: 0, retryRun: 0 }
  const route = { allow: true, openCurrentGeneration() { seen.openCurrentGeneration += 1; return route.allow } }
  const retryRunRef = { current: () => { seen.retryRun += 1 } }
  const routeMainRef = { current: null }
  const Harness = props => {
    const state = useStartOverRecovery({
      runId: props.runId, generation: props.generation, reviewMode: props.reviewMode,
    })
    const ops = useStartOverCoordination(state, {
      runId: props.runId, generation: props.generation, reviewMode: props.reviewMode,
      runStatus: props.runStatus, route, retryRunRef, routeMainRef,
      showToast: message => toasts.push(message),
      resetWorkspace: () => { seen.resetWorkspace += 1 },
    })
    exposed.current = { ...state, ...ops }
    return null
  }
  let root
  try {
    const { createRoot } = await import('react-dom/client')
    root = createRoot(document.querySelector('#root'))
    const render = async (props) => {
      await React.act(async () => {
        root.render(React.createElement(Harness, {
          runId: 'demo', generation: GEN_A, reviewMode: false, runStatus: 'ready', ...props,
        }))
      })
    }
    const stored = (runId = 'demo') => {
      const raw = dom.window.sessionStorage.getItem(KEY(runId))
      return raw == null ? null : JSON.parse(raw)
    }
    await fn({
      render, saga: exposed, fetchCalls, responders, toasts, seen, route, timers, armed,
      fireDelay, stored, storage: dom.window.sessionStorage,
    })
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    Date.now = realNow
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
}

const accepted = (generation = GEN_B) => call => ({
  ok: true,
  json: async () => ({
    ok: true, operation_id: call.body.operation_id,
    expected_generation: call.body.expected_generation, generation,
  }),
})
const refused = (status, detail) => () => ({
  ok: false, status, headers: { get: () => null }, json: async () => ({ detail }),
})

test('a verified receipt is what authorizes the handoff, and it clears the durable envelope', async () => {
  await withSaga(async ({ render, saga, responders, fetchCalls, seen, stored, toasts }) => {
    await render({})
    assert.equal(saga.current.startOverMutationBlocked, false)

    responders.push(accepted(GEN_B))
    await React.act(async () => { saga.current.beginStartOver(GEN_A) })

    assert.equal(fetchCalls.length, 1)
    assert.equal(fetchCalls[0].url, '/api/runs/demo/reset')
    assert.equal(fetchCalls[0].body.expected_generation, GEN_A)
    const operationId = fetchCalls[0].body.operation_id
    assert.match(operationId, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
    // The envelope is durable BEFORE the outcome is known, and it now carries the exact replacement
    // generation the server's receipt named — that value is what the handoff is allowed to trust.
    assert.equal(stored().operationId, operationId)
    assert.equal(stored().phase, 'accepted')
    assert.equal(stored().replacementGeneration, GEN_B)
    assert.equal(saga.current.startOverMutationBlocked, true,
      'the run stays locked until the replacement generation is actually open')
    assert.equal(seen.openCurrentGeneration, 0, 'the source generation is still the visible one')

    // The replacement generation becomes visible: only now is the route handed over.
    await render({ generation: GEN_B })
    assert.equal(seen.openCurrentGeneration, 1)
    assert.equal(seen.resetWorkspace, 1)
    assert.equal(stored(), null, 'a completed operation leaves no recovery evidence behind')
    assert.equal(saga.current.startOverMutationBlocked, false)
    assert.ok(toasts.includes('Previous run generation archived. The new run is open.'))
  })
})

test('a route that cannot be reopened keeps the lock instead of losing the operation', async () => {
  await withSaga(async ({ render, saga, responders, route, stored, toasts }) => {
    await render({})
    responders.push(accepted(GEN_B))
    await React.act(async () => { saga.current.beginStartOver(GEN_A) })

    route.allow = false
    await render({ generation: GEN_B })
    assert.equal(saga.current.startOverRouteSyncFailed, true)
    assert.equal(saga.current.startOverHandoff, true)
    assert.equal(stored().phase, 'accepted', 'the verified receipt must survive a failed handoff')
    assert.equal(saga.current.startOverMutationBlocked, true)
    assert.ok(toasts.includes('The new run is ready, but its address could not be updated. Retry opening it.'))

    route.allow = true
    await React.act(async () => { saga.current.retryStartOver() })
    assert.equal(stored(), null)
    assert.equal(saga.current.startOverMutationBlocked, false)
  })
})

test('a 425 receipt is PENDING, not failed: it keeps the envelope and re-checks the same operation',
  async () => {
    await withSaga(async ({ render, saga, responders, fetchCalls, stored, fireDelay, toasts }) => {
      await render({})
      const pending = call => refused(425, {
        code: 'reset_pending', message: 'saved', operation_id: call.body.operation_id,
        expected_generation: call.body.expected_generation, status: 'pending',
      })(call)
      responders.push(pending)
      await React.act(async () => { saga.current.beginStartOver(GEN_A) })

      const operationId = fetchCalls[0].body.operation_id
      assert.equal(stored().phase, 'pending')
      assert.equal(stored().operationId, operationId)
      assert.equal(saga.current.startOverMutationBlocked, true)
      assert.ok(toasts.includes('The server saved this exact Start over request. Checking its outcome…'))

      // The automatic re-check replays the EXACT identity — a duplicate archive is the one outcome
      // this protocol exists to prevent.
      responders.push(accepted(GEN_B))
      await fireDelay(900 + 450)
      assert.equal(fetchCalls.length, 2)
      assert.equal(fetchCalls[1].body.operation_id, operationId)
      assert.equal(fetchCalls[1].body.expected_generation, GEN_A)
      assert.equal(stored().phase, 'accepted')
    })
  })

test('the automatic re-check is BOUNDED: after three tries the outcome is declared unknown',
  async () => {
    await withSaga(async ({ render, saga, responders, fetchCalls, stored, fireDelay, toasts }) => {
      await render({})
      const pending = call => refused(425, {
        code: 'reset_pending', message: 'saved', operation_id: call.body.operation_id,
        expected_generation: call.body.expected_generation, status: 'pending',
      })(call)
      responders.push(pending)
      await React.act(async () => { saga.current.beginStartOver(GEN_A) })
      // Literal cadence, not derived from the constant: 900 + n*450 for n = 1..3.
      for (const delay of [1350, 1800]) {
        responders.push(pending)
        await fireDelay(delay)
        assert.equal(stored().phase, 'pending')
      }
      responders.push(pending)
      await fireDelay(2250)
      assert.equal(fetchCalls.length, 4)
      assert.equal(stored().phase, 'unknown', 'the budget is spent; nothing is assumed about the run')
      assert.equal(saga.current.startOverMutationBlocked, true)
      assert.ok(toasts.includes('Start over is still unresolved. Use Retry exact request to check it again.'))
    })
  })

test('an unreachable server leaves the outcome UNKNOWN and the envelope intact', async () => {
  await withSaga(async ({ render, saga, responders, stored, toasts }) => {
    await render({})
    responders.push(refused(503, { code: 'upstream', message: 'gateway' }))
    await React.act(async () => { saga.current.beginStartOver(GEN_A) })

    assert.equal(stored().phase, 'unknown')
    assert.equal(saga.current.startOverMutationBlocked, true,
      'an unknown outcome must keep the run locked, not release it as if nothing happened')
    assert.ok(toasts.includes(
      'Start-over outcome is not confirmed. Retry this exact request before doing anything else.'))
  })
})

test('only the ORIGINAL response may prove a pre-mutation rejection', async () => {
  // The first request is refused with a status that proves the server never acted: the envelope is
  // released and the operator is free again.
  await withSaga(async ({ render, saga, responders, stored, toasts }) => {
    await render({})
    responders.push(refused(428, { code: 'run_not_finished', message: 'the run is still going' }))
    await React.act(async () => { saga.current.beginStartOver(GEN_A) })
    assert.equal(stored(), null)
    assert.equal(saga.current.startOverMutationBlocked, false)
    assert.ok(toasts.includes('the run is still going'))
  })
  // The SAME status on a retry proves nothing of the sort — the first request may still be
  // finishing, or may already have crossed the archive. The lock stays.
  await withSaga(async ({ render, saga, responders, stored }) => {
    await render({})
    responders.push(refused(503, { code: 'upstream', message: 'gateway' }))
    await React.act(async () => { saga.current.beginStartOver(GEN_A) })
    assert.equal(stored().phase, 'unknown')

    responders.push(refused(428, { code: 'run_not_finished', message: 'the run is still going' }))
    await React.act(async () => { saga.current.retryStartOver() })
    assert.equal(stored()?.phase, 'unknown',
      'a retry receiving 428 must not be read as "the run was never touched"')
    assert.equal(saga.current.startOverMutationBlocked, true)
  })
})

test('a response that arrives after the component moved on may not commit', async () => {
  await withSaga(async ({ render, saga, responders, stored, storage }) => {
    await render({})
    let settle
    responders.push(() => new Promise(resolve => { settle = resolve }))
    await React.act(async () => { saga.current.beginStartOver(GEN_A) })
    assert.equal(stored().phase, 'submitting')

    // The route moves to another run. The request in flight belongs to nobody now.
    await render({ runId: 'other', generation: GEN_C })
    settle(accepted(GEN_B)({ body: { operation_id: stored().operationId, expected_generation: GEN_A } }))
    await React.act(async () => { await Promise.resolve(); await Promise.resolve() })

    assert.equal(JSON.parse(storage.getItem(KEY('demo'))).phase, 'submitting',
      'a late response must not advance an envelope this component is no longer waiting on')
    assert.equal(storage.getItem(KEY('other')), null)
  })
})

test('a competing tab owns the envelope: its operation is adopted, never overwritten', async () => {
  await withSaga(async ({ render, saga, responders, fetchCalls, stored, storage, toasts }) => {
    await render({})
    responders.push(refused(503, { code: 'upstream', message: 'gateway' }))
    await React.act(async () => { saga.current.beginStartOver(GEN_A) })
    const mine = stored().operationId
    assert.equal(stored().phase, 'unknown')

    // Another tab replaces the durable envelope with a DIFFERENT operation for the same run.
    assert.equal(saveRunStartOverIntent({
      schema: 3, runId: 'demo', expectedGeneration: GEN_A, operationId: FOREIGN_OP,
      replacementGeneration: null, phase: 'submitting', updatedAt: Date.now(),
    }, storage), true)
    assert.notEqual(mine, FOREIGN_OP)

    await React.act(async () => { saga.current.retryStartOver() })
    assert.equal(fetchCalls.length, 1, 'no second request may be sent under a foreign identity')
    assert.equal(stored().operationId, FOREIGN_OP, 'the foreign envelope is adopted, not clobbered')
    assert.equal(saga.current.startOverIntent.operationId, FOREIGN_OP)
    assert.ok(toasts.includes('The saved Start over identity changed. No request was submitted.'))
  })
})

test('a reload mid-submit reopens as UNKNOWN under the same operation id', async () => {
  await withSaga(async ({ render, saga, storage }) => {
    assert.equal(saveRunStartOverIntent({
      schema: 3, runId: 'demo', expectedGeneration: GEN_A, operationId: FOREIGN_OP,
      replacementGeneration: null, phase: 'submitting', updatedAt: Date.now(),
    }, storage), true)
    await render({})
    assert.equal(saga.current.startOverIntent.phase, 'unknown',
      'a submitting marker that outlived its component means the response was lost, not that it failed')
    assert.equal(saga.current.startOverIntent.operationId, FOREIGN_OP,
      'the identity is preserved so an explicit retry rejoins instead of archiving twice')
    assert.equal(saga.current.startOverMutationBlocked, true)
    // The durable record is untouched by merely reading it back.
    assert.equal(loadRunStartOverIntent('demo', storage).intent.phase, 'submitting')
  })
})

test('review mode can never carry a Start-over recovery state', async () => {
  await withSaga(async ({ render, saga, storage }) => {
    assert.equal(saveRunStartOverIntent({
      schema: 3, runId: 'demo', expectedGeneration: GEN_A, operationId: FOREIGN_OP,
      replacementGeneration: null, phase: 'unknown', updatedAt: Date.now(),
    }, storage), true)
    await render({ reviewMode: true })
    // This is what makes RunView's three Start-over screens unreachable for a reviewer, which is in
    // turn why they may omit the review pill and the `review-mode` class (see RunScreen.jsx).
    assert.deepEqual(saga.current.startOverRecovery, { kind: 'none', intent: null })
    assert.equal(saga.current.startOverIntent, null)
    assert.equal(saga.current.startOverMutationBlocked, false)
  })
})

test('the request deadline and the retry budget are fixed contracts, not whatever the code says',
  () => {
    // Pinned as LITERALS: every other assertion in this file is written in terms of these, so a
    // widened budget would sail through a green suite while changing how long a destructive
    // operation can sit unresolved in front of an operator.
    assert.equal(START_OVER_REQUEST_TIMEOUT_MS, 15_000)
    assert.equal(START_OVER_AUTO_RETRY_LIMIT, 3)
  })

// ── RunView must not re-grow a private copy ─────────────────────────────────────────────────────

test('RunView keeps the preflight and delegates the operation to the one saga module', async () => {
  const runView = await readFile(new URL('../src/RunView.jsx', import.meta.url), 'utf8')
  assert.match(runView, /import \{ useStartOverCoordination, useStartOverRecovery \} from '\.\/useStartOverRecovery\.js'/)
  assert.match(runView, /beginStartOver\(confirmedGeneration\)/,
    'the retained-work preflight stays with the drafts it is about, and then delegates')
  // NEGATIVE pins: what must not come back is the TEXT. Each of these was a live line of the saga.
  assert.doesNotMatch(runView, /resetRun\(/, 'RunView re-grew the destructive request')
  assert.doesNotMatch(runView, /saveRunStartOverIntent|clearRunStartOverIntent|loadRunStartOverIntent/,
    'RunView re-grew a second writer of the durable Start-over envelope')
  assert.doesNotMatch(runView, /START_OVER_AUTO_RETRY_LIMIT|START_OVER_REQUEST_TIMEOUT_MS/,
    'RunView re-grew the auto-retry budget or the request deadline')
  assert.doesNotMatch(runView, /startOverRequestRef|startOverAutoRetryRef/,
    'the in-flight request fence belongs to the saga, not to the view')
})
