import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('metric curves distinguish loading, empty, failed, stale, and serialize refresh reads', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const timers = new Map()
  let timerId = 0
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    fetch: url => new Promise(resolve => requests.push({ url: String(url), resolve })),
    setInterval: callback => { const id = ++timerId; timers.set(id, callback); return id },
    clearInterval: id => timers.delete(id),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  const reply = async (request, payload, status = 200) => act(async () => {
    request.resolve({ ok: status < 400, status, headers: { get: () => null }, json: async () => payload })
    await Promise.resolve()
  })
  const poll = async () => act(async () => [...timers.values()].at(-1)?.())
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { MetricCurves }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/Inspector.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    const view = (nodeId, attempt = 0) => React.createElement(MetricCurves, {
      key: `demo:${nodeId}:${attempt}`, runId: 'demo', nodeId, attempt, status: 'pending',
    })

    await act(async () => root.render(view(1)))
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading metric curves/)
    assert.equal(requests.length, 1)
    assert.match(requests[0].url, /\/nodes\/1\/metrics\?attempt=0$/)

    await reply(requests[0], { detail: 'offline' }, 503)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Metric curves unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /no metric curves logged yet/)

    await act(async () => document.querySelector('[role="alert"] button')
      .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })))
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading metric curves/)
    await reply(requests[1], { node_id: 1, attempt: 0, metrics: {} })
    assert.match(document.body.textContent, /no metric curves logged yet/)

    await poll()
    await reply(requests[2], {
      node_id: 1, attempt: 0, metrics: { 'train/loss': [{ step: 1, value: 0.4 }] },
    })
    assert.match(document.body.textContent, /train.*1 metric/)

    await poll(); await poll()
    assert.equal(requests.length, 4, 'overlapping poll ticks queue instead of starting a second read')
    await reply(requests[3], {
      node_id: 1, attempt: 0, metrics: { 'old/loss': [{ step: 1, value: 9 }] },
    })
    assert.equal(requests.length, 5, 'the queued refresh starts after the active read settles')
    await reply(requests[4], {
      node_id: 1, attempt: 0, metrics: { 'eval/score': [{ step: 2, value: 0.8 }] },
    })
    assert.match(document.body.textContent, /eval.*1 metric/)
    assert.doesNotMatch(document.body.textContent, /old.*1 metric/)

    await poll()
    await reply(requests[5], { detail: 'offline' }, 503)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Last loaded metric curves; refresh failed.*Retry/)
    assert.match(document.body.textContent, /eval.*1 metric/)

    await act(async () => document.querySelector('[role="status"] button')
      .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })))
    await act(async () => root.render(view(2)))
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading metric curves/)
    assert.doesNotMatch(document.body.textContent, /eval.*1 metric/)
    await reply(requests[6], {
      node_id: 1, attempt: 0, metrics: { 'late/old-node': [{ step: 1, value: 3 }] },
    })
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading metric curves/)
    assert.match(requests[7].url, /\/nodes\/2\/metrics\?attempt=0$/)
    await reply(requests[7], { node_id: 2, attempt: 0, metrics: {} })
    assert.match(document.body.textContent, /no metric curves logged yet/)
    assert.doesNotMatch(document.body.textContent, /late.*1 metric/)

    // Same node id, new lifecycle: the key resets all visible evidence and the attempt receipt is
    // required. A late response from the superseded attempt cannot repopulate the replacement node.
    await poll()
    await act(async () => root.render(view(2, 1)))
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading metric curves/)
    assert.match(requests[9].url, /\/nodes\/2\/metrics\?attempt=1$/)
    await reply(requests[8], {
      node_id: 2, attempt: 0, metrics: { 'stale/reset': [{ step: 1, value: 7 }] },
    })
    assert.doesNotMatch(document.body.textContent, /stale.*1 metric/)
    await reply(requests[9], {
      node_id: 2, attempt: 1, metrics: { 'fresh/reset': [{ step: 1, value: 0.2 }] },
    })
    assert.match(document.body.textContent, /fresh.*1 metric/)
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})
