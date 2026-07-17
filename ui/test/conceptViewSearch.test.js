// View 1 ConceptView tree-search wiring test. The filter logic itself is unit-tested in
// conceptSearch.test.js; here we verify the component wires it to the header input + row rendering
// (auto-expand of ancestors, <mark> highlight, experiment-match evidence, no-match state). `node --test`.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const GENERATION_A = 'a'.repeat(64)
const lenses = [{ name: 'is_a', label: 'Family / is-a', rels: ['is_a'], kind: 'path' }]
const limits = {
  membership_nodes: 2048, concepts_per_node: 64, memberships: 8192,
  tree_nodes: 4096, edges: 2048, edge_endpoints: 4096,
}

// Minimal valid single-path frame (mirrors conceptViewResourceTruth's framePayload) — a hierarchy
// loss > loss/contrastive > loss/contrastive/dcl with the leaf tagged by one evaluated experiment.
function framePayload(id) {
  const treeIds = id.split('/').map((_, index, parts) => parts.slice(0, index + 1).join('/'))
  const treeNodes = Object.fromEntries(treeIds.map((treeId, index) => [treeId, {
    parent: index === 0 ? null : treeIds[index - 1], depth: index,
    children: index + 1 < treeIds.length ? [treeIds[index + 1]] : [],
    tagged: treeId === id,
  }]))
  const refs = [{
    node_id: 0, node_generation: 0, metric: 0.7, metric_kind: 'robust_metric',
    status: 'evaluated', feasible: true, is_best: true, membership_provenance: 'researcher-authored',
  }]
  return {
    schema: 1, status: 'complete', run_id: 'run#one', generation: GENERATION_A,
    requested_seq: null, captured_seq: 4, max_seq: 4, historical: false,
    lens: 'is_a', effective_lens: 'is_a', requested_lens: 'is_a',
    requested_lens_spec: { name: 'is_a', rels: ['is_a'], kind: 'path', registration: 'shipped' },
    lens_contract: { requested: 'is_a', effective: 'is_a', registration: 'shipped', fallback: null },
    lenses, edges_present: false, lens_edges_present: false,
    touch: { [id]: 1 },
    tree: { lens: 'is_a', roots: [treeIds[0]], nodes: treeNodes },
    metrics: { baseline: 0.5, direction: 'max', rows: { [id]: {
      touched: 1, evaluated: 1, best: 0.7, mean: 0.7, worst: 0.7,
      delta_best: 0.2, delta_mean: 0.2, first_touch: 0,
    } } },
    experiment_refs: { [id]: refs },
    authoritative: true,
    authority: { authoritative: true, source_authoritative: true, complete: true,
      scope: 'captured_recoverable_event_prefix', semantic_claims_verified: false },
    provenance: { source: 'events.jsonl', projection: 'event_log_fold',
      membership_semantics: 'recorded_claims', membership_counts: { 'researcher-authored': 1 } },
    complete: true,
    completeness: {
      complete: true, truncated: false, reasons: [], limits,
      source: { membership_nodes: 1, edges: 0 },
      included: { membership_nodes: 1, memberships: 1, concepts: 1, tree_nodes: treeIds.length,
        edges: 0, experiment_refs: 1 },
      source_integrity: { complete: true, generation_identified: true },
    },
  }
}

test('ConceptView tree search filters, auto-expands, marks, and matches experiments', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const nativeSetTimeout = globalThis.setTimeout
  const nativeClearTimeout = globalThis.clearTimeout
  const deadlines = new Map()
  let timerId = 0
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => nativeSetTimeout(callback, 0),
    cancelAnimationFrame: handle => nativeClearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise((resolve, reject) => requests.push({
      url: String(url), options, resolve, reject,
    })),
    setTimeout: (callback, delay, ...args) => {
      if (delay !== 12_000) return nativeSetTimeout(callback, delay, ...args)
      const id = ++timerId; deadlines.set(id, callback); return id
    },
    clearTimeout: id => { if (!deadlines.delete(id)) nativeClearTimeout(id) },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root, vite
  const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
  const setInput = (input, value) => act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value)
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
    await settle()
  })
  const conceptRows = () => [...document.querySelectorAll('.cv-crow')]
  const searchInput = () => document.querySelector('.cv-search .cs-input')
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
    const state = {
      direction: 'max', engine_running: true, best_node_id: 0,
      node_concepts: { 0: ['loss/contrastive/dcl'] }, concept_consolidation: {}, concept_edges: {},
      node_concept_provenance: { 0: 'researcher-authored' },
      nodes: { 0: { id: 0, attempt: 0, status: 'evaluated', idea: {}, metric: 0.7,
        confirmed_mean: null, feasible: true } },
    }
    await act(async () => {
      root.render(React.createElement(conceptModule.default, {
        runId: 'run#one', generation: GENERATION_A, sequence: null, state, onPickNode() {},
      }))
      await settle()
    })
    // answer the /concepts fetch with a valid 3-level path frame
    assert.ok(requests.length >= 1, 'expected a /concepts request')
    const payload = framePayload('loss/contrastive/dcl')
    payload.experiment_refs['loss/contrastive/dcl'].push({
      node_id: 1, node_generation: 0, metric: null, metric_kind: 'robust_metric',
      status: 'failed', feasible: null, is_best: false, membership_provenance: 'classifier-v1',
    })
    payload.touch['loss/contrastive/dcl'] = 2
    payload.metrics.rows['loss/contrastive/dcl'].touched = 2
    payload.completeness.source.membership_nodes = 2
    payload.completeness.included.membership_nodes = 2
    payload.completeness.included.memberships = 2
    payload.completeness.included.experiment_refs = 2
    payload.provenance.membership_counts['classifier-v1'] = 1
    await act(async () => {
      requests[0].resolve({ ok: true, status: 200, headers: { get: () => null },
        json: async () => payload })
      await settle()
    })
    assert.ok(searchInput(), 'search input rendered in the tree header')
    // collapsed by default -> only the root concept row
    assert.deepEqual(conceptRows().map(r => r.querySelector('.cv-cid').textContent), ['loss'])

    // search "dcl" -> auto-expands the whole path, marks the leaf
    await setInput(searchInput(), 'dcl')
    assert.deepEqual(conceptRows().map(r => r.querySelector('.cv-cid').textContent),
      ['loss', 'contrastive', 'dcl'])
    const marks = [...document.querySelectorAll('.cv-cid mark')].map(m => m.textContent)
    assert.deepEqual(marks, ['dcl'])
    assert.ok(document.querySelector('.cv-crow.hit'), 'the matched concept row is emphasized')

    // search a status -> matches the experiment, auto-opens its evidence under the path
    await setInput(searchInput(), 'failed')
    assert.ok(document.querySelector('.cv-erow'), 'the matching experiment row is shown')
    assert.equal(document.querySelectorAll('.cv-erow').length, 1,
      'automatic evidence opening narrows to the matching reference')
    assert.equal(conceptRows().length, 3, 'the path to the tagged concept stays visible')
    await act(async () => {
      document.querySelector('.cv-badge.btn').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await settle()
    })
    assert.equal(document.querySelectorAll('.cv-erow').length, 2,
      'manual expansion widens filtered evidence to every reference behind the badge')

    // no match -> explicit empty state, no concept rows
    await setInput(searchInput(), 'zzznope')
    assert.equal(conceptRows().length, 0)
    assert.ok(document.querySelector('.cv-nomatch'), 'no-match message shown')

    // clearing restores the unfiltered (collapsed) tree
    await setInput(searchInput(), '')
    assert.deepEqual(conceptRows().map(r => r.querySelector('.cv-cid').textContent), ['loss'])
    assert.equal(document.querySelector('mark'), null)
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
})
