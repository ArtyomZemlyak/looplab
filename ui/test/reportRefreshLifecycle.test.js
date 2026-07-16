import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const state = {
  run_id: 'demo', task_id: 'task', goal: 'report receipt', direction: 'min', phase: 'running',
  nodes: {}, best_node_id: null, reward_hacks: [], drifts: [], research: [], report: null,
}

test('report refresh is receipt-scoped, double-submit safe, and immune to an older safety timer', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/', pretendToBeVisual: true,
  })
  const realSetTimeout = globalThis.setTimeout
  const realClearTimeout = globalThis.clearTimeout
  const safetyTimers = []
  let fetches = 0
  let nextReceipt = 10
  let root
  let vite
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    location: dom.window.location,
    requestAnimationFrame: callback => realSetTimeout(callback, 0),
    cancelAnimationFrame: handle => realClearTimeout(handle),
    IS_REACT_ACT_ENVIRONMENT: true,
    fetch: async () => {
      fetches += 1
      const seq = nextReceipt++
      return { ok: true, json: async () => ({ ok: true, seq, content: { trigger: 'manual' } }) }
    },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    globalThis.setTimeout = (callback, delay, ...args) => {
      if (delay === 30000) {
        const handle = { callback: () => callback(...args), cleared: false }
        safetyTimers.push(handle)
        return handle
      }
      return realSetTimeout(callback, delay, ...args)
    }
    globalThis.clearTimeout = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) handle.cleared = true
      else realClearTimeout(handle)
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: ReportView }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/Report.jsx'),
    ])
    root = createRoot(document.querySelector('#root'))
    const render = observedSeq => React.act(async () => {
      root.render(React.createElement(ReportView, { state, runId: 'demo', observedSeq }))
      await Promise.resolve()
    })
    const button = () => [...document.querySelectorAll('button')]
      .find(element => /Refresh/.test(element.textContent))

    await render(9)
    await React.act(async () => {
      button().click()
      button().click()
      await Promise.resolve()
      await Promise.resolve()
    })
    assert.equal(fetches, 1, 'the synchronous request ref must reject a second click before render')
    assert.match(button().textContent, /Refreshing/)
    assert.equal(safetyTimers.length, 1)

    await render(10)
    assert.match(button().textContent, /Refresh report/)
    assert.equal(safetyTimers[0].cleared, true)

    await React.act(async () => {
      button().click()
      await Promise.resolve()
      await Promise.resolve()
    })
    assert.equal(fetches, 2)
    assert.match(button().textContent, /Refreshing/)
    assert.equal(safetyTimers.length, 2)

    await React.act(async () => { safetyTimers[0].callback(); await Promise.resolve() })
    assert.match(button().textContent, /Refreshing/,
      'a stale timer from request A must not clear request B')
    await render(11)
    assert.match(button().textContent, /Refresh report/)
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    if (vite) await vite.close()
    globalThis.setTimeout = realSetTimeout
    globalThis.clearTimeout = realClearTimeout
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
    dom.window.close()
  }
})
