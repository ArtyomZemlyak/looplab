import test from 'node:test'
import assert from 'node:assert/strict'
import { delta } from '../src/lineageDelta.js'

const node = (id, metric, digest = 'a'.repeat(64), extra = {}) => ({
  id, attempt: 0, status: 'evaluated', feasible: true, metric, parent_ids: id ? [0] : [],
  parent_comparison: id ? { version: 1, node_id: 0, attempt: 0 } : null,
  metric_provenance: {
    comparability: { keys: { measured: 'same-inputs' } },
    base_revision: { version: 1, complete: true, digest, node_id: id, generation: 0,
      seed_event_seq: 2, file_count: 1, bytes: 10,
      scope: 'seeded_editables_before_mounts_and_overlay',
      archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } },
  }, ...extra,
})
const state = (child = node(1, 7), extra = {}) => ({ direction: 'min',
  upstream_enabled: true, nodes: { 0: node(0, 10), 1: child }, ...extra })

test('lineage arrows require comparable measured scores on the recorded base', () => {
  const same = state()
  assert.deepEqual(delta(same.nodes[1], same), { d: -3, improved: true })
  const max = state(undefined, { direction: 'max' })
  assert.deepEqual(delta(max.nodes[1], max), { d: -3, improved: false })
  for (const run of [
    state(node(1, 7, 'b'.repeat(64))),
    state(node(1, 7, undefined, { metric_provenance: null })),
    state(node(1, 7, undefined, { confirmed_mean: 6 })),
    state(node(1, 7, undefined, { feasible: false })),
    state(node(1, 7, undefined, { status: 'pending' })),
    state(node(1, 7, undefined, { tombstoned: true })),
    state(node(1, NaN)), state(undefined, { direction: null }),
    state(undefined, { source_integrity: { complete: false } }),
    state(undefined, { objective_key: 'accuracy' }),
    state(undefined, { breed_excluded: [1] }), state(undefined, { breed_excluded: [0] }),
    state(node(1, 7, undefined, { parent_comparison: { version: 1, node_id: 0, attempt: 1 } })),
    state(node(1, 7, undefined, { parent_comparison: undefined })),
    state(node(1, 7, undefined, { parent_ids: [0, 2] })),
  ]) assert.equal(delta(run.nodes[1], run), null)
})

test('matching base alone never establishes matching evaluation inputs', () => {
  const child = node(1, 7)
  child.metric_provenance.comparability.keys.measured = 'other-inputs'
  const run = state(child)
  assert.equal(delta(child, run), null)
  delete child.metric_provenance.comparability
  assert.equal(delta(child, run), null)
  assert.equal(delta(run.nodes[0], run), null)
})
