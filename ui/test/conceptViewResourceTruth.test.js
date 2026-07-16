import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const emptyPayload = {
  lens: 'is_a', lenses: [{ name: 'is_a', label: 'Family · is-a' }], edges_present: false,
  tree: { roots: [], nodes: {} }, metrics: { baseline: null, rows: {} },
}
const conceptPayload = id => ({
  lens: 'is_a', lenses: [{ name: 'is_a', label: 'Family · is-a' }], edges_present: false,
  tree: { roots: [id], nodes: { [id]: { children: [], tagged: true } } },
  metrics: { baseline: 0.5, rows: { [id]: {
    touched: 1, evaluated: 1, best: 0.7, mean: 0.7,
    delta_best: 0.2, delta_mean: 0.2, first_touch: 0,
  } } },
})

test('ConceptView fences, retries and preserves truthful last-good resource states', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const deadlines = new Map()
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
      if (delay !== 12_000) return nativeSetTimeout(callback, delay, ...args)
      const id = ++timerId; deadlines.set(id, callback); return id
    },
    clearTimeout: id => { if (!deadlines.delete(id)) nativeClearTimeout(id) },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
  const reply = (request, payload, status = 200) => act(async () => {
    request.resolve({ ok: status < 400, status, headers: { get: () => null }, json: async () => payload })
    await settle()
  })
  const click = button => act(async () => {
    button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); await settle()
  })
  const button = text => [...document.querySelectorAll('button')]
    .find(node => node.textContent.trim() === text)
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, conceptModule] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ConceptView.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    let picked = null
    let state = {
      direction: 'max', engine_running: true,
      node_concepts: { 0: ['loss/a'] }, concept_consolidation: {}, concept_edges: {},
      nodes: { 0: { id: 0, idea: {}, metric: 0.7, confirmed_mean: null, feasible: true } },
    }
    const render = () => act(async () => {
      root.render(React.createElement(conceptModule.default, {
        runId: 'run#one', state, onPickNode: id => { picked = id },
      })); await settle()
    })

    await render()
    assert.match(document.querySelector('[role="status"]').textContent, /Building the concept view/)
    assert.equal(document.querySelector('[aria-label="Visible metric columns"]'), null)
    assert.match(requests[0].url, /\/api\/runs\/run%23one\/concepts\?lens=is_a$/)
    assert.ok(requests[0].options.signal instanceof AbortSignal)
    await reply(requests[0], emptyPayload)
    assert.match(document.body.textContent, /No concepts have been tagged yet.*Experiments.*Concept hierarchy.*Outcome comparison/s)
    assert.equal(document.querySelector('[aria-label="Visible metric columns"]'), null,
      'empty data must not expose irrelevant table controls')

    await act(async () => { button('Refresh concepts').click(); button('Refresh concepts').click(); await settle() })
    assert.equal(requests.length, 2, 'double refresh remains one request')
    assert.equal(button('Refreshing…').disabled, true)
    await act(async () => { [...deadlines.values()].at(-1)(); await settle() })
    assert.equal(requests[1].options.signal.aborted, true)
    assert.match(document.querySelector('[role="alert"]').textContent,
      /No concepts have been tagged yet.*Refresh failed.*last loaded empty result/s)
    await reply(requests[1], conceptPayload('late/wrong'))
    assert.doesNotMatch(document.body.textContent, /late\/wrong/,
      'late timed-out completion cannot replace last-good data')

    await click(button('Refresh concepts'))
    await reply(requests[2], conceptPayload('loss/a'))
    assert.match(document.body.textContent, /Concept tree.*1 concepts/s)
    assert.equal(document.querySelector('.cv-cid')?.getAttribute('title'), 'loss/a')
    await click(document.querySelector('button[aria-label="Expand loss/a"]'))
    const experiment = document.querySelector('button[aria-label="Open experiment 0 in Inspector"]')
    assert.equal(experiment?.tagName, 'BUTTON')
    assert.equal(experiment?.getAttribute('role'), null, 'native button owns Enter and Space semantics')
    await click(experiment)
    assert.equal(picked, 0)
    const selectedColumns = [...document.querySelectorAll('.cv-col[aria-pressed="true"]')]
    assert.equal(selectedColumns.length, 3)
    await click(selectedColumns[0]); await click(selectedColumns[1])
    assert.equal(document.querySelectorAll('.cv-col[aria-pressed="true"]').length, 1)
    assert.equal(document.querySelector('.cv-col[aria-pressed="true"]').disabled, true,
      'the last metric column cannot be removed and break table geometry')

    let before = requests.length
    state = { ...state, engine_running: false }
    await render()
    assert.equal(requests.length, before, 'liveness-only state changes do not refetch concepts')
    state = { ...state, node_concepts: { 0: ['architecture/moe'] } }
    await render()
    assert.equal(requests.length, before + 1, 'same-count retag refreshes the projection')
    assert.match(document.body.textContent, /Refreshing concepts/)
    assert.equal(document.querySelector('.cv-cid')?.getAttribute('title'), 'loss/a',
      'last-good data stays visible while a semantic refresh is pending')
    await reply(requests.at(-1), conceptPayload('architecture/moe'))
    assert.equal(document.querySelector('.cv-cid')?.getAttribute('title'), 'architecture/moe')

    before = requests.length
    state = { ...state, nodes: { 0: { ...state.nodes[0], metric: 0.9 } } }
    await render()
    assert.equal(requests.length, before + 1, 'metric-only changes refresh baseline and rollups')
    await reply(requests.at(-1), { ...emptyPayload, metrics: {} })
    assert.match(document.querySelector('[role="alert"]').textContent, /last loaded concept view; refresh failed/i)
    assert.equal(document.querySelector('.cv-cid')?.getAttribute('title'), 'architecture/moe')
    assert.doesNotMatch(document.body.textContent, /No concepts have been tagged yet/,
      'malformed HTTP 200 never masquerades as an authoritative empty result')
  } finally {
    if (root) await act(async () => { root.unmount(); await settle() })
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})
