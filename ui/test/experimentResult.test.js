import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { click, mountLive } from './_mount.js'
import { parentScoreDifference } from '../src/scoreComparison.js'

const reference = JSON.parse(await readFile(new URL('../../tests/fixtures/parent_comparison_v1.json', import.meta.url), 'utf8'))
const parent = { id: 0, attempt: 0, status: 'evaluated', feasible: true, metric: 0.4 }
const node = { id: 1, attempt: 0, status: 'evaluated', feasible: true, metric: 0.6, parent_ids: [0], ...reference }
const state = { direction: 'max', best_node_id: 1, nodes: { 0: parent, 1: node } }
const comparable = n => ({ ...n, metric_provenance: { comparability: { keys: { measured: 'inputs' } } } })
const stamped = (n, digest = 'a'.repeat(64)) => ({ ...comparable(n), metric_provenance: {
  ...comparable(n).metric_provenance, base_revision: { version: 1, complete: true, digest,
    node_id: n.id, generation: n.attempt, seed_event_seq: 1, file_count: 1, bytes: 10,
    scope: 'seeded_editables_before_mounts_and_overlay',
    archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } } } })
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
    assert.match(text(), /#0: 0.4.*Comparison is not established/)
    assert.doesNotMatch(text(), /improved|robust|significant improvement/)
    await click(mounted.container.querySelector('button'))
    assert.deepEqual(tabs, ['Metrics'])
    assert.equal(harness.fetch.calls.length, 0)
    await mounted.rerender({ node: { ...node, confirmed_mean: 0.55, confirmed_seeds: 3, confirmed_std: null },
      state: { ...state, direction: 'min' } })
    assert.match(text(), /Lower is better/)
    assert.match(text(), /3 repeat checks/)
    assert.match(text(), /Spread is not recorded/)
    assert.match(text(), /do not establish generalization/)
    await mounted.rerender({ node: { ...node, confirmed_mean: 0.55, confirmed_seeds: 1, confirmed_std: 0 }, state })
    assert.match(text(), /Standard deviation: 0/)
    assert.match(text(), /multiple successful repeats not established/)
  } finally { await mounted.unmount() }
})

test('parent comparison honors receipts, source changes and objective retargets', async () => {
  const mounted = await harness.mount(Result, { node: comparable(node),
    state: { ...state, nodes: { 0: comparable(parent), 1: comparable(node) } } })
  try {
    const text = () => mounted.container.textContent
    assert.match(text(), /Matching evaluation conditions recorded/)
    await mounted.rerender({ node: comparable(node), state: { ...state, nodes: { 1: comparable(node), 0: {
      ...parent, metric_provenance: { comparability: { keys: { measured: 'different-inputs' } } } } } } })
    assert.match(text(), /Do not compare: evaluation inputs differ/)
    await mounted.rerender({ node: { ...node, extra_metrics_provenance: { accuracy: 'declared' } },
      state: { ...state, objective_key: 'accuracy' } })
    assert.match(text(), /accuracy · Higher is better/)
    assert.match(text(), /declared/)
    assert.doesNotMatch(text(), /provenance unknown/)
    assert.match(text(), /Objective changed; inspect its source/)
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

test('Inspector interpretation follows the shared comparison across attempts, bases, Trust and min/max', async () => {
  const child = comparable(node), initial = comparable(parent)
  const view = (n = child, p = initial, extra = {}) => ({ node: n,
    state: { ...state, nodes: { 0: p, 1: n }, ...extra } })
  const mounted = await harness.mount(Result, view())
  try {
    for (const direction of ['min', 'max']) {
      const props = view({ ...child, confirmed_mean: 100 }, initial, { direction })
      await mounted.rerender(props)
      const d = parentScoreDifference(props.node, props.state.nodes, props.state)
      assert.ok(d > 0)
      assert.match(mounted.container.textContent, new RegExp(`Evaluation score is ${direction === 'min' ? 'worse' : 'better'} by 0.2`))
      assert.match(mounted.container.textContent, /Confirmation mean100/)
      const tie = view({ ...child, metric: 0.4 }, initial, { direction })
      await mounted.rerender(tie)
      assert.match(mounted.container.textContent, /Evaluation score matches the parent/)
    }
    const cases = [
      view(child, { ...initial, attempt: 1 }), view({ ...child, parent_comparison: null }),
      view({ ...child, parent_comparison: { version: 2, node_id: 0, attempt: 0 } }),
      view(child, initial, { breed_excluded: [0] }), view(child, initial, { breed_excluded: [1] }),
      view({ ...child, feasible: null }), view(child, { ...initial, feasible: false }),
      view(child, { ...initial, status: 'failed' }), view(child, initial, { aborted_nodes: [0] }),
      view(child, initial, { source_integrity: { complete: false } }),
      view(child, initial, { direction: null }), view(child, initial, { objective_key: 'accuracy' }),
      view(stamped(node), stamped(parent, 'b'.repeat(64))), view(child, stamped(parent)),
      view({ ...child, parent_ids: [0, 2] }), view({ ...child, metric: null, confirmed_mean: 0.2 }),
    ]
    for (const props of cases) {
      await mounted.rerender(props)
      assert.equal(parentScoreDifference(props.node, props.state.nodes, props.state), null)
      assert.doesNotMatch(mounted.container.textContent, /Matching evaluation conditions recorded|Evaluation score (is|matches)/)
    }
    await mounted.rerender(view(child, initial, { breed_excluded: [1] }))
    assert.match(mounted.container.textContent, /Excluded by Trust gate/)
    assert.doesNotMatch(mounted.container.textContent, /Selected by the engine/)
    await mounted.rerender(view(stamped(node), stamped(parent)))
    assert.match(mounted.container.textContent, /Matching evaluation conditions recorded/)
    assert.deepEqual(harness.fetch.calls, [])
  } finally { await mounted.unmount() }
})

test('stale or newer detail cannot borrow current state evidence or claim selection', async () => {
  const current = comparable(node)
  const currentState = { ...state, nodes: { 0: comparable(parent), 1: current } }
  const mounted = await harness.mount(Result, { node: current, state: currentState })
  try {
    assert.match(mounted.container.textContent, /Selected by the engine/)
    for (const detail of [{ ...current, attempt: 1 }, { ...current, metric: 0.8 },
      { ...current, status: 'pending' }, { ...current, parent_ids: [2] }]) {
      await mounted.rerender({ node: detail, state: currentState })
      assert.doesNotMatch(mounted.container.textContent, /Selected by the engine|Matching evaluation conditions recorded|Evaluation score (is|matches)/)
      assert.match(mounted.container.textContent, /Details and current result differ/)
    }
    await mounted.rerender({ node: stamped(node), state: currentState })
    assert.doesNotMatch(mounted.container.textContent, /Matching evaluation conditions recorded/)
    for (const seeds of [null, 0, 1, -1, 1.5, '3']) {
      await mounted.rerender({ node: { ...current, confirmed_mean: 0.5, confirmed_seeds: seeds }, state: currentState })
      assert.match(mounted.container.textContent, /multiple successful repeats not established/)
      assert.doesNotMatch(mounted.container.textContent, /successful seed/)
    }
    await mounted.rerender({ node: { ...current, parent_ids: '0' }, state: currentState })
    assert.match(mounted.container.textContent, /Parent references are unavailable/)
    assert.doesNotMatch(mounted.container.textContent, /No parent experiment is recorded/)
  } finally { await mounted.unmount() }
})
