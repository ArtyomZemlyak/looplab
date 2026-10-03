import test from 'node:test'
import assert from 'node:assert/strict'
import { currentResultNode, confirmationSeedResults } from '../src/resultEvidence.js'
import { fetchStub, mountLive, until } from './_mount.js'

const generation = 'a'.repeat(64)
const provenance = { comparability: { keys: { measured: 'same' } } }
const node = (id, metric) => ({ id, attempt: 0, status: 'evaluated', feasible: true, metric,
  parent_ids: id ? [0] : [], parent_comparison: id ? { version: 1, node_id: 0, attempt: 0 } : null,
  operator: id ? 'improve' : 'draft', idea: { params: {}, rationale: '' }, metric_provenance: provenance })
const cached = { ...node(1, 1.2), run_generation: generation, code: 'print(1.2)', files: {},
  annotations: [], confirmed_mean: .9, confirmed_std: .2, confirmed_seeds: 2,
  confirm_seeds_detail: { 10: .7, 11: 1.1 }, trace: { nodes: [] }, trace_revision: null }
const current = { ...node(1, 1.2), confirmed_mean: .8, confirmed_std: .1, confirmed_seeds: 3 }
const stateFor = n => ({ run_id: 'r', direction: 'min', nodes: { 0: node(0, 1), 1: n }, best_node_id: 1 })

test('result and seed projections retain complete lifecycle records rather than mixing fields', () => {
  const state = stateFor(current)
  assert.equal(currentResultNode(cached, state), current)
  for (const patch of [{ attempt: 1 }, { status: 'pending' }, { id: 2 }])
    assert.equal(currentResultNode(cached, { ...state, nodes: { 1: { ...current, ...patch } } }), cached)
  const legacy = { ...cached, attempt: undefined }
  assert.equal(currentResultNode(legacy, state), legacy)
  assert.deepEqual(confirmationSeedResults(current, cached, state), {}, 'stale detail seeds cannot accompany a new aggregate')
  const recorded = { 10: .7, 11: .8, 12: .9 }
  assert.equal(confirmationSeedResults(current, cached, { ...state, confirm_seed_results: { 1: recorded } }), recorded)
  assert.equal(confirmationSeedResults(cached, cached, { ...state, nodes: { 1: { ...current, attempt: 1 } },
    confirm_seed_results: { 1: { 99: 42 } } }), cached.confirm_seeds_detail, 'a newer attempt cannot supply the older attempt’s seeds')
})

test('Metrics follows state evidence while a terminal detail read stays cached', async () => {
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1': cached,
    '/api/runs/r/nodes/1/metrics': { node_id: 1, attempt: 0, metrics: {} },
  } })
  try {
    const { default: Inspector } = await h.load('/src/Inspector.jsx')
    const props = state => ({ runId: 'r', nodeId: 1, state, live: null, tab: 'Metrics',
      setTab() {}, onToast() {}, expectedGeneration: generation })
    const view = await h.mount(Inspector, props(stateFor(cached)))
    const result = () => view.container.querySelector('[aria-label="Experiment result"]')
    const table = caption => [...view.container.querySelectorAll('table')].find(table => table.querySelector('caption')?.textContent === caption)
    try {
      await until(() => result()?.textContent.includes('Confirmation mean0.9'), 'cached measured result')
      const reads = () => h.fetch.calls.filter(call => call.path === '/api/runs/r/nodes/1').length
      assert.equal(reads(), 1)
      await view.rerender(props(stateFor(current)))
      assert.match(result().textContent, /Evaluation score1.2.*Confirmation mean0.8.*Standard deviation: 0.1/)
      assert.match(result().textContent, /Evaluation score is worse by 0.2/)
      assert.doesNotMatch(result().textContent, /Confirmation mean0.9/)
      assert.equal(table('Per-seed confirmation metrics'), undefined, 'old seed readings are withheld')
      const metrics = table('Node metric comparison')
      assert.match(metrics.textContent, /0.8 confirmation mean/)
      assert.equal(reads(), 1, 'new aggregates do not add a detail poll')
      await view.rerender(props({ ...stateFor(current), confirm_seed_results: { 1: { 10: .7, 11: .8, 12: .9,
        invalid: 42, 13: true, 14: '100', 15: null } } }))
      assert.match(table('Per-seed confirmation metrics').textContent, /100.7.*110.8.*120.9/)
      assert.equal(table('Per-seed confirmation metrics').querySelectorAll('tbody tr').length, 3)

      const retargeted = { ...current, metric: 2, confirmed_mean: null, confirmed_std: null,
        confirmed_seeds: null, task_metric: 1.2, extra_metrics: { latency: 2 }, extra_metrics_provenance: { latency: 'declared' } }
      await view.rerender(props({ ...stateFor(retargeted), objective_key: 'latency', confirm_seed_results: {} }))
      assert.match(result().textContent, /latency.*Evaluation score2.*Not confirmed/)
      assert.doesNotMatch(table('Node metric comparison').textContent, /confirmation mean|0.9|0.8/)
      assert.equal(table('Per-seed confirmation metrics'), undefined)
      assert.equal(reads(), 1)
      assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('ranked cells name measurement types independently for this node and the champion', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { Metrics } = await h.load('/src/Inspector.jsx')
    const state = { ...stateFor(current), best_node_id: 0 }
    const view = await h.mount(Metrics, { n: current, detail: {}, state, runId: 'r' })
    try {
      const cells = view.container.querySelector('tr.chosen-row').querySelectorAll('td')
      assert.match(cells[2].textContent, /0.8 confirmation mean/)
      assert.match(cells[3].textContent, /1 evaluation score/)
      assert.equal(view.container.querySelectorAll('.experiment-result').length, 1)
      assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('an exact node link refuses a fresher detail reply while an unpinned selection accepts it', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { default: Inspector } = await h.load('/src/Inspector.jsx')
    const newer = { ...cached, attempt: 1, metric: 42, confirmed_mean: null, confirmed_std: null, confirmed_seeds: null }
    const curveAttempts = []
    const backend = fetchStub({ '/api/runs/r/nodes/1': newer,
      '/api/runs/r/nodes/1/metrics': ({ url }) => {
        const attempt = Number(url.searchParams.get('attempt')); curveAttempts.push(attempt)
        return { node_id: 1, attempt, metrics: {} }
      } })
    globalThis.fetch = backend
    const props = { runId: 'r', nodeId: 1, state: stateFor(current), live: null, tab: 'Metrics',
      setTab() {}, onToast() {}, expectedGeneration: generation }
    const view = await h.mount(Inspector, { ...props, expectedAttempt: 0 })
    try {
      await until(() => view.container.textContent.includes('attempt changed while details were loading'), 'newer detail refused')
      assert.doesNotMatch(view.container.textContent, /Evaluation score42/)
      assert.equal(curveAttempts.includes(1), false)
      await view.rerender({ ...props, expectedAttempt: null })
      await until(() => view.container.textContent.includes('Evaluation score42'), 'unpinned current selection accepts fresher detail')
      assert.doesNotMatch(view.container.textContent, /Selected by the engine/)
      assert.ok(backend.calls.every(call => call.method === 'GET'))
      assert.equal(backend.calls.filter(call => call.path === '/api/runs/r/nodes/1').length, 2,
        'changing exact/live scope triggers one new owned read')
    } finally { await view.unmount() }
  } finally { await h.close() }
})
