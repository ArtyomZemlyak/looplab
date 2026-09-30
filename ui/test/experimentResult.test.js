import test from 'node:test'
import assert from 'node:assert/strict'
import { click, mountLive } from './_mount.js'

const parent = { id: 0, status: 'evaluated', feasible: true, metric: 0.4 }
const node = { id: 1, status: 'evaluated', feasible: true, metric: 0.6, parent_ids: [0] }
const state = { direction: 'max', best_node_id: 1, nodes: { 0: parent, 1: node } }
const comparable = n => ({ ...n, metric_provenance: { comparability: { keys: { measured: 'inputs' } } } })
let harness
let Result
test.before(async () => {
  harness = await mountLive()
  ;({ default: Result } = await harness.load('/src/ExperimentResult.jsx'))
})
test.after(async () => { await harness?.close() })

test('score, direction, confirmation and evidence navigation are visible without a model request', async () => {
  const tabs = []
  const mounted = await harness.mount(Result, { node, state, onTab: tab => tabs.push(tab) })
  try {
    const text = () => mounted.container.textContent
    assert.match(text(), /Higher is better/)
    assert.match(text(), /Selected by the engine/)
    assert.match(text(), /Not confirmed/)
    assert.match(text(), /#0: 0.4.*conditions are not established/)
    assert.doesNotMatch(text(), /improved|robust|significant improvement/)
    await click(mounted.container.querySelector('button'))
    assert.deepEqual(tabs, ['Metrics'])
    assert.equal(harness.fetch.calls.length, 0)
    await mounted.rerender({ node: { ...node, confirmed_mean: 0.55, confirmed_seeds: 3, confirmed_std: null },
      state: { ...state, direction: 'min' } })
    assert.match(text(), /Lower is better/)
    assert.match(text(), /3 successful seeds/)
    assert.match(text(), /Spread is not recorded/)
    assert.match(text(), /do not establish generalization/)
    await mounted.rerender({ node: { ...node, confirmed_mean: 0.55, confirmed_seeds: 1, confirmed_std: 0 }, state })
    assert.match(text(), /Standard deviation: 0/)
    assert.match(text(), /Multiple successful repeat checks are not established/)
  } finally { await mounted.unmount() }
})

test('parent comparison honors receipts, source changes and objective retargets', async () => {
  const mounted = await harness.mount(Result, { node: comparable(node),
    state: { ...state, nodes: { 0: comparable(parent) } } })
  try {
    const text = () => mounted.container.textContent
    assert.match(text(), /matching evaluation conditions recorded/)
    await mounted.rerender({ node: comparable(node), state: { ...state, nodes: { 0: {
      ...parent, metric_provenance: { comparability: { keys: { measured: 'different-inputs' } } } } } } })
    assert.match(text(), /do not compare: evaluation inputs differ/)
    await mounted.rerender({ node: { ...node, extra_metrics_provenance: { accuracy: 'declared' } },
      state: { ...state, objective_key: 'accuracy' } })
    assert.match(text(), /accuracy · Higher is better/)
    assert.match(text(), /declared/)
    assert.doesNotMatch(text(), /provenance unknown/)
    assert.match(text(), /objective changed; inspect its source/)
  } finally { await mounted.unmount() }
})

test('unfinished, removed, infeasible and salvaged results cannot look like a successful winner', async () => {
  const mounted = await harness.mount(Result, { node, state })
  try {
    const text = () => mounted.container.textContent
    for (const status of ['running', 'pending', 'failed']) {
      await mounted.rerender({ node: { ...node, status, confirmed_mean: 42 }, state })
      assert.doesNotMatch(text(), /Selected by the engine|42|0.6/)
      assert.match(text(), /No completed evaluation yet|Evaluation failed/)
    }
    await mounted.rerender({ node, state: { ...state, aborted_nodes: [1] } })
    assert.match(text(), /Removed from active results/)
    assert.doesNotMatch(text(), /Selected by the engine/)
    await mounted.rerender({ node: { ...node, feasible: false }, state: { ...state, best_node_id: null } })
    assert.match(text(), /Constraint violation.*excluded from winner selection/)
    await mounted.rerender({ node: { ...node, metric_provenance: { salvaged: true, stage: 'score' } }, state })
    assert.match(text(), /salvaged, admitted for selection/)
    assert.match(text(), /NOT measured/)
    await mounted.rerender({ node: { ...node, metric: NaN, parent_ids: [] }, state: { direction: null } })
    assert.match(text(), /direction is not recorded/)
    assert.match(text(), /not automatically a task baseline/)
    assert.doesNotMatch(text(), /NaN/)
  } finally { await mounted.unmount() }
})
