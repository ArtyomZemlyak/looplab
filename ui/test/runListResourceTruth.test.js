import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('RunList resources ignore late successes, late failures, and unmounted settlements', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const pending = []
  const read = () => new Promise((resolve, reject) => pending.push({ resolve, reject }))
  let load
  let renders = 0
  let root
  let mounted = false
  let vite
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { useResource }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/RunList.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    mounted = true

    function Harness() {
      const [data, state, next] = useResource(read, 'initial')
      load = next
      renders += 1
      return React.createElement('output', { 'data-state': state }, data)
    }
    await act(async () => root.render(React.createElement(Harness)))

    let older
    let newer
    await act(async () => {
      older = load(); newer = load(); await Promise.resolve()
    })
    assert.equal(pending.length, 2)
    await act(async () => { pending[1].resolve('new'); await newer })
    assert.equal(document.querySelector('output').textContent, 'new')
    assert.equal(document.querySelector('output').dataset.state, 'ready')
    await act(async () => { pending[0].resolve('old'); await older })
    assert.equal(document.querySelector('output').textContent, 'new',
      'an older poll cannot resurrect its snapshot')

    let staleFailure
    let freshSuccess
    await act(async () => {
      staleFailure = load(); freshSuccess = load(); await Promise.resolve()
    })
    await act(async () => { pending[3].resolve('freshest'); await freshSuccess })
    await act(async () => { pending[2].reject(new Error('stale')); await staleFailure })
    assert.equal(document.querySelector('output').textContent, 'freshest')
    assert.equal(document.querySelector('output').dataset.state, 'ready',
      'an older failure cannot downgrade the latest ready snapshot')

    let late
    await act(async () => { late = load(); await Promise.resolve() })
    await act(async () => root.unmount())
    mounted = false
    const rendersAtUnmount = renders
    await act(async () => { pending[4].resolve('after-unmount'); await late })
    assert.equal(renders, rendersAtUnmount)
  } finally {
    if (root && mounted) await act(async () => root.unmount())
    if (vite) await vite.close()
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
})
