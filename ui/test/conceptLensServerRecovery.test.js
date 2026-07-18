import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

import { acquireConceptLensIntent } from '../src/conceptLensRecovery.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const GENERATION_A = 'a'.repeat(64)
const GENERATION_B = 'b'.repeat(64)
const REQUEST_ID = 'c'.repeat(64)
const JOB_ID = 'd'.repeat(16)
const limits = {
  membership_nodes: 2048, concepts_per_node: 64, memberships: 8192,
  tree_nodes: 4096, edges: 2048, edge_endpoints: 4096,
}

function frame(runId, generation, { lens = 'is_a', derived = false } = {}) {
  const id = derived ? 'loss/contrastive' : 'loss/root'
  const rels = derived ? ['uses'] : ['is_a']
  const kind = derived ? 'edge' : 'path'
  const lenses = [
    { name: 'is_a', label: 'Family / is-a', rels: ['is_a'], kind: 'path' },
    { name: 'uses', label: 'Usage / uses', rels: ['uses'], kind: 'edge' },
  ]
  const treeNodes = derived ? {
    [id]: { parent: null, depth: 0, children: [], tagged: true, cross_parents: [] },
  } : {
    loss: { parent: null, depth: 0, children: [id], tagged: false },
    [id]: { parent: 'loss', depth: 1, children: [], tagged: true },
  }
  const registration = derived ? 'ephemeral-validated' : 'shipped'
  return {
    schema: 1, status: 'complete', run_id: runId, generation,
    requested_seq: null, captured_seq: 4, max_seq: 4, historical: false,
    lens, effective_lens: lens, requested_lens: lens,
    requested_lens_spec: { name: lens, rels, kind, registration },
    lens_contract: { requested: lens, effective: lens, registration, fallback: null },
    lenses, edges_present: derived, lens_edges_present: derived,
    touch: { [id]: 1 }, tree: { lens, roots: [derived ? id : 'loss'], nodes: treeNodes },
    metrics: { baseline: 0.5, direction: 'max', rows: { [id]: {
      touched: 1, evaluated: 1, best: 0.7, mean: 0.7, worst: 0.7,
      delta_best: 0.2, delta_mean: 0.2, first_touch: 0,
    } } },
    experiment_refs: { [id]: [{
      node_id: 0, node_generation: 0, metric: 0.7, metric_kind: 'robust_metric',
      status: 'evaluated', feasible: true, is_best: true,
      membership_provenance: 'researcher-authored',
    }] },
    authoritative: true,
    authority: { authoritative: true, source_authoritative: true, complete: true,
      scope: 'captured_recoverable_event_prefix', semantic_claims_verified: false },
    provenance: { source: 'events.jsonl', projection: 'event_log_fold',
      membership_semantics: 'recorded_claims', membership_counts: { 'researcher-authored': 1 } },
    complete: true,
    completeness: {
      complete: true, truncated: false, reasons: [], limits,
      source: { membership_nodes: 1, edges: derived ? 1 : 0 },
      included: { membership_nodes: 1, memberships: 1, concepts: 1,
        tree_nodes: derived ? 1 : 2, edges: derived ? 1 : 0, experiment_refs: 1 },
      source_integrity: { complete: true, generation_identified: true },
    },
  }
}

function emptyFrame(runId, generation) {
  const value = frame(runId, generation)
  return {
    ...value,
    touch: {}, tree: { lens: 'is_a', roots: [], nodes: {} },
    metrics: { ...value.metrics, rows: {} }, experiment_refs: {},
    provenance: { ...value.provenance, membership_counts: {} },
    completeness: {
      ...value.completeness,
      source: { membership_nodes: 0, edges: 0 },
      included: { membership_nodes: 0, memberships: 0, concepts: 0,
        tree_nodes: 0, edges: 0, experiment_refs: 0 },
    },
  }
}

const abandoned = (runId, generation) => ({
  ...frame(runId, generation), ok: false, code: 'concept_lens_abandoned',
  reason: 'operator_recovered_abandon', abandoned: true, resolved: true,
  provider_outcome: 'unknown', billing_status: 'unknown',
  warning: 'The provider may already have completed and billed this request.',
  request_id: REQUEST_ID, seq: 9,
})

const response = (payload, status = 200) => ({
  ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
})

test('ConceptView reconciles lost paid receipts before enabling any new provider identity', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const native = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout }
  const calls = []
  const recoveryByRun = new Map()
  let delayedRecovery = null
  let paidPosts = 0
  let jobResult = null
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
    CustomEvent: dom.window.CustomEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: callback => native.setTimeout(callback, 0),
    cancelAnimationFrame: handle => native.clearTimeout(handle),
    fetch: async (url, options = {}) => {
      const href = String(url)
      const parsed = new URL(href, 'https://looplab.test')
      const segments = parsed.pathname.split('/')
      const runId = decodeURIComponent(segments[3] || '')
      calls.push({ href, options, runId })
      if (parsed.pathname.endsWith('/concepts/lens/recovery/abandon')) {
        const body = JSON.parse(options.body)
        assert.equal(options.headers['Idempotency-Key'], undefined)
        assert.match(options.headers['Resolution-Idempotency-Key'],
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
        assert.deepEqual(body, { expected_generation: GENERATION_A,
          request_id: REQUEST_ID, expected_started_seq: 8 })
        return response(abandoned(runId, body.expected_generation))
      }
      if (parsed.pathname.endsWith('/concepts/lens')) {
        paidPosts += 1
        return response({ detail: { code: 'unexpected_paid_dispatch' } }, 500)
      }
      if (parsed.pathname.includes('/api/jobs/')) {
        assert.equal(options.headers?.['Idempotency-Key'], undefined)
        return response(jobResult || { ...abandoned('running', GENERATION_A), status: 'done' })
      }
      if (parsed.pathname.endsWith('/concepts/lens/recovery')) {
        assert.equal(options.method || 'GET', 'GET')
        assert.equal(options.headers?.['Idempotency-Key'], undefined)
        const generation = parsed.searchParams.get('expected_generation')
        if (runId === 'fence' && generation === GENERATION_A) {
          return new Promise(resolve => { delayedRecovery = resolve })
        }
        const configured = recoveryByRun.get(runId)
        if (configured?.error) return response({ detail: {
          code: 'recovery_unavailable', message: 'unavailable',
        } }, 503)
        return response(typeof configured === 'function'
          ? configured(generation) : configured || { schema: 1, generation, state: 'none' })
      }
      if (parsed.pathname.endsWith('/concepts')) {
        const generation = recoveryByRun.get(`${runId}:generation`) || GENERATION_A
        const requestedLens = parsed.searchParams.get('lens') || 'is_a'
        const configuredFrame = recoveryByRun.get(`${runId}:frame`)
        return response(configuredFrame || frame(runId, generation, {
          lens: requestedLens, derived: requestedLens !== 'is_a',
        }))
      }
      throw new Error(`unexpected URL ${href}`)
    },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root, vite
  const flush = async () => {
    for (let i = 0; i < 12; i += 1) await Promise.resolve()
    await new Promise(resolve => native.setTimeout(resolve, 0))
    for (let i = 0; i < 12; i += 1) await Promise.resolve()
  }
  const button = text => [...document.querySelectorAll('button')]
    .find(node => node.textContent.trim() === text)
  const input = () => document.querySelector('[aria-label="Describe a lens to create"]')
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom',
      logLevel: 'silent', server: { middlewareMode: true } })
    const [{ createRoot }, conceptModule, runMode] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ConceptView.jsx'),
      vite.ssrLoadModule('/src/runMode.js'),
    ])
    root = createRoot(document.getElementById('root'))
    let runId = 'local'
    let generation = GENERATION_A
    const state = { direction: 'max', engine_running: true, best_node_id: 0,
      node_concepts: { 0: ['loss/root'] }, node_concept_provenance: { 0: 'researcher-authored' },
      concept_consolidation: {}, concept_edges: {},
      nodes: { 0: { id: 0, attempt: 0, status: 'evaluated', metric: 0.7,
        confirmed_mean: null, feasible: true, idea: {} } } }
    const render = () => act(async () => {
      root.render(React.createElement(conceptModule.default, {
        runId, generation, sequence: null, state, onPickNode() {},
      }))
      await flush()
    })

    acquireConceptLensIntent(runId, generation, 'saved local prompt', dom.window.sessionStorage)
    await render()
    assert.ok(button('Resume paid lens'), 'a local receipt has priority over server discovery')
    assert.equal(calls.filter(call => call.runId === runId
      && call.href.includes('/lens/recovery?')).length, 0)

    dom.window.sessionStorage.clear()
    runId = 'orphan'
    recoveryByRun.set(runId, { schema: 1, generation, state: 'orphaned',
      request_id: REQUEST_ID, started_seq: 8, input_seq: 7 })
    await render()
    assert.equal(input().disabled, true)
    const resolveButton = button('Resolve orphaned paid claim')
    assert.ok(resolveButton)
    const abandonCallsBefore = calls.filter(call =>
      call.href.endsWith('/concepts/lens/recovery/abandon')).length
    await act(async () => {
      resolveButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      resolveButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await flush()
    })
    assert.equal(calls.filter(call =>
      call.href.endsWith('/concepts/lens/recovery/abandon')).length - abandonCallsBefore, 1,
    'same-tick double activation resolves one durable claim exactly once')
    assert.equal(input().disabled, false, 'a validated durable terminal releases the new-action fence')
    assert.match(document.querySelector('#paid-concept-lens-status').textContent,
      /durable claim is abandoned.*billing.*unknown.*no provider retry/i)

    runId = 'done-uncertain'
    jobResult = {
      ...frame(runId, GENERATION_A), ok: false, code: 'concept_lens_uncertain',
      error_kind: 'uncertain', error: 'The durable terminal is unavailable.',
      generation: GENERATION_A, request_id: REQUEST_ID, ambiguous: true, status: 'done',
    }
    recoveryByRun.set(runId, { schema: 1, generation, state: 'running', status: 'done',
      request_id: REQUEST_ID, started_seq: 8, input_seq: 7, job_id: JOB_ID })
    const recoveryReadsBefore = calls.filter(call => call.runId === runId
      && call.href.includes('/lens/recovery?')).length
    await render()
    assert.equal(input().disabled, true)
    assert.ok(button('Resolve orphaned paid claim'),
      'a freshly reconfirmed done-but-uncertain job becomes explicitly resolvable')
    assert.equal(calls.filter(call => call.runId === runId
      && call.href.includes('/lens/recovery?')).length - recoveryReadsBefore, 2,
    'an ambiguous done receipt is followed by exactly one fresh owner-plane ledger read')
    assert.equal(paidPosts, 0, 'done-job recovery never replays provider work')
    jobResult = null

    runId = 'running'
    recoveryByRun.set(runId, { schema: 1, generation, state: 'running', status: 'done',
      request_id: REQUEST_ID, started_seq: 8, input_seq: 7, job_id: JOB_ID })
    await render()
    assert.equal(input().disabled, false)
    assert.ok(calls.some(call => call.href.endsWith(`/api/jobs/${JOB_ID}`)),
      'status=done discovery still polls the retained exact job receipt')

    runId = 'terminal-derived'
    recoveryByRun.set(runId, currentGeneration => ({
      schema: 1, generation: currentGeneration, state: 'terminal', request_id: REQUEST_ID,
      started_seq: 8, input_seq: 7,
      terminal: { ...frame(runId, currentGeneration, { lens: 'usage', derived: true }),
        ok: true, spec: { name: 'usage', label: 'By usage', rels: ['uses'],
          kind: 'edge', provenance: 'agent' }, request_id: REQUEST_ID, seq: 9 },
    }))
    await render()
    assert.equal(document.querySelector('[aria-label="Concept relationship lens"]').value, 'usage')
    assert.match(document.querySelector('#paid-concept-lens-status').textContent,
      /Recovered a validated paid lens.*No provider request was replayed/i)

    runId = 'terminal-empty'
    recoveryByRun.set(`${runId}:frame`, emptyFrame(runId, generation))
    recoveryByRun.set(runId, currentGeneration => ({
      schema: 1, generation: currentGeneration, state: 'terminal', request_id: REQUEST_ID,
      started_seq: 8, input_seq: 7, terminal: abandoned(runId, currentGeneration),
    }))
    await render()
    await act(async () => { await flush(); await flush() })
    assert.match(document.body.textContent, /No concepts have been tagged yet/i)
    assert.match(document.querySelector('.cv-state-warning[role="status"]').textContent,
      /durable claim is abandoned.*no provider retry/i,
    'terminal recovery remains visible when the ordinary concept frame renders its empty state')

    runId = 'conflict'
    recoveryByRun.set(runId, { schema: 1, generation, state: 'conflict',
      code: 'concept_lens_recovery_conflict', message: 'repair required' })
    await render()
    assert.equal(input().disabled, true)
    assert.ok(button('Recheck paid recovery'))

    runId = 'read-error'
    recoveryByRun.set(runId, { error: true })
    await render()
    assert.equal(input().disabled, true)
    assert.match(document.querySelector('#paid-concept-lens-status').textContent,
      /could not be verified.*disabled/i)

    Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
      getItem() { throw new DOMException('blocked', 'SecurityError') },
      setItem() { throw new DOMException('blocked', 'SecurityError') },
    } })
    runId = 'storage-blocked'
    await render()
    assert.equal(input().disabled, true)
    assert.match(document.querySelector('#paid-concept-lens-status').textContent,
      /Server receipts are reconciled.*cannot save one request identity/i)

    Object.defineProperty(globalThis, 'sessionStorage', {
      configurable: true, writable: true, value: dom.window.sessionStorage,
    })
    dom.window.sessionStorage.clear()
    runId = 'stale-link'
    await act(async () => {
      runMode.setRunAccess(runId, { readOnly: true, mode: 'stale-link' })
      await flush()
    })
    await render()
    assert.equal(input().disabled, true)
    await act(async () => {
      input().closest('form').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }))
      await flush()
    })
    assert.match(document.querySelector('[role="alert"]').textContent,
      /earlier generation.*no provider request was sent/i)
    assert.equal(dom.window.sessionStorage.length, 0,
      'a stale diagnostic link cannot stage a paid identity')
    assert.equal(paidPosts, 0)
    await act(async () => { runMode.clearRunAccess(runId); await flush() })

    runId = 'fence'
    generation = GENERATION_A
    recoveryByRun.set(`${runId}:generation`, generation)
    await render()
    assert.equal(input().disabled, true)
    generation = GENERATION_B
    recoveryByRun.set(`${runId}:generation`, generation)
    await render()
    assert.equal(input().disabled, false)
    await act(async () => {
      delayedRecovery(response({ schema: 1, generation: GENERATION_A, state: 'orphaned',
        request_id: REQUEST_ID, started_seq: 8, input_seq: 7 }))
      await flush()
    })
    assert.equal(button('Resolve orphaned paid claim'), undefined,
      'a late recovery response cannot cross the generation/navigation fence')
    assert.equal(paidPosts, 0, 'recovery never dispatched provider work')
  } finally {
    if (root) await act(async () => { root.unmount(); await flush() })
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})
