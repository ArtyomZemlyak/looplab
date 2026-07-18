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
const GENERATION_C = 'c'.repeat(64)
const limits = {
  membership_nodes: 2048, concepts_per_node: 64, memberships: 8192,
  tree_nodes: 4096, edges: 2048, edge_endpoints: 4096,
}

function framePayload({ id = null, runId = 'run#one', generation = GENERATION_A,
  requestedLens = 'is_a', effectiveLens = requestedLens, derived = false,
  requestedRels = null, requestedKind = null, nodeGeneration = 0,
  requestedSeq = null, capturedSeq = 4, maxSeq = capturedSeq,
  direction = 'max',
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
    metrics: { baseline: hasConcept ? 0.5 : null, direction, rows: hasConcept ? { [id]: {
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
const linkedEdgePayload = (options = {}) => {
  const payload = framePayload({
    id: 'loss/contrastive', requestedLens: 'usage', effectiveLens: 'usage', derived: true,
    requestedRels: ['uses'], requestedKind: 'edge', edgesPresent: true,
    lensEdgesPresent: true, ...options,
  })
  payload.tree.roots.push('training/contrastive')
  payload.tree.nodes['training/contrastive'] = {
    parent: null, depth: 0, children: [], tagged: false, cross_parents: [],
  }
  payload.tree.nodes['loss/contrastive'].cross_parents = ['training/contrastive']
  payload.completeness.included.tree_nodes += 1
  return payload
}

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
    fetch: (url, options = {}) => {
      const href = String(url)
      if (href.includes('/concepts/lens/recovery?') && (options.method || 'GET') === 'GET') {
        const expected = new URL(href, 'https://looplab.test').searchParams.get('expected_generation')
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
          json: async () => ({ schema: 1, generation: expected, state: 'none' }) })
      }
      return new Promise((resolve, reject) => requests.push({ url: href, options, resolve, reject }))
    },
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
    assert.equal(conceptModule.validDerivedLensLabel('By usage'), true)
    assert.equal(conceptModule.validDerivedLensLabel('x'.repeat(61)), false)
    assert.equal(conceptModule.validDerivedLensLabel('   '), false)
    assert.equal(conceptModule.validDerivedLensLabel(' padded label '), false,
      'derived labels must match the server-normalized nonblank form')
    assert.equal(conceptModule.validDerivedLensLabel('hidden\u200blabel'), false,
      'format/control Unicode cannot enter an option label or title')
    assert.equal(conceptModule.classifyConceptCompleteness(['membership_cap']), 'bounded-cap')
    for (const reasons of [
      ['delta_dependency_cycle'], ['delta_dependency_missing_parent'],
      ['delta_dependency_unknown_parent_membership'], ['invalid_consolidation_map'],
      ['invalid_concept_id'], ['rename_cycle'], ['rename_hop_cap'], ['concept_mode_unsupported'],
      ['membership_cap', 'rename_cycle'],
    ]) assert.equal(conceptModule.classifyConceptCompleteness(reasons),
      'materialization-corruption', `${reasons.join(',')} is not safe cap truncation`)
    assert.equal(conceptModule.classifyConceptCompleteness(['event_log_corruption']), 'integrity')
    assert.equal(conceptModule.classifyConceptCompleteness([7]), 'integrity',
      'the exported classifier is total over malformed callers')
    const derivedPayload = framePayload({
      id: 'derived/root', requestedLens: 'usage', effectiveLens: 'usage', derived: true,
      edgesPresent: true, lensEdgesPresent: true,
    })
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(derivedPayload, {
      requestedLens: 'usage', direction: 'max', derived: true, rels: ['uses'],
    }))
    const paidDerivedExpected = {
      requestedLens: 'usage', direction: 'max', derived: true, rels: ['uses'],
    }
    const cappedDerivedPayload = framePayload({
      id: 'derived/root', requestedLens: 'usage', effectiveLens: 'usage', derived: true,
      edgesPresent: true, lensEdgesPresent: true,
      complete: false, truncated: true, reasons: ['membership_cap'],
    })
    assert.doesNotThrow(() => conceptModule.validatePaidDerivedPayload(
      cappedDerivedPayload, paidDerivedExpected))
    for (const unsafePartial of [
      framePayload({
        id: 'derived/root', requestedLens: 'usage', effectiveLens: 'usage', derived: true,
        edgesPresent: true, lensEdgesPresent: true, complete: false,
        sourceAuthoritative: false, reasons: ['event_log_corruption'],
      }),
      framePayload({
        id: 'derived/root', requestedLens: 'usage', effectiveLens: 'usage', derived: true,
        edgesPresent: true, lensEdgesPresent: true, complete: false,
        truncated: false, reasons: ['rename_hop_cap'],
      }),
    ]) {
      assert.doesNotThrow(() => conceptModule.validateConceptPayload(
        unsafePartial, paidDerivedExpected), 'the base viewer can display a truthful unsafe partial')
      assert.throws(() => conceptModule.validatePaidDerivedPayload(
        unsafePartial, paidDerivedExpected), /Invalid concept projection/,
      'paid success accepts only the server allow-listed size-cap partials')
    }
    assert.throws(() => conceptModule.validateConceptPayload(framePayload({
      id: 'derived/root', requestedLens: 'usage', effectiveLens: 'usage', derived: true,
      edgesPresent: true, lensEdgesPresent: true, complete: false,
      truncated: true, reasons: ['rename_hop_cap'],
    }), paidDerivedExpected), /Invalid concept projection/,
    'a corruption-adjacent reason cannot claim monotone truncation just because it ends in _cap')
    assert.throws(() => conceptModule.validateConceptPayload(derivedPayload, {
      requestedLens: 'usage', direction: 'max', derived: false,
    }), /Invalid concept projection/)
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(framePayload({
      requestedLens: 'usage', effectiveLens: 'is_a', derived: true,
    }), { requestedLens: 'usage', direction: 'max', derived: true, rels: ['uses'] }))
    const additiveReceipts = framePayload()
    additiveReceipts.completeness = {
      ...additiveReceipts.completeness,
      limits: { ...additiveReceipts.completeness.limits, future_limit: 7 },
      source: { ...additiveReceipts.completeness.source, future_source_count: 0 },
      included: { ...additiveReceipts.completeness.included, future_included_count: 0 },
      source_integrity: {
        ...additiveReceipts.completeness.source_integrity,
        future_integrity_receipt: true,
      },
    }
    assert.doesNotThrow(() => conceptModule.validateConceptPayload(additiveReceipts),
      'additive receipt fields must not brick an otherwise valid projection')
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
    const topology = conceptPayload('loss/a')
    const topologyRoot = topology.tree.roots[0]
    const taggedId = 'loss/a'
    for (const [label, malformed] of [
      ['duplicate roots', {
        ...topology, tree: { ...topology.tree, roots: [topologyRoot, topologyRoot] },
      }],
      ['duplicate children', { ...topology, tree: { ...topology.tree, nodes: {
        ...topology.tree.nodes,
        [topologyRoot]: { ...topology.tree.nodes[topologyRoot], children: [taggedId, taggedId] },
      } } }],
      ['disconnected nodes', { ...topology, tree: { ...topology.tree, nodes: {
        ...topology.tree.nodes,
        orphan: { parent: null, depth: 0, children: [], tagged: false },
      } } }],
      ['tagged receipt mismatch', { ...topology, tree: { ...topology.tree, nodes: {
        ...topology.tree.nodes,
        [taggedId]: { ...topology.tree.nodes[taggedId], tagged: false },
      } } }],
      ['touch key-set mismatch', { ...topology, touch: { ...topology.touch, orphan: 1 } }],
      ['metric key-set mismatch', { ...topology, metrics: { ...topology.metrics, rows: {
        ...topology.metrics.rows, orphan: topology.metrics.rows[taggedId],
      } } }],
    ]) {
      assert.throws(() => conceptModule.validateConceptPayload(malformed),
        /Invalid concept projection/, `${label} must fail closed`)
    }
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
      { ...state, nodes: { 0: { ...state.nodes[0], tombstoned: true } } },
      { ...state, aborted_nodes: [0] },
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
    assert.equal(conceptModule.legacyAxisFallbackPresent({
      nodes: { 0: { id: 0, idea: { theme: 'legacy-axis' } } }, node_concepts: {},
    }), true, 'an old-run authored axis is detected only when its folded row is absent')
    assert.equal(conceptModule.legacyAxisFallbackPresent({
      nodes: { 0: { id: 0, idea: { theme: 'legacy-axis' } } }, node_concepts: { 0: [] },
    }), false, 'an explicit empty folded membership must never fall back to authored data')
    assert.equal(conceptModule.legacyAxisFallbackPresent({
      nodes: { 0: { id: 0, idea: { theme: 'legacy-axis' } } }, node_concepts: {},
      aborted_nodes: [0],
    }), false, 'an aborted legacy node must not create current-view compatibility copy')

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
    assert.match(document.body.textContent,
      /Concept hierarchy.*1 tagged concept.*2 displayed concept nodes.*1 tagged experiment/s,
      'synthetic ancestors are displayed concept nodes, not mislabeled as tagged concepts')
    const hierarchyView = document.querySelector('.concept-view')
    assert.equal(hierarchyView?.getAttribute('aria-label'), 'Concept hierarchy')
    assert.equal(document.querySelector('[aria-label="Concept projection lens"]')?.value, 'is_a')
    const metricContext = document.getElementById('concept-metric-context')
    assert.equal(metricContext?.getAttribute('role'), 'note')
    assert.match(metricContext?.textContent,
      /Primary objective metric.*Unnamed metric.*unit not recorded.*maximize.*Δ columns.*orientation-normalized.*positive values mean better/is)
    assert.match(hierarchyView?.getAttribute('aria-describedby') || '', /concept-metric-context/)
    assert.equal(document.getElementById('concept-relationship-legend'), null,
      'the hierarchy must not describe arbitrary relationship-parent semantics')

    state = { ...state, direction: 'min' }
    await render()
    await reply(requests.at(-1), conceptPayload('loss/a', { direction: 'min' }))
    assert.match(document.getElementById('concept-metric-context')?.textContent,
      /unit not recorded.*minimize.*positive values mean better/is,
      'the persistent metric context names minimize without reversing normalized delta semantics')
    state = { ...state, direction: 'max' }
    await render()
    await reply(requests.at(-1), conceptPayload('loss/a'))
    await click(button('Expand concept rows'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'loss/a')
    const median = document.querySelector('.cv-baseline')
    assert.equal(median?.getAttribute('role'), 'note')
    assert.match(median?.textContent, /run median/i)
    assert.match(median?.getAttribute('title'),
      /eligible evaluated experiments.*not explicitly infeasible.*Δ columns/s)
    assert.match(median?.getAttribute('aria-label'),
      /metric available and not explicitly infeasible.*Delta columns are direction-normalized/i)
    assert.equal(document.querySelector('.cv-exp-button'), null,
      'bulk hierarchy expansion must not materialize every evidence row')
    const evidenceToggle = document.querySelector(
      'button[aria-label="Show 1 tagged experiment for loss/a"]')
    assert.equal(evidenceToggle?.getAttribute('aria-expanded'), 'false')
    await click(evidenceToggle)
    const experiment = document.querySelector('button[aria-label*="Open in Inspector"]')
    assert.equal(experiment?.tagName, 'BUTTON')
    assert.equal(experiment?.getAttribute('role'), null, 'native button owns Enter and Space semantics')
    assert.match(experiment.textContent, /Experiment #0 · attempt 0.*evaluated/s)
    assert.match(experiment.textContent, /feasible.*membership · researcher-authored.*rollup · eligible/s,
      'constraint, membership provenance, and rollup eligibility must be visible without a tooltip')
    assert.match(experiment.getAttribute('title'), /membership researcher-authored.*included in the concept rollup/i)
    await click(experiment)
    assert.equal(picked, 0)
    state = { ...state, nodes: { 0: { ...state.nodes[0], attempt: undefined } } }
    await render()
    const unboundProjectionRequest = requests.at(-1)
    const unboundExperiment = document.querySelector(
      'button[aria-label*="not in the displayed run snapshot"]')
    assert.equal(unboundExperiment?.disabled, true,
      'a missing attempt identity must not bind a frame ref to a same-number live node')
    state = { ...state, nodes: { 0: { ...state.nodes[0], attempt: 0 } } }
    await render()
    assert.equal(requests.at(-1), unboundProjectionRequest,
      'a same-scope projection tick coalesces behind the request already in flight')
    assert.equal(unboundProjectionRequest.options.signal.aborted, false)
    await reply(unboundProjectionRequest, conceptPayload('loss/a'))
    const reboundProjectionRequest = requests.at(-1)
    assert.notEqual(reboundProjectionRequest, unboundProjectionRequest)
    const excludedPayload = conceptPayload('loss/a')
    excludedPayload.experiment_refs['loss/a'][0].feasible = false
    Object.assign(excludedPayload.metrics.rows['loss/a'], {
      evaluated: 0, best: null, mean: null, worst: null, delta_best: null, delta_mean: null,
    })
    await reply(reboundProjectionRequest, excludedPayload)
    assert.match(document.querySelector('.cv-exp-button')?.textContent, /infeasible.*rollup · excluded/s)
    assert.match(document.querySelector('.cv-exp-button')?.getAttribute('title'),
      /excluded from the concept rollup because it is infeasible/i)
    assert.match(document.querySelector('.cv-expmetric')?.textContent, /excluded/i)
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
    await click(button('Expand concept rows'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'architecture/moe')

    before = requests.length
    state = { ...state, nodes: { 0: { ...state.nodes[0], metric: 0.9 } } }
    await render()
    assert.equal(requests.length, before + 1, 'metric-only changes refresh baseline and rollups')
    const coalescedRequest = requests.at(-1)
    for (const metric of [0.95, 0.96, 0.97, 0.98]) {
      state = { ...state, nodes: { 0: { ...state.nodes[0], metric } } }
      await render()
    }
    assert.equal(requests.length, before + 1,
      'continuous rapid projection ticks cannot restart or starve the active request')
    assert.equal(coalescedRequest.options.signal.aborted, false)
    await reply(coalescedRequest, conceptPayload('coalesced/settled'))
    assert.equal(requests.length, before + 2,
      'settling the active request immediately launches exactly the newest pending projection')
    const currentSemanticRequest = requests.at(-1)
    assert.notEqual(currentSemanticRequest, coalescedRequest)
    assert.equal(coalescedRequest.options.signal.aborted, false)
    assert.match(document.body.textContent, /Refreshing concepts/,
      'a superseded response remains visibly non-current while the newest projection loads')
    await reply(currentSemanticRequest, conceptPayload('coalesced/latest'))
    await click(button('Expand concept rows'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'),
      'coalesced/latest')

    state = { ...state, nodes: { 0: { ...state.nodes[0], metric: 0.99 } } }
    await render()
    await reply(requests.at(-1), { ...emptyPayload, metrics: {} })
    assert.match(document.querySelector('[role="alert"]').textContent, /last loaded concept view; refresh failed/i)
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'),
      'coalesced/latest')
    assert.doesNotMatch(document.body.textContent, /No concepts have been tagged yet/,
      'malformed HTTP 200 never masquerades as an authoritative empty result')

    await click(button('Retry'))
    const partial = conceptPayload('safe/root', {
      complete: false, truncated: true, reasons: ['membership_cap'],
    })
    await reply(requests.at(-1), partial)
    assert.match(document.body.textContent,
      /bounded partial frame.*configured limits omitted records.*membership_cap/i)
    await click(button('Expand concept rows'))
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'safe/root')
    assert.match(document.body.textContent, /recorded claims.*not independently verified/i)

    await click(button('Refresh'))
    await reply(requests.at(-1), conceptPayload('cycle', {
      complete: false, reasons: ['delta_dependency_cycle'],
    }))
    const corruptionNotice = document.querySelector('.cv-resource-note.partial')
    assert.equal(corruptionNotice?.getAttribute('role'), 'alert')
    assert.match(corruptionNotice?.textContent,
      /materialization is incomplete.*delta_dependency_cycle.*rows are safe.*missing concepts.*Refresh cannot repair.*Lab.*Events and Authoring.*fork and replay/is)
    assert.equal(document.querySelector('.cv-crow.tagged .cv-cid')?.getAttribute('title'), 'cycle',
      'safe rows remain visible without turning corruption into a coverage claim')

    await click(button('Refresh'))
    await reply(requests.at(-1), framePayload({
      complete: false, reasons: ['rename_cycle'],
    }))
    const corruptionCard = document.querySelector('.cv-state-card[role="alert"]')
    assert.match(corruptionCard?.textContent,
      /Concept materialization is blocked.*rename_cycle.*not a no-concepts result.*Refresh cannot repair.*Lab.*Events and Authoring.*fork and replay/is)
    assert.equal(button('Retry'), undefined,
      'a read retry cannot repair a durable materialization receipt')
    assert.equal(button('Refresh concepts'), undefined,
      'permanent corruption must not masquerade as an ordinary empty refresh state')

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
    assert.equal(lensInput.maxLength, 800)
    await setInput(lensInput, '界'.repeat(700))
    before = requests.length
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before,
      'an over-byte-limit prompt is rejected before any paid transport')
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /too long.*800 characters.*2,048 bytes/i)
    await setInput(lensInput, 'group by usage')
    assert.equal(document.querySelector('.cv-lenserr'), null,
      'editing the prompt clears the previous local validation error')
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
    assert.match(lensPost.options.headers['Idempotency-Key'],
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
    assert.deepEqual(JSON.parse(lensPost.options.body), {
      prompt: 'group by usage', expected_generation: GENERATION_B,
    })
    assert.match(document.querySelector('#paid-concept-lens-status')?.textContent,
      /paid request dispatch may have started.*same saved identity/i)

    runId = 'lens-scope-run'
    generation = GENERATION_A
    await render()
    const scopedGet = requests.at(-1)
    assert.match(scopedGet.url, /\/api\/runs\/lens-scope-run\/concepts\?lens=is_a$/)
    await reply(scopedGet, conceptPayload('scope/root', {
      runId: 'lens-scope-run', generation: GENERATION_A,
    }))
    const scopedInput = document.querySelector('[aria-label="Describe a lens to create"]')
    assert.equal(scopedInput.value, '', 'another run never inherits the pending paid prompt')
    assert.equal(scopedInput.disabled, false, 'another run is not blocked by the old run lock')
    await setInput(scopedInput, 'group by composition')
    before = requests.length
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before + 1,
      'paid lens single-flight locks are scoped to the exact run generation')
    const scopedPost = requests.at(-1)
    assert.equal(scopedPost.options.method, 'POST')
    assert.deepEqual(JSON.parse(scopedPost.options.body), {
      prompt: 'group by composition', expected_generation: GENERATION_A,
    })
    await reply(scopedPost, {
      ...conceptPayload('scope/root', { runId: 'lens-scope-run', generation: GENERATION_A }),
      ok: false, reason: 'no_model', request_id: '1'.repeat(64),
    })
    assert.match(document.querySelector('.cv-lenserr')?.textContent, /No model is configured/)
    assert.equal(document.querySelector('[aria-label="Describe a lens to create"]').value,
      'group by composition', 'authoritative no-model rejection preserves the recoverable draft')

    await setInput(document.querySelector('[aria-label="Describe a lens to create"]'),
      'group by composition')
    before = requests.length
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before + 1)
    await reply(requests.at(-1), {
      ...conceptPayload('scope/root', { runId: 'lens-scope-run', generation: GENERATION_A }),
      ok: false, code: 'job_capacity', reason: 'capacity',
      generation: GENERATION_A, request_id: '4'.repeat(64),
    })
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /service is at capacity.*No request was started.*retry later/i)
    assert.equal(document.querySelector('[aria-label="Describe a lens to create"]').value,
      'group by composition', 'HTTP-200 capacity terminal clears identity but preserves the draft')

    await setInput(document.querySelector('[aria-label="Describe a lens to create"]'),
      'group by composition')
    before = requests.length
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before + 1)
    await reply(requests.at(-1), { detail: {
      code: 'run_generation_changed', message: 'changed',
    } }, 409)
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /Run changed.*Reload Concepts.*another paid lens/i)

    await setInput(document.querySelector('[aria-label="Describe a lens to create"]'),
      'group by composition')
    before = requests.length
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before + 1)
    await act(async () => {
      requests.at(-1).reject(new TypeError('simulated network loss'))
      await settle()
    })
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /Outcome unknown.*provider charges may have occurred.*Resume.*same saved request.*another key/i)
    const unknownPost = requests.at(-1)
    assert.ok(button('Why no local discard?'),
      'local-only discard is unavailable before durable server reconciliation')
    before = requests.length
    await click(button('Resume paid lens'))
    assert.equal(requests.length, before + 1)
    const reconcilePost = requests.at(-1)
    assert.equal(reconcilePost.options.headers['Idempotency-Key'],
      unknownPost.options.headers['Idempotency-Key'])
    await reply(reconcilePost, {
      ...conceptPayload('scope/root', { runId: 'lens-scope-run', generation: GENERATION_A }),
      ok: false, ambiguous: true, code: 'concept_lens_uncertain',
      generation: GENERATION_A, request_id: '3'.repeat(64),
    })
    assert.ok(button('Abandon unknown request'),
      'server abandonment appears only after same-key reconciliation returns an exact request id')
    before = requests.length
    await click(button('Abandon unknown request'))
    assert.equal(requests.length, before + 1)
    const abandonPost = requests.at(-1)
    assert.match(abandonPost.url, /\/api\/runs\/lens-scope-run\/concepts\/lens\/abandon$/)
    assert.equal(abandonPost.options.headers['Idempotency-Key'],
      unknownPost.options.headers['Idempotency-Key'])
    assert.deepEqual(JSON.parse(abandonPost.options.body), {
      expected_generation: GENERATION_A, request_id: '3'.repeat(64),
    })
    await reply(abandonPost, {
      ...conceptPayload('scope/root', { runId: 'lens-scope-run', generation: GENERATION_A }),
      ok: false, code: 'concept_lens_abandoned', reason: 'operator_abandoned',
      abandoned: true, resolved: true,
      provider_outcome: 'unknown', billing_status: 'unknown',
      warning: 'Provider may already have completed and billed the request.',
      generation: GENERATION_A, request_id: '3'.repeat(64), seq: 9,
    })
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /abandoned on the server.*may already have completed and been billed.*usage.*unavailable/i)
    assert.ok(button('Create lens · paid'), 'a durable abandon terminal clears the local intent')

    await setInput(document.querySelector('[aria-label="Describe a lens to create"]'),
      'group by race outcome')
    before = requests.length
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before + 1)
    await act(async () => {
      requests.at(-1).reject(new TypeError('simulated race response loss'))
      await settle()
    })
    await click(button('Resume paid lens'))
    await reply(requests.at(-1), {
      ...conceptPayload('scope/root', { runId: 'lens-scope-run', generation: GENERATION_A }),
      ok: false, ambiguous: true, code: 'concept_lens_uncertain',
      generation: GENERATION_A, request_id: '5'.repeat(64),
    })
    await click(button('Abandon unknown request'))
    const racedAbandon = requests.at(-1)
    await reply(racedAbandon, {
      ...framePayload({ id: 'race/root', runId: 'lens-scope-run', generation: GENERATION_A,
        requestedLens: 'race-usage', effectiveLens: 'race-usage', derived: true,
        edgesPresent: true, lensEdgesPresent: true,
        complete: false, truncated: true, reasons: ['membership_cap'] }),
      ok: true, spec: { name: 'race-usage', label: 'Race usage', rels: ['uses'],
        kind: 'edge', provenance: 'agent' },
      request_id: '5'.repeat(64),
    })
    assert.match(requests.at(-1).url,
      /\/api\/runs\/lens-scope-run\/concepts\?lens=race-usage&rels=uses$/,
      'a provider terminal that wins the abandon race restores its derived lens')
    await reply(requests.at(-1), linkedEdgePayload({
      runId: 'lens-scope-run', generation: GENERATION_A,
      requestedLens: 'race-usage', effectiveLens: 'race-usage',
      complete: false, truncated: true, reasons: ['membership_cap'],
    }))
    assert.equal(document.querySelector('[aria-label="Concept projection lens"]').value,
      'race-usage')
    assert.match(document.body.textContent, /bounded partial frame.*configured limits omitted records/i)
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /provider completed before abandonment.*validated lens was restored/i)

    await setInput(document.querySelector('[aria-label="Describe a lens to create"]'),
      'reject corrupted paid frame')
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    const corruptedPaidPost = requests.at(-1)
    await reply(corruptedPaidPost, {
      ...framePayload({ id: 'corrupt/root', runId: 'lens-scope-run', generation: GENERATION_A,
        requestedLens: 'corrupt-usage', effectiveLens: 'corrupt-usage', derived: true,
        edgesPresent: true, lensEdgesPresent: true, complete: false,
        sourceAuthoritative: false, reasons: ['event_log_corruption'] }),
      ok: true, spec: { name: 'corrupt-usage', label: 'Corrupt usage', rels: ['uses'],
        kind: 'edge', provenance: 'agent' },
      request_id: '6'.repeat(64),
    })
    assert.equal(requests.at(-1), corruptedPaidPost,
      'a corruption-partial paid success cannot launch a derived projection read')
    assert.ok(button('Resume paid lens'),
      'a malformed paid terminal remains on its exact saved identity instead of being accepted')
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /Outcome unknown.*same saved request.*another key/i)

    runId = 'final-run'
    generation = GENERATION_B
    await render()
    await reply(requests.at(-1), conceptPayload('current/root', {
      runId: 'final-run', generation: GENERATION_B, capturedSeq: 7,
    }))
    const restoredInput = document.querySelector('[aria-label="Describe a lens to create"]')
    assert.equal(restoredInput.value, 'group by usage',
      'reload restores the exact prompt bound to the saved paid intent')
    assert.equal(restoredInput.disabled, true)
    assert.match(button('Resume paid lens')?.textContent || '', /Resume paid lens/)
    before = requests.length
    await click(button('Resume paid lens'))
    assert.equal(requests.length, before + 1)
    const resumedPost = requests.at(-1)
    assert.equal(resumedPost.options.headers['Idempotency-Key'],
      lensPost.options.headers['Idempotency-Key'], 'reload resumes with the exact original key')

    await reply(resumedPost, {
      ...framePayload({ id: 'derived/root', runId: 'final-run', generation: GENERATION_B,
        requestedLens: 'usage', effectiveLens: 'usage', derived: true,
        capturedSeq: 7, edgesPresent: true, lensEdgesPresent: true,
        complete: false, truncated: true, reasons: ['membership_cap'] }),
      ok: true, spec: { name: 'usage', label: 'By usage', rels: ['uses'],
        kind: 'edge', provenance: 'agent' },
      request_id: '2'.repeat(64),
    })
    assert.match(requests.at(-1).url,
      /\/api\/runs\/final-run\/concepts\?lens=usage&rels=uses$/)
    const firstRelationshipGet = requests.at(-1)
    assert.equal(document.querySelector('.concept-view')?.getAttribute('aria-label'),
      'Concept relationship view for projected uses links',
      'relationship semantics remain stable while the new projection is loading')
    assert.match(document.body.textContent, /Loading the latest relationship projection/i)
    await reply(firstRelationshipGet, { detail: 'temporary failure' }, 503)
    assert.equal(document.querySelector('.concept-view')?.getAttribute('aria-label'),
      'Concept relationship view for projected uses links',
      'a relationship read failure must not be mislabeled as a hierarchy')
    assert.doesNotMatch(document.body.textContent, /Loading the latest hierarchy/i)
    await click(button('Retry'))
    const relationshipRetry = requests.at(-1)
    assert.match(relationshipRetry.url,
      /\/api\/runs\/final-run\/concepts\?lens=usage&rels=uses$/)
    await reply(relationshipRetry, linkedEdgePayload({
      runId: 'final-run', generation: GENERATION_B, capturedSeq: 7,
      complete: false, truncated: true, reasons: ['membership_cap'],
    }))
    assert.equal(document.querySelector('[aria-label="Concept projection lens"]').value, 'usage')
    assert.match(document.body.textContent, /bounded partial frame.*configured limits omitted records/i,
      'a fully validated size-cap partial paid result remains visibly partial')
    const relationshipView = document.querySelector('.concept-view')
    assert.match(relationshipView?.getAttribute('aria-label') || '',
      /Concept relationship view for projected uses links/)
    assert.match(document.querySelector('.cv-heading')?.textContent || '',
      /Concept relationships.*2 displayed concept nodes/s)
    assert.ok(button('Expand concept rows'))
    assert.ok(button('Collapse concept rows'))
    const relationshipLegend = document.getElementById('concept-relationship-legend')
    assert.equal(relationshipLegend?.getAttribute('role'), 'note')
    assert.match(relationshipLegend?.textContent,
      /projected uses links.*one primary display parent.*\+N links.*additional projected parents.*not a taxonomy hierarchy/is)
    assert.match(relationshipView?.getAttribute('aria-describedby') || '',
      /concept-metric-context.*concept-relationship-legend/)
    assert.equal(document.querySelector('.cv-cid[title="loss/contrastive"]')?.textContent,
      'loss/contrastive', 'edge lenses retain the full canonical id instead of an ambiguous leaf')
    const crossLink = document.querySelector('.cv-crosslinks > summary')
    assert.match(crossLink?.textContent, /\+1 link/)
    assert.match(crossLink?.getAttribute('title'), /training\/contrastive/,
      'validated secondary parents remain visible evidence instead of disappearing from the tree')
    await click(crossLink)
    assert.equal(crossLink.parentElement?.open, true)
    assert.match(crossLink.parentElement?.textContent || '', /projected uses link.*training\/contrastive/is,
      'keyboard and touch users can open the additional projected-parent evidence')

    generation = GENERATION_A
    await render()
    assert.match(requests.at(-1).url, /\/api\/runs\/final-run\/concepts\?lens=is_a$/,
      'same-id generation replacement discards the ephemeral lens scope')
    await reply(requests.at(-1), conceptPayload('unverified/root', {
      runId: 'final-run', generation: GENERATION_A,
    }))

    const replacementInput = document.querySelector('[aria-label="Describe a lens to create"]')
    await setInput(replacementInput, 'old generation grouping')
    before = requests.length
    await act(async () => {
      document.querySelector('.cv-lensnew').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await settle()
    })
    assert.equal(requests.length, before + 1)
    generation = GENERATION_C
    await render()
    await reply(requests.at(-1), conceptPayload('replacement/root', {
      runId: 'final-run', generation: GENERATION_C,
    }))
    const archiveButton = button('Archive old-generation receipt')
    assert.ok(archiveButton, 'verified generation replacement offers an explicit local archive')
    before = requests.length
    await click(archiveButton)
    assert.equal(requests.length, before, 'archiving an old receipt never calls provider or server')
    assert.equal(document.querySelector('[aria-label="Describe a lens to create"]').value,
      'old generation grouping', 'archive retains the recoverable prompt for the replacement run')
    assert.match(document.querySelector('.cv-lenserr')?.textContent,
      /archived locally.*provider outcome.*billing.*old server claim.*unknown.*replacement generation.*independent/i)
    assert.ok(button('Create lens · paid'))

    generation = null
    await render()
    await reply(requests.at(-1), conceptPayload('unverified/root', {
      runId: 'final-run', generation: null, sourceAuthoritative: false,
    }))
    const unverifiedLensInput = document.querySelector('[aria-label="Describe a lens to create"]')
    assert.equal(unverifiedLensInput.disabled, true)
    assert.equal(unverifiedLensInput.placeholder, 'verified generation required')
    assert.equal(button('Create lens · paid').disabled, true,
      'paid lens work fails closed without an exact displayed run generation')
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
    const expandRows = button('Expand concept rows')
    assert.ok(expandRows, 'the prototype-key crash path must actually expand the concept outline')
    await act(async () => { expandRows.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); await settle() })
    const cids = [...document.querySelectorAll('.cv-cid')].map(n => n.textContent)
    assert.ok(cids.includes('constructor'), 'the prototype-key intermediate row rendered')
    assert.ok(cids.includes('foo'), 'the child row rendered only after the outline was expanded')
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
