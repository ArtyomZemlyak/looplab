import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import { useAttention } from '../src/useAttention.js'

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
const page = items => ({
  schema: 1, generated_at: 1_700_000_100, items,
  truncated: false, next_cursor: null, partial: false,
})
const response = body => ({
  ok: true, status: 200, headers: { get: () => null }, json: async () => body,
})

async function waitFor(predicate, message, timeoutMs = 30) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.fail(message)
}

test('a hung Attention source times out without delaying or freezing its sibling', async () => {
  const realSetTimeout = globalThis.setTimeout
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const queues = {
    attention: [page([firstRun])],
    permissions: [{ pending: [] }],
  }
  const fetchStub = async input => {
    const source = String(input).includes('/api/attention?') ? 'attention' : 'permissions'
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
    setTimeout: (callback, delay, ...args) =>
      realSetTimeout(callback, delay === 8000 ? 40 : delay, ...args),
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
      latest = useAttention({ intervalMs: 2_147_483_647 })
      return null
    }
    root = createRoot(document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness)) })
    await React.act(async () => {
      await waitFor(() => latest?.initialized, 'initial Attention reads did not settle')
    })

    queues.attention.push({ never: true })
    queues.permissions.push({ pending: [currentPermission] })
    await React.act(async () => { latest.refresh() })
    await React.act(async () => {
      await waitFor(() => latest.permissions[0]?.requestId === currentPermission.id,
        'the healthy permission source was delayed by a hung run source')
    })
    assert.equal(latest.permissionsStale, false)
    assert.equal(latest.runStale, false,
      'last-good run data is retained until the run source reaches its own deadline')
    assert.doesNotMatch(JSON.stringify(latest), /RAW_SECRET/)

    await React.act(async () => {
      await new Promise(resolve => setTimeout(resolve, 55))
    })
    assert.equal(latest.runStale, true)
    assert.equal(latest.permissionsStale, false)

    queues.attention.push(page([secondRun]))
    queues.permissions.push({ never: true })
    await React.act(async () => { latest.refresh() })
    await React.act(async () => {
      await waitFor(() => latest.runs[0]?.id === secondRun.id,
        'the healthy run source was delayed by a hung permission source')
    })
    assert.equal(latest.runStale, false)
    assert.equal(latest.permissionsStale, false)

    await React.act(async () => {
      await new Promise(resolve => setTimeout(resolve, 55))
    })
    assert.equal(latest.runStale, false)
    assert.equal(latest.permissionsStale, true)
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
})
