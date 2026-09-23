// The Attention center reads two INDEPENDENT sources — the run feed and the Assistant's pending
// permissions — each on its own serialized poll with its own request deadline
// (`useAttention.js::ATTENTION_REQUEST_TIMEOUT_MS`). A hung source must reach ITS deadline without
// delaying or freezing its sibling, and must keep its last-good data until that deadline.
//
// DETERMINISTIC TIME (review 2026-09-22, UI-05). This test used to compress the 8 s deadline into
// a REAL 60 ms and observe inside a REAL 30 ms window, so what it asserted depended on how fast the
// box ran it: under load the observation outlasted the stand-in deadline and the "still last-good"
// assertions failed (measured on this 4-core box with 12-16 busy processes: 3 of 40 runs, in both
// halves). Now node:test's `mock.timers` owns `setTimeout`: the production deadline and poll delays
// run unchanged, a timer fires only when the test ticks the clock past it, and every observation is
// made at an exact clock reading — 1 ms before a deadline and at it. Responses still arrive through
// real promise turns (`settleUntil`), which a busy box slows down but cannot reorder.
import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import {
  ATTENTION_REQUEST_TIMEOUT_MS, PERMISSION_POLL_MS, useAttention,
} from '../src/useAttention.js'

const GENERATION = 'f'.repeat(64)
const firstRun = {
  id: 'a'.repeat(64), kind: 'finished', severity: 'success', run_id: 'run-a',
  generation: GENERATION, seq: 10, created: 1_700_000_000, active: false,
  browser: true, derived: false, node_id: null, node_generation: null,
}
const secondRun = { ...firstRun, id: 'b'.repeat(64), run_id: 'run-b', seq: 11 }
const currentPermission = {
  id: '1'.repeat(16), session: '2'.repeat(16), created: 1_700_000_000,
  expires_at: 4_000_000_000,
  action: { command: 'RAW_SECRET', scope: 'private/RAW_SECRET' },
  preview: 'preview:RAW_SECRET',
}
// `snapshot_id`, `stale` and `active_action_count` are required envelope fields — a page missing any
// of them is protocol-invalid and dropped WHOLE, which made the run source read stale immediately
// and the "retained until its own deadline" assertion fail on validation rather than on timing.
const page = items => ({
  schema: 1, generated_at: 1_700_000_100, items,
  snapshot_id: 'f'.repeat(64), stale: false, active_action_count: 0,
  truncated: false, next_cursor: null, partial: false,
})
const response = body => ({
  ok: true, status: 200, headers: { get: () => null }, json: async () => body,
})

// Let in-flight responses land: act-wrapped turns of the REAL event loop (`setImmediate` is not
// mocked), bounded by a COUNT of turns rather than a span of time. A response here is a chain of
// promise turns; nothing it waits on is a timer, so no clock is involved in reaching the predicate.
const turn = () => React.act(async () => { await new Promise(resolve => setImmediate(resolve)) })
async function settleUntil(predicate, message) {
  for (let turns = 0; turns < 100; turns += 1) {
    if (predicate()) return
    await turn()
  }
  assert.fail(message)
}
// The same turns with nothing to wait for: before asserting that a timer has NOT fired, give
// anything it would have caused every chance to land.
async function settleTurns(count = 10) {
  for (let turns = 0; turns < count; turns += 1) await turn()
}

test('a hung Attention source times out without delaying or freezing its sibling', async t => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const queues = { attention: [page([firstRun])], permissions: [{ pending: [] }] }
  const reads = { attention: 0, permissions: 0 }
  const fetchStub = async input => {
    const source = String(input).includes('/api/attention?') ? 'attention' : 'permissions'
    reads[source] += 1
    const next = queues[source].shift()
    assert.ok(next, `missing queued ${source} response`)
    if (next.never === true) return new Promise(() => {})
    return response(next)
  }
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    Event: dom.window.Event,
    location: dom.window.location,
    fetch: fetchStub,
    IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }

  let root
  try {
    const { createRoot } = await import('react-dom/client')
    // Every timer the hook arms from here on — deadlines, poll delays, retry backoff — is the
    // test's to fire. The clock starts at 0 and moves only on `tick`.
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const tick = ms => React.act(async () => { t.mock.timers.tick(ms) })
    let latest = null
    const Harness = () => {
      latest = useAttention({ intervalMs: 2_147_483_647 })
      return null
    }
    root = createRoot(document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness)) })
    await settleUntil(() => latest?.initialized, 'initial Attention reads did not settle')

    // The run source hangs; the permission source answers at once and then on its own 4 s cadence.
    queues.attention.push({ never: true })
    queues.permissions.push(...Array.from({ length: 3 }, () => ({ pending: [currentPermission] })))
    await React.act(async () => { latest.refresh() })
    await settleUntil(() => latest.permissions[0]?.requestId === currentPermission.id,
      'the healthy permission source was delayed by a hung run source')
    assert.equal(latest.permissionsStale, false)
    assert.equal(latest.runStale, false,
      'last-good run data is retained until the run source reaches its own deadline')
    assert.doesNotMatch(JSON.stringify(latest), /RAW_SECRET/)

    await tick(PERMISSION_POLL_MS)
    await settleUntil(() => reads.permissions === 3,
      'the permission source was frozen by a hung run source: its next poll never ran')
    assert.equal(latest.permissionsStale, false)
    await tick(ATTENTION_REQUEST_TIMEOUT_MS - PERMISSION_POLL_MS - 1)
    await settleTurns()
    assert.equal(latest.runStale, false,
      '1 ms before its deadline the hung run source still holds its last-good data')

    await tick(1)
    await settleUntil(() => latest.runStale === true,
      'the hung run source did not go stale at its own deadline')
    assert.equal(latest.permissionsStale, false)
    assert.equal(reads.permissions, 4, 'the sibling kept its own cadence through the whole hang')
    assert.equal(latest.runs[0]?.id, firstRun.id, 'a stale source keeps showing its last-good rows')

    // The same property in reverse: the permission source hangs, the run source answers at once.
    queues.attention.push(page([secondRun]))
    queues.permissions.push({ never: true })
    await React.act(async () => { latest.refresh() })
    await settleUntil(() => latest.runs[0]?.id === secondRun.id,
      'the healthy run source was delayed by a hung permission source')
    assert.equal(latest.runStale, false)
    assert.equal(latest.permissionsStale, false)

    await tick(ATTENTION_REQUEST_TIMEOUT_MS - 1)
    await settleTurns()
    assert.equal(latest.permissionsStale, false,
      '1 ms before its deadline the hung permission source still holds its last-good data')
    await tick(1)
    await settleUntil(() => latest.permissionsStale === true,
      'the hung permission source did not go stale at its own deadline')
    assert.equal(latest.runStale, false)
    assert.equal(reads.attention, 3,
      'the run source read three times: initially, the hung read, the reverse refresh')
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
})
