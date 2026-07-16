import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('secondary panels distinguish loading, failed, ready-empty, stale, and superseded reads', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const timers = new Map()
  let timerId = 0
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
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
    request.resolve({
      ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
    })
    await Promise.resolve()
  })
  const click = async (button) => act(async () => button.dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true })))
  const button = text => [...document.querySelectorAll('button')].find(node => node.textContent.trim().startsWith(text))
  const render = async (Component, props = {}) => act(async () => root.render(React.createElement(Component, {
    onClose() {}, ...props,
  })))
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, panels] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/panels.jsx'),
    ])
    root = createRoot(document.getElementById('root'))

    await render(panels.AuthoringPanel)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading prompts files/)
    await reply(requests.at(-1), { detail: 'provider secret' }, 503)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /prompts files: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /provider secret|no files|no prompts dir/)
    await click(button('Retry'))
    await reply(requests.at(-1), { dir: null, files: [] })
    assert.match(document.body.textContent, /no prompts dir configured.*no files/)

    const beforeMemory = requests.length
    await render(panels.MemoryPanel)
    const memoryRequests = requests.slice(beforeMemory)
    assert.equal(memoryRequests.length, 2)
    await reply(memoryRequests.find(r => r.url.endsWith('/api/memory')), { detail: 'private path' }, 503)
    await reply(memoryRequests.find(r => r.url.endsWith('/api/knowledge')), { dir: null, files: [] })
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Cross-run memory: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /private path|No lessons yet/)
    await click(button('Knowledge'))
    assert.match(document.body.textContent, /No knowledge notes/)

    await render(panels.RegistryPanel, { state: { nodes: {}, promotions: [] } })
    await reply(requests.at(-1), { detail: 'database host' }, 503)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Cross-run leaderboard: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /database host|No runs in the registry/)
    await click(button('Retry'))
    await reply(requests.at(-1), [])
    assert.match(document.body.textContent, /No runs in the registry yet/)

    await render(panels.CrossRunPanel, { state: { task_id: 'demo', direction: 'max' } })
    await reply(requests.at(-1), { detail: 'offline' }, 503)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Cross-run results: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /0 comparable|No comparable runs/)
    await click(button('Retry'))
    await reply(requests.at(-1), [{
      run_id: 'run-1', task_id: 'demo', direction: 'max', best_metric: 1, nodes: 2, finished: true,
    }])
    await click(button('overlay trajectories'))
    await reply(requests.at(-1), { detail: 'run storage path' }, 503)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Trajectory overlay: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /run storage path/)

    await render(panels.GpuPanel)
    await reply(requests.at(-1), { detail: 'nvidia path' }, 503)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /GPU telemetry: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /nvidia path|No GPU/)
    await click(button('Retry'))
    await reply(requests.at(-1), { available: true, gpus: [] })
    assert.match(document.body.textContent, /No GPU devices reported/)
    await act(async () => [...timers.values()].at(-1)())
    await reply(requests.at(-1), { available: true, gpus: [{ name: 'new GPU', util: 1, mem_used: 2, mem_total: 8, temp: 3, power: 4 }] })
    const poll = [...timers.values()].at(-1)
    await act(async () => { poll(); poll() })
    const [older, newer] = requests.slice(-2)
    await reply(newer, { available: true, gpus: [{ name: 'latest GPU', util: 5, mem_used: 2, mem_total: 8, temp: 3, power: 4 }] })
    await reply(older, { available: true, gpus: [{ name: 'superseded GPU', util: 9, mem_used: 2, mem_total: 8, temp: 3, power: 4 }] })
    assert.match(document.body.textContent, /latest GPU/)
    assert.doesNotMatch(document.body.textContent, /superseded GPU/)
    await act(async () => poll())
    await reply(requests.at(-1), { detail: 'poll failed' }, 503)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Last loaded data; refresh failed.*Retry/)
    assert.match(document.body.textContent, /latest GPU/)
    assert.doesNotMatch(document.body.textContent, /poll failed/)
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
