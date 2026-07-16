import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('trace unavailable is an actionable alert while partial remains status', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
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
    const [{ createRoot }, { NodeTrace, TraceUnavailable }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/Inspector.jsx'),
    ])
    root = createRoot(document.getElementById('root'))

    let retries = 0
    await act(async () => root.render(React.createElement(TraceUnavailable, {
      label: 'Observation unavailable.', onRetry: () => { retries += 1 },
    })))
    const alert = document.querySelector('[role="alert"]')
    const retry = alert?.querySelector('button')
    assert.equal(alert?.textContent, 'Observation unavailable.Retry trace')
    assert.equal(retry?.type, 'button')
    await act(async () => retry.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })))
    assert.equal(retries, 1)

    await act(async () => root.render(React.createElement(NodeTrace, {
      spans: [], runId: 'demo', projection: { truncated: true },
    })))
    assert.equal(document.querySelector('[role="alert"]'), null)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /partial/)

    await act(async () => root.render(React.createElement(NodeTrace, {
      spans: [], runId: 'demo', projection: { unavailable: true },
      onRetry: () => { retries += 1 },
    })))
    assert.ok(document.querySelector('[role="alert"] button'))
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
