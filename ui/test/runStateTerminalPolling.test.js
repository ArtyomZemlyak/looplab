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
  const probes = [
    lifecycle(),
    lifecycle({ seq: 13, event_count: 14, engine_running: true }),
  ]
  const calls = []
  const fetchStub = (input, options = {}) => {
    const url = String(input)
    calls.push(url)
    if (url.endsWith('/state')) return Promise.resolve(jsonResponse(stateFrame))
    if (url.endsWith('/lifecycle')) return Promise.resolve(jsonResponse(probes.shift()))
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
    assert.equal(calls.filter(url => url.endsWith('/events')).length, 0,
      'a known-terminal initial state must not open an immediately-terminal SSE request')

    await React.act(async () => {
      await new Promise(resolve => setTimeout(resolve, 95))
    })
    assert.equal(calls.filter(url => url.endsWith('/lifecycle')).length, 1)
    assert.equal(calls.filter(url => url.endsWith('/events')).length, 0)

    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    await React.act(async () => {
      await new Promise(resolve => setTimeout(resolve, 105))
    })
    assert.equal(calls.filter(url => url.endsWith('/lifecycle')).length, 1,
      'a hidden terminal tab must not keep probing')

    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    await React.act(async () => {
      await waitFor(() => calls.some(url => url.endsWith('/events')),
        'a changed lifecycle did not reopen the live stream')
    })
    assert.equal(calls.filter(url => url.endsWith('/lifecycle')).length, 2,
      'visibility resumes with one immediate compact probe')
    assert.equal(calls.filter(url => url.endsWith('/events')).length, 1)
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
})
