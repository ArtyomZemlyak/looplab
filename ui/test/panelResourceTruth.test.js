import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('secondary panels validate reads and serialize poll/retry recovery without hiding last-good data', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const timers = new Map()
  const requestTimeouts = new Map()
  const nativeSetTimeout = globalThis.setTimeout
  const nativeClearTimeout = globalThis.clearTimeout
  let timerId = 0
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => nativeSetTimeout(callback, 0),
    cancelAnimationFrame: handle => nativeClearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise(resolve => requests.push({ url: String(url), options, resolve })),
    setTimeout: (callback, delay, ...args) => {
      if (delay !== 15_000) return nativeSetTimeout(callback, delay, ...args)
      const id = ++timerId; requestTimeouts.set(id, callback); return id
    },
    clearTimeout: id => { if (!requestTimeouts.delete(id)) nativeClearTimeout(id) },
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

    // JSON identity is collision-free for delimiter-bearing ids and changes with every rendered label.
    const collisionA = [{ run_id: 'a,b', label: 'c' }, { run_id: 'd', label: null }]
    const collisionB = [{ run_id: 'a', label: 'b,c' }, { run_id: 'd', label: null }]
    assert.notEqual(panels.crossRunTrajectoryKey(collisionA, 'max', 'task'),
      panels.crossRunTrajectoryKey(collisionB, 'max', 'task'))
    assert.notEqual(panels.crossRunTrajectoryKey(collisionA, 'max', 'task'),
      panels.crossRunTrajectoryKey([{ ...collisionA[0], label: 'changed' }, collisionA[1]], 'max', 'task'))

    await render(panels.AuthoringPanel)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading prompts files/)
    assert.doesNotMatch(document.body.textContent, /select a file to edit/)
    await reply(requests.at(-1), null)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /prompts files: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /no files|no prompts dir|select a file to edit/)
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    await reply(requests.at(-1), { dir: null, files: {} })
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /prompts files: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /select a file to edit/)
    await click(button('Retry'))
    await reply(requests.at(-1), { dir: null, files: [] })
    assert.match(document.body.textContent, /no prompts dir configured.*no files.*select a file to edit/)
    await click(button('skills'))
    assert.doesNotMatch(document.body.textContent, /select a file to edit|no files/)
    await reply(requests.at(-1), { dir: null, files: [{ name: 'bad.md', text: null }] })
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /skills files: Unavailable/)
    assert.doesNotMatch(document.body.textContent, /select a file to edit/)

    const beforeMemory = requests.length
    await render(panels.MemoryPanel)
    const memoryRequests = requests.slice(beforeMemory)
    assert.equal(memoryRequests.length, 2)
    await reply(memoryRequests.find(r => r.url.endsWith('/api/memory')),
      { dir: null, cases: {}, lessons: [], notes: [] })
    await reply(memoryRequests.find(r => r.url.endsWith('/api/knowledge')), { dir: null, files: {} })
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Cross-run memory: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /No lessons yet/)
    await click(button('Knowledge'))
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Knowledge notes: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /No knowledge notes/)
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    await reply(requests.at(-1), { dir: null, files: [] })
    assert.match(document.body.textContent, /No knowledge notes/)
    await click(button('Lessons'))
    await click(button('Retry'))
    await reply(requests.at(-1), { dir: null, cases: [], lessons: [], notes: [] })
    assert.match(document.body.textContent, /No lessons yet/)

    await render(panels.RegistryPanel, { state: { nodes: {}, promotions: [] } })
    await reply(requests.at(-1), {})
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Cross-run leaderboard: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /No runs in the registry/)
    await click(button('Retry'))
    await reply(requests.at(-1), [])
    assert.match(document.body.textContent, /No runs in the registry yet/)

    await render(panels.CrossRunPanel, { state: { task_id: 'demo', direction: 'max' } })
    await reply(requests.at(-1), [{ run_id: 'malformed' }])
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Cross-run results: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /0 comparable|No comparable runs/)
    await click(button('Retry'))
    await reply(requests.at(-1), [{
      run_id: 'run-1', task_id: 'demo', direction: 'max', best_metric: 1, best_confirmed: null,
      nodes: 2, finished: true, phase: 'finished', label: 'Primary',
    }])
    assert.equal(document.querySelector('select[aria-label="Comparable task"]')?.className, 'text')
    assert.ok(document.querySelector('.panel-resource-toolbar'))
    await click(button('Overlay trajectories'))
    await reply(requests.at(-1), { state: { nodes: [] } })
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /Trajectory overlay: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /no comparable run trajectories/)
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    await reply(requests.at(-1), { state: { nodes: { 0: { id: 0, metric: 1, feasible: true } } } })
    assert.match(document.body.textContent, /Cross-run trajectories.*Primary/)

    const trajectoryRows = [{ run_id: 'label-run', label: 'Before' }]
    await render(panels.CrossRunTrajectories, { rows: trajectoryRows, dir: 'max', task: 'demo' })
    const oldLabelRequest = requests.at(-1)
    await render(panels.CrossRunTrajectories, {
      rows: [{ ...trajectoryRows[0], label: 'After' }], dir: 'max', task: 'demo',
    })
    const newLabelRequest = requests.at(-1)
    assert.notEqual(newLabelRequest, oldLabelRequest, 'a rendered label change must reload the keyed trajectory')
    await reply(oldLabelRequest, { state: { nodes: { 0: { id: 0, metric: 9, feasible: true } } } })
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Loading Trajectory overlay/)
    await reply(newLabelRequest, { state: { nodes: { 0: { id: 0, metric: 2, feasible: true } } } })
    assert.match(document.body.textContent, /Cross-run trajectories.*After/)
    assert.doesNotMatch(document.body.textContent, /Before/)

    await render(panels.GpuPanel)
    const hungRequest = requests.at(-1)
    assert.equal(hungRequest.options.signal.aborted, false)
    await act(async () => [...requestTimeouts.values()].at(-1)())
    assert.equal(hungRequest.options.signal.aborted, true)
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /GPU telemetry: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /No GPU/)
    let before = requests.length
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    assert.equal(requests.length, before + 1)
    const poll = [...timers.values()].at(-1)
    await act(async () => { poll(); poll() })
    assert.equal(requests.length, before + 1, 'poll ticks must skip a manual request in flight')
    assert.match(requests.at(-1).url, /\/api\/gpu$/)
    await reply(hungRequest, { available: true, gpus: [{
      name: 'late timed-out GPU', util: 9, mem_used: 9, mem_total: 9, temp: 9, power: 9,
    }] })
    assert.equal(button('Retrying…')?.disabled, true, 'late timed-out completion cannot settle the retry')
    await reply(requests.at(-1), { available: 'yes', gpus: [] })
    assert.match(document.querySelector('[role="alert"]')?.textContent || '', /GPU telemetry: Unavailable.*Retry/)
    assert.doesNotMatch(document.body.textContent, /late timed-out GPU|No GPU/)

    before = requests.length
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    assert.equal(requests.length, before + 1)
    await reply(requests.at(-1), { available: false })
    assert.match(document.body.textContent, /No GPU \/ nvidia-smi/)

    before = requests.length
    await act(async () => { poll(); poll() })
    assert.equal(requests.length, before + 1, 'two ticks while pending must create one request')
    await reply(requests.at(-1), { detail: 'poll failed' }, 503)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Last loaded data; refresh failed.*Retry/)
    assert.match(document.body.textContent, /No GPU \/ nvidia-smi/,
      'stale available:false last-good result stays visible')
    assert.doesNotMatch(document.body.textContent, /poll failed/)

    before = requests.length
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Retrying… Last loaded data remains visible/)
    assert.match(document.body.textContent, /No GPU \/ nvidia-smi/)
    await act(async () => poll())
    assert.equal(requests.length, before + 1, 'manual stale retry owns the single flight')
    const hungStaleRetry = requests.at(-1)
    await act(async () => [...requestTimeouts.values()].at(-1)())
    assert.equal(hungStaleRetry.options.signal.aborted, true)
    assert.equal(button('Retry')?.disabled, false, 'timeout must re-enable exact manual retry')
    assert.match(document.body.textContent, /No GPU \/ nvidia-smi/)
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    await reply(requests.at(-1), { available: true, gpus: [] })
    assert.match(document.body.textContent, /No GPU devices reported/)

    await act(async () => poll())
    await reply(requests.at(-1), { available: true, gpus: [{ name: {}, util: 1 }] })
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Last loaded data; refresh failed/)
    assert.match(document.body.textContent, /No GPU devices reported/,
      'stale empty-device last-good result stays visible after malformed HTTP 200')
    await click(button('Retry'))
    assert.equal(button('Retrying…')?.disabled, true)
    assert.match(document.body.textContent, /No GPU devices reported/)
    await reply(requests.at(-1), { available: true, gpus: [{
      name: 'current GPU', util: 5, mem_used: 2, mem_total: 8, temp: 3, power: 4,
    }] })
    assert.match(document.body.textContent, /current GPU/)
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
