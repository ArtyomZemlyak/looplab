import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('missing Inspector summary exposes detail failure, safe retry, and focus recovery', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    fetch: url => new Promise(resolve => requests.push({ url: String(url), resolve })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  const reply = async (request, payload, status = 200) => act(async () => {
    request.resolve({ ok: status < 400, status, headers: { get: () => null }, json: async () => payload })
    await Promise.resolve()
  })
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
    root = createRoot(document.getElementById('root'))
    await act(async () => root.render(React.createElement(Inspector, {
      runId: 'demo', nodeId: 99, state: { nodes: {}, drifts: [], direction: 'max' },
      live: null, tab: 'Overview', setTab() {}, onToast() {},
    })))
    assert.match(document.querySelector('[role="status"]')?.textContent || '',
      /Loading experiment #99 details/)

    await reply(requests[0], { detail: 'password=must-not-render' }, 503)
    const retry = document.querySelector('[role="alert"] button')
    assert.ok(retry)
    assert.match(document.body.textContent, /Full node details could not be loaded/)
    assert.doesNotMatch(document.body.textContent, /password/)

    retry.focus()
    await act(async () => retry.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })))
    assert.match(document.querySelector('[role="status"]')?.textContent || '',
      /Loading experiment #99 details/)
    await reply(requests[1], { detail: 'token=must-not-render' }, 503)
    await act(async () => new Promise(resolve => setTimeout(resolve, 5)))
    assert.equal(document.activeElement, document.querySelector('[role="alert"]'))
    assert.doesNotMatch(document.body.textContent, /token/)

    const secondRetry = document.querySelector('[role="alert"] button')
    await act(async () => secondRetry.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })))
    await reply(requests[2], { id: 99 }, 200)
    await act(async () => new Promise(resolve => setTimeout(resolve, 5)))
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /invalid response.*Retry/)
    assert.equal(document.querySelector('[role="tablist"]'), null)
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
