import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { mountLive, until } from './_mount.js'

const generation = 'a'.repeat(64)
const reference = JSON.parse(await readFile(new URL('../../tests/fixtures/parent_comparison_v1.json', import.meta.url), 'utf8'))
const node = (id, metric) => ({ id, attempt: 0, status: 'evaluated', feasible: true, metric,
  parent_ids: id ? [0] : [], ...(id ? reference : { parent_comparison: null }),
  operator: id ? 'improve' : 'draft', idea: { params: {}, rationale: '' },
  metric_provenance: { comparability: { keys: { measured: 'same' } } } })
const stamped = (n, digest) => ({ ...n, metric_provenance: { ...n.metric_provenance,
  base_revision: { version: 1, complete: true, digest, node_id: n.id, generation: n.attempt,
    seed_event_seq: 1, file_count: 1, bytes: 10, scope: 'seeded_editables_before_mounts_and_overlay',
    archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } } } })

test('real Inspector keeps current result/base evidence while terminal detail stays cached', async () => {
  const parent = node(0, 10), child = node(1, 7)
  const detail = { ...child, run_generation: generation, code: 'print(7)', files: {},
    annotations: [], confirm_seeds_detail: {}, trace: { nodes: [] }, trace_revision: null }
  const h = await mountLive({ visible: true, routes: { '/api/runs/r/nodes/1': detail } })
  try {
    const { default: Inspector } = await h.load('/src/Inspector.jsx')
    const initial = { run_id: 'r', direction: 'min', nodes: { 0: parent, 1: child }, best_node_id: 1 }
    const props = state => ({ runId: 'r', nodeId: 1, state, live: null, tab: 'Overview',
      setTab() {}, onToast() {}, expectedGeneration: generation })
    const mounted = await h.mount(Inspector, props(initial))
    const result = () => mounted.container.querySelector('[aria-label="Experiment result"]')
    await until(() => result()?.textContent.includes('Matching evaluation conditions recorded'), 'measured Inspector comparison')
    const detailReads = () => h.fetch.calls.filter(call => call.path === '/api/runs/r/nodes/1').length
    assert.equal(detailReads(), 1)

    await mounted.rerender(props({ ...initial, nodes: { 0: { ...parent, attempt: 1, status: 'pending', metric: null }, 1: child } }))
    assert.match(result().textContent, /parent attempt is unavailable or has changed/)
    assert.doesNotMatch(result().textContent, /Matching evaluation conditions recorded|Evaluation score is better/)

    const updated = { ...child, metric: 6, confirmed_mean: 5, confirmed_seeds: 3, confirmed_std: 0.1 }
    await mounted.rerender(props({ ...initial, nodes: { 0: parent, 1: updated } }))
    assert.match(result().textContent, /Evaluation score6/)
    assert.match(result().textContent, /Confirmation mean5.*3 repeat checks/)
    assert.match(result().textContent, /Evaluation score is better by 4/)
    assert.equal(detailReads(), 1, 'result evidence updates from state without adding a detail poll')

    const digest = 'b'.repeat(64)
    const changed = { ...initial, upstream_enabled: true,
      nodes: { 0: stamped(parent, 'a'.repeat(64)), 1: stamped(updated, digest) } }
    await mounted.rerender(props(changed))
    assert.match(result().textContent, /Comparison is not established/)
    assert.doesNotMatch(result().textContent, /Matching evaluation conditions recorded|Evaluation score is better/)
    assert.equal(mounted.container.querySelector('.base-revision code').getAttribute('title'), digest,
      'the adjacent archive identity must describe the same current result')
    await mounted.rerender(props({ ...changed, breed_excluded: [1] }))
    assert.match(result().textContent, /Excluded by Trust gate/)
    assert.doesNotMatch(result().textContent, /Selected by the engine/)
    assert.equal(detailReads(), 1)
    assert.ok(h.fetch.calls.every(call => call.method === 'GET'), 'no action or provider request is submitted')
  } finally { await h.close() }
})
