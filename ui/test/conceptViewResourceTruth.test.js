import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const lenses = [
  { name: 'is_a', label: 'Family / is-a', rels: ['is_a'], kind: 'path' },
  { name: 'uses', label: 'Usage / uses', rels: ['uses'], kind: 'edge' },
]
const GENERATION_A = 'a'.repeat(64)
const GENERATION_B = 'b'.repeat(64)
const limits = {
  membership_nodes: 2048, concepts_per_node: 64, memberships: 8192,
  tree_nodes: 4096, edges: 2048, edge_endpoints: 4096,
}

function framePayload({ id = null, runId = 'run#one', generation = GENERATION_A,
  requestedLens = 'is_a', effectiveLens = requestedLens, derived = false,
  requestedRels = null, requestedKind = null, nodeGeneration = 0,
  requestedSeq = null, capturedSeq = 4, maxSeq = capturedSeq,
  complete = true, sourceAuthoritative = true, truncated = false,
  reasons = [], edgesPresent = false, lensEdgesPresent = false } = {}) {
  const hasConcept = id !== null
  const authoritative = complete && sourceAuthoritative
  const sourceIntegrity = sourceAuthoritative || generation === null
  const refs = hasConcept ? [{
    node_id: 0, node_generation: nodeGeneration, metric: 0.7, metric_kind: 'robust_metric',
    status: 'evaluated', feasible: true, is_best: true,
    membership_provenance: 'researcher-authored',
  }] : []
  const registration = derived ? 'ephemeral-validated' : 'shipped'
  const rels = requestedRels || (requestedLens === 'is_a' ? ['is_a'] : ['uses'])
  const kind = requestedKind || (requestedLens === 'is_a' ? 'path' : 'edge')
  const edgeTree = kind === 'edge' && requestedLens === effectiveLens
  const hierarchyIds = hasConcept && !edgeTree
    ? id.split('/').map((_, index, parts) => parts.slice(0, index + 1).join('/')) : []
  const treeIds = hasConcept ? (edgeTree ? [id] : hierarchyIds) : []
  const treeNodes = Object.fromEntries(treeIds.map((treeId, index) => [treeId, {
    parent: index === 0 ? null : treeIds[index - 1], depth: index,
    children: index + 1 < treeIds.length ? [treeIds[index + 1]] : [],
    tagged: treeId === id, ...(edgeTree ? { cross_parents: [] } : {}),
  }]))
  return {
    schema: 1, status: complete ? 'complete' : 'partial', run_id: runId, generation,
    requested_seq: requestedSeq, captured_seq: capturedSeq, max_seq: maxSeq,
    historical: capturedSeq < maxSeq,
    lens: effectiveLens, effective_lens: effectiveLens, requested_lens: requestedLens,
    requested_lens_spec: { name: requestedLens, rels, kind, registration },
    lens_contract: { requested: requestedLens, effective: effectiveLens, registration,
      fallback: requestedLens === effectiveLens ? null : 'no_matching_edges' },
    lenses, edges_present: edgesPresent, lens_edges_present: lensEdgesPresent,
    touch: hasConcept ? { [id]: 1 } : {},
    tree: { lens: effectiveLens, roots: hasConcept ? [treeIds[0]] : [], nodes: treeNodes },
    metrics: { baseline: hasConcept ? 0.5 : null, direction: 'max', rows: hasConcept ? { [id]: {
      touched: 1, evaluated: 1, best: 0.7, mean: 0.7,
      worst: 0.7, delta_best: 0.2, delta_mean: 0.2, first_touch: 0,
    } } : {} },
    experiment_refs: hasConcept ? { [id]: refs } : {},
    authoritative,
    authority: { authoritative, source_authoritative: sourceAuthoritative, complete,
      scope: 'captured_recoverable_event_prefix', semantic_claims_verified: false },
    provenance: { source: 'events.jsonl', projection: 'event_log_fold',
      membership_semantics: 'recorded_claims',
      membership_counts: hasConcept ? { 'researcher-authored': 1 } : {} },
    complete,
    completeness: {
      complete, truncated, reasons, limits,
      source: { membership_nodes: hasConcept ? 1 : 0, edges: edgesPresent ? 1 : 0 },
      included: { membership_nodes: hasConcept ? 1 : 0, memberships: hasConcept ? 1 : 0,
        concepts: hasConcept ? 1 : 0, tree_nodes: treeIds.length,
        edges: edgesPresent ? 1 : 0, experiment_refs: hasConcept ? 1 : 0 },
      source_integrity: sourceIntegrity
        ? { complete: true, generation_identified: generation !== null }
        : { complete: false, generation_identified: generation !== null,
          corrupt_line: 2, dropped_lines: 1 },
    },
  }
}

const emptyPayload = framePayload()
const conceptPayload = (id, options = {}) => framePayload({ id, ...options })

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
  const setInput = (input, value) => act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value)
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true })); await settle()
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
    let runId = 'run#one'
    let generation = GENERATION_A
    let displayedSequence = null
    let state = {
      direction: 'max', engine_running: true, best_node_id: 0,
      node_concepts: { 0: ['loss/a'] }, concept_consolidation: {}, concept_edges: {},
      node_concept_provenance: { 0: 'researcher-authored' },
      nodes: { 0: { id: 0, attempt: 0, status: 'evaluated', idea: {}, metric: 0.7,
        confirmed_mean: null, feasible: true } },
    }
    const render = () => act(async () => {
      root.render(React.createElement(conceptModule.default, {
        runId, generation, sequence: displayedSequence, state, onPickNode: id => { picked = id },
      })); await settle()
    })

    assert.throws(() => conceptModule.validateConceptPayload({
      ...emptyPayload, requested_lens: 'uses',
    }, { requestedLens: 'is_a' }), /Invalid concept projection/)
    assert.throws(() => conceptModule.validateConceptPayload({
      ...conceptPayload('bad/count'), metrics: {
        ...conceptPayload('bad/count').metrics,
        rows: { 'bad/count': { ...conceptPayload('bad/count').metrics.rows['bad/count'], touched: '1' } },
      },
    }), /Invalid concept projection/)
    const derivedPayload = framePayload({
      id: 'derived/root', requestedLens: 'usage', effectiveLens: 'usage', derived: true,
      edgesPresent: true, lensEdgesPresent: true,
    })
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(derivedPayload, {
      requestedLens: 'usage', direction: 'max', derived: true, rels: ['uses'],
    }))
    assert.throws(() => conceptModule.validateConceptPayload(derivedPayload, {
      requestedLens: 'usage', direction: 'max', derived: false,
    }), /Invalid concept projection/)
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(framePayload({
      requestedLens: 'usage', effectiveLens: 'is_a', derived: true,
    }), { requestedLens: 'usage', direction: 'max', derived: true, rels: ['uses'] }))
    const derivedPath = framePayload({
      id: 'taxonomy/root', requestedLens: 'taxonomy', effectiveLens: 'taxonomy', derived: true,
      requestedRels: ['is_a'], requestedKind: 'path',
    })
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(derivedPath, {
      requestedLens: 'taxonomy', direction: 'max', derived: true, rels: ['is_a'],
    }), 'a validated ephemeral is-a path is a legal hierarchy without lens edges')
    assert.throws(() => conceptModule.validateConceptPayload({
      ...derivedPath, edges_present: true, lens_edges_present: true,
    }, { requestedLens: 'taxonomy', direction: 'max', derived: true, rels: ['is_a'] }),
    /Invalid concept projection/)
    assert.throws(() => conceptModule.validateConceptPayload({
      ...derivedPayload, lens: 'uses', effective_lens: 'uses',
      tree: { ...derivedPayload.tree, lens: 'uses' },
    }, { requestedLens: 'usage', direction: 'max', derived: true }), /Invalid concept projection/)
    assert.throws(() => conceptModule.validateConceptPayload({
      ...conceptPayload('loss/a'),
      tree: { ...conceptPayload('loss/a').tree, roots: ['loss/a'] },
    }), /Invalid concept projection/, 'path topology must agree with parent/depth/children receipts')
    assert.throws(() => conceptModule.validateConceptPayload({
      ...conceptPayload('loss/a'), completeness: {
        ...conceptPayload('loss/a').completeness,
        included: { ...conceptPayload('loss/a').completeness.included, membership_nodes: 0 },
      },
    }), /Invalid concept projection/, 'receipt counts must match self-contained lifecycle refs')
    const preStart = framePayload({ generation: null, requestedSeq: 0, capturedSeq: 0, maxSeq: 4,
      complete: false, sourceAuthoritative: false, reasons: ['generation_unavailable'] })
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(preStart, {
      generation: GENERATION_A, requestedSeq: 0,
    }), 'a historical prefix before run_started legitimately has no generation marker')
    assert.throws(() => conceptModule.validateConceptPayload(framePayload({
      generation: null, capturedSeq: -1, maxSeq: -1, complete: false,
      sourceAuthoritative: false, reasons: ['generation_unavailable'],
    }), { generation: GENERATION_A, requestedSeq: null }), /Invalid concept projection/,
    'a live frame cannot silently lose the expected run generation')
    const baseProjectionKey = conceptModule.conceptProjectionKey(state)
    assert.equal(baseProjectionKey,
      conceptModule.conceptProjectionKey({ ...state, engine_running: false }))
    for (const changed of [
      { ...state, best_node_id: 1 },
      { ...state, node_concept_provenance: { 0: 'classifier-v2' } },
      { ...state, nodes: { 0: { ...state.nodes[0], attempt: 1 } } },
      { ...state, nodes: { 0: { ...state.nodes[0], status: 'pending' } } },
    ]) assert.notEqual(baseProjectionKey, conceptModule.conceptProjectionKey(changed))

    await render()
    assert.match(document.querySelector('[role="status"]').textContent, /Building the concept view/)
    assert.equal(document.querySelector('[aria-label="Visible metric columns"]'), null)
    assert.match(requests[0].url, /\/api\/runs\/run%23one\/concepts\?lens=is_a$/)
    assert.ok(requests[0].options.signal instanceof AbortSignal)
    assert.equal(requests[0].options.cache, 'no-store')
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
    assert.match(document.body.textContent, /Concept tree.*2 concepts/s)
    await click(button('Expand all'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'loss/a')
    const experiment = document.querySelector('button[aria-label="Open experiment 0 in Inspector"]')
    assert.equal(experiment?.tagName, 'BUTTON')
    assert.equal(experiment?.getAttribute('role'), null, 'native button owns Enter and Space semantics')
    await click(experiment)
    assert.equal(picked, 0)
    state = { ...state, nodes: { 0: { ...state.nodes[0], attempt: undefined } } }
    await render()
    const unboundExperiment = document.querySelector(
      'button[aria-label="Experiment 0 is not in the displayed run snapshot"]')
    assert.equal(unboundExperiment?.disabled, true,
      'a missing attempt identity must not bind a frame ref to a same-number live node')
    state = { ...state, nodes: { 0: { ...state.nodes[0], attempt: 0 } } }
    await render()
    await reply(requests.at(-1), conceptPayload('loss/a'))
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
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'loss/a',
      'last-good data stays visible while a semantic refresh is pending')
    await reply(requests.at(-1), conceptPayload('architecture/moe'))
    await click(button('Expand all'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'architecture/moe')

    before = requests.length
    state = { ...state, nodes: { 0: { ...state.nodes[0], metric: 0.9 } } }
    await render()
    assert.equal(requests.length, before + 1, 'metric-only changes refresh baseline and rollups')
    const obsoleteSemanticRequest = requests.at(-1)
    state = { ...state, nodes: { 0: { ...state.nodes[0], metric: 0.95 } } }
    await render()
    const currentSemanticRequest = requests.at(-1)
    assert.notEqual(currentSemanticRequest, obsoleteSemanticRequest)
    await reply(obsoleteSemanticRequest, conceptPayload('late/semantic'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'architecture/moe',
      'a superseded semantic projection cannot become current before effect cleanup')
    await reply(currentSemanticRequest, { ...emptyPayload, metrics: {} })
    assert.match(document.querySelector('[role="alert"]').textContent, /last loaded concept view; refresh failed/i)
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'architecture/moe')
    assert.doesNotMatch(document.body.textContent, /No concepts have been tagged yet/,
      'malformed HTTP 200 never masquerades as an authoritative empty result')

    await click(button('Retry'))
    const partial = conceptPayload('safe/root', {
      complete: false, truncated: true, reasons: ['membership_cap'],
    })
    await reply(requests.at(-1), partial)
    assert.match(document.body.textContent, /bounded partial frame.*configured limits omitted records/i)
    await click(button('Expand all'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'safe/root')
    assert.match(document.body.textContent, /recorded claims.*not independently verified/i)

    before = requests.length
    runId = 'next%2Frun'
    generation = GENERATION_B
    await render()
    assert.equal(requests.length, before + 1)
    assert.match(requests.at(-1).url, /\/api\/runs\/next%252Frun\/concepts\?lens=is_a$/)
    assert.doesNotMatch(document.body.textContent, /safe\/root/,
      'last-good data is scoped to the exact run identity')
    const nextRunRequest = requests.at(-1)
    runId = 'final-run'
    await render()
    await reply(nextRunRequest, conceptPayload('late/run', {
      runId: 'next%2Frun', generation: GENERATION_B,
    }))
    assert.doesNotMatch(document.body.textContent, /late\/run/)
    await reply(requests.at(-1), framePayload({ runId: 'final-run', generation: GENERATION_B }))
    assert.match(document.body.textContent, /No concepts have been tagged yet/)

    displayedSequence = 2
    await render()
    assert.match(requests.at(-1).url, /\/api\/runs\/final-run\/concepts\?lens=is_a&seq=2$/)
    await reply(requests.at(-1), framePayload({
      id: 'historical/root', runId: 'final-run', generation: GENERATION_B,
      requestedSeq: 2, capturedSeq: 2, maxSeq: 7,
    }))
    assert.match(document.body.textContent, /Historical concept frame at sequence 2 of 7/)
    assert.equal(document.querySelector('[aria-label="Describe a lens to create"]').disabled, true,
      'paid lens derivation is unavailable while viewing a historical frame')

    displayedSequence = null
    await render()
    await reply(requests.at(-1), conceptPayload('current/root', {
      runId: 'final-run', generation: GENERATION_B, capturedSeq: 7,
    }))
    const lensInput = document.querySelector('[aria-label="Describe a lens to create"]')
    assert.equal(lensInput.disabled, false)
    await setInput(lensInput, 'group by usage')
    before = requests.length
    await act(async () => {
      const form = document.querySelector('.cv-lensnew')
      form.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      form.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before + 1, 'paid lens derivation has a synchronous single-flight lock')
    const lensPost = requests.at(-1)
    assert.equal(lensPost.options.method, 'POST')
    assert.deepEqual(JSON.parse(lensPost.options.body), { prompt: 'group by usage' })
    await reply(lensPost, {
      ...framePayload({ id: 'derived/root', runId: 'final-run', generation: GENERATION_B,
        requestedLens: 'usage', effectiveLens: 'usage', derived: true,
        capturedSeq: 7, edgesPresent: true, lensEdgesPresent: true }),
      ok: true, spec: { name: 'usage', label: 'By usage', rels: ['uses'],
        kind: 'edge', provenance: 'agent' },
    })
    assert.match(requests.at(-1).url,
      /\/api\/runs\/final-run\/concepts\?lens=usage&rels=uses$/)
    await reply(requests.at(-1), framePayload({
      id: 'derived/root', runId: 'final-run', generation: GENERATION_B,
      requestedLens: 'usage', effectiveLens: 'usage', derived: true,
      capturedSeq: 7, edgesPresent: true, lensEdgesPresent: true,
    }))
    assert.equal(document.querySelector('[aria-label="Concept hierarchy lens"]').value, 'usage')

    generation = GENERATION_A
    await render()
    assert.match(requests.at(-1).url, /\/api\/runs\/final-run\/concepts\?lens=is_a$/,
      'same-id generation replacement discards the ephemeral lens scope')
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

// Finding 1 (round-2 mega-review): a concept id can be an untrusted prototype key. The intermediate
// (untagged) node of a "constructor/foo" tag is "constructor", which has NO experiment_refs entry —
// reading the RAW experiment_refs object by that id resolves up the prototype chain to
// Object.prototype.constructor (a function), and expanding the row then calls `.map` on the function and
// crashes the whole view. The frame's refs/metrics must be read through a prototype-safe (null-proto) map.
test('ConceptView does not crash on a concept id that is a JS prototype key (expand-all)', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const native = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout }
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: cb => native.setTimeout(cb, 0), cancelAnimationFrame: h => native.clearTimeout(h),
    fetch: (url, options = {}) => new Promise(resolve => requests.push({ url: String(url), options, resolve })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root, vite
  const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
  const button = text => [...document.querySelectorAll('button')].find(n => n.textContent.trim() === text)
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
    })
    const [{ createRoot }, conceptModule] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ConceptView.jsx'),
    ])
    // the payload the endpoint would ship: "constructor" is an untagged intermediate, only "constructor/foo"
    // carries refs/metrics — validation accepts it (a real, valid tree), so the crash input is reachable.
    const payload = conceptPayload('constructor/foo')
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(payload))
    root = createRoot(document.getElementById('root'))
    const state = {
      direction: 'max', engine_running: true, best_node_id: 0,
      node_concepts: { 0: ['constructor/foo'] }, concept_consolidation: {}, concept_edges: {},
      node_concept_provenance: { 0: 'researcher-authored' },
      nodes: { 0: { id: 0, attempt: 0, status: 'evaluated', idea: {}, metric: 0.7, confirmed_mean: null, feasible: true } },
    }
    await act(async () => {
      root.render(React.createElement(conceptModule.default, {
        runId: 'run#one', generation: GENERATION_A, sequence: null, state, onPickNode() {},
      })); await settle()
    })
    await act(async () => {
      requests.at(-1).resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => payload })
      await settle()
    })
    // expand every row (this is what triggers experiments.map on the prototype-key node) — must not throw
    await act(async () => { button('Expand all')?.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); await settle() })
    const cids = [...document.querySelectorAll('.cv-cid')].map(n => n.textContent)
    assert.ok(cids.includes('constructor'), 'the prototype-key intermediate row rendered')
    assert.ok(document.querySelector('.cv-table'), 'the concept table rendered without crashing')
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
