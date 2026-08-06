import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import { useRunState } from '../src/hooks.js'

const GENERATION = 'a'.repeat(64)
const stateFrame = {
  generation: GENERATION,
  seq: 12,
  event_count: 13,
  state: {
    run_id: 'demo', nodes: {}, finished: true, engine_running: false, phase: 'finished',
  },
}
const lifecycle = overrides => ({
  schema: 1, generation: GENERATION, seq: 12, event_count: 13,
  engine_running: false, ...overrides,
})
const jsonResponse = body => ({
  ok: true, status: 200, headers: { get: () => null }, json: async () => body,
})

async function waitFor(predicate, message, timeoutMs = 250) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise(resolve => setTimeout(resolve, 2))
  }
  assert.fail(message)
}

test('terminal run state uses a minute-scale visibility-paused probe before reopening SSE', async () => {
  const realSetTimeout = globalThis.setTimeout
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  let hidden = false
  Object.defineProperty(dom.window.document, 'hidden', {
    configurable: true, get: () => hidden,
  })
  // The probe answer is a variable the TEST arms, not a queue the CLOCK drains. The interval below
  // is rewritten 60s -> 80ms, so on a loaded box an extra tick can land inside any wall-clock window
  // this test waits out; with a queue that tick consumed the CHANGED lifecycle early and every count
  // after it shifted by one (observed: "2 !== 1" at the first probe assertion under six concurrent
  // agents). An unchanged answer is idempotent, so a stray tick now changes nothing it asserts.
  let probeAnswer = lifecycle()
  const calls = []
  const probeCount = () => calls.filter(url => url.endsWith('/lifecycle')).length
  const streamCount = () => calls.filter(url => url.endsWith('/events')).length
  const fetchStub = (input, options = {}) => {
    const url = String(input)
    calls.push(url)
    if (url.endsWith('/state')) return Promise.resolve(jsonResponse(stateFrame))
    if (url.endsWith('/lifecycle')) return Promise.resolve(jsonResponse(probeAnswer))
    if (url.endsWith('/events')) {
      return new Promise((_resolve, reject) => {
        options.signal?.addEventListener('abort', () => {
          const error = new Error('aborted')
          error.name = 'AbortError'
          reject(error)
        }, { once: true })
      })
    }
    throw new Error(`unexpected request: ${url}`)
  }
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    Event: dom.window.Event,
    location: dom.window.location,
    sessionStorage: dom.window.sessionStorage,
    fetch: fetchStub,
    setTimeout: (callback, delay, ...args) =>
      realSetTimeout(callback, delay === 60000 ? 80 : delay, ...args),
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
    let latest = null
    const Harness = () => {
      latest = useRunState('demo')
      return null
    }
    root = createRoot(document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness)) })
    await React.act(async () => {
      await waitFor(() => latest?.status === 'ready', 'initial terminal state did not settle')
    })
    assert.equal(latest.connected, true)
    assert.equal(streamCount(), 0,
      'a known-terminal initial state must not open an immediately-terminal SSE request')

    await React.act(async () => {
      await waitFor(() => probeCount() >= 1, 'a terminal run never probed its lifecycle at all')
    })
    // Deliberately a BOUND, not a count. "Exactly one tick lands in 95ms" is an artifact of the
    // rewritten interval and the box's load; what the minute-scale rule actually forbids is a tight
    // loop, and at 80ms per tick that would be dozens here — this still fails on one.
    assert.ok(probeCount() <= 3,
      `a minute-scale probe must not spin: ${probeCount()} lifecycle reads in one interval`)
    assert.equal(streamCount(), 0)

    // Snapshot at the moment of hiding: the property is that the count STOPS GROWING, which is
    // independent of how many ticks happened to fit before it.
    const probesBeforeHiding = probeCount()
    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    await React.act(async () => {
      await new Promise(resolve => setTimeout(resolve, 105))
    })
    assert.equal(probeCount(), probesBeforeHiding,
      'a hidden terminal tab must not keep probing')

    probeAnswer = lifecycle({ seq: 13, event_count: 14, engine_running: true })
    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    await React.act(async () => {
      await waitFor(() => streamCount() > 0, 'a changed lifecycle did not reopen the live stream')
    })
    assert.ok(probeCount() > probesBeforeHiding,
      'visibility resumes with an immediate compact probe rather than waiting out the interval')
    assert.equal(streamCount(), 1)
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
})
