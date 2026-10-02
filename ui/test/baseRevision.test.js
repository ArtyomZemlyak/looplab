import test from 'node:test'
import assert from 'node:assert/strict'
import { nodeBase, baseChoices, baseMatches, capabilityOrigin } from '../src/baseRevision.js'

const digest = 'a'.repeat(64)
const node = { id: 1, attempt: 0, metric_provenance: { base_revision: { version: 1, complete: true, digest,
  node_id: 1, generation: 0, seed_event_seq: 2, file_count: 1, bytes: 10,
  scope: 'seeded_editables_before_mounts_and_overlay', archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } } } }
test('partial and legacy provenance is unknown, never filled from the promoted base', () => {
  assert.equal(nodeBase({}), null)
  assert.equal(nodeBase({ metric_provenance: { base_revision: { ...node.metric_provenance.base_revision, complete: false } } }), null)
  assert.equal(nodeBase(node)?.digest, digest)
  assert.equal(baseMatches({}, digest), false)
  assert.equal(baseMatches({}, 'unknown'), true)
  assert.equal(nodeBase({ ...node, attempt: 1 }), null)
  assert.equal(nodeBase({ ...node, metric_provenance: { base_revision: { ...node.metric_provenance.base_revision, bytes: null } } }), null)
})
test('base filtering preserves unknowns and ignores tombstones; capability provenance uses recorded origin', () => {
  assert.deepEqual(baseChoices({ 1: node, 2: {}, 3: { ...node, tombstoned: true } }), [{ digest, count: 1 }, { digest: 'unknown', count: 1 }])
  const origin = { type: 'base_advanced', selector: { digest }, source_node_id: 1 }
  assert.equal(capabilityOrigin({ upstream_history: [origin] }, digest), origin)
  assert.equal(capabilityOrigin({ upstream_history: [origin] }, 'b'.repeat(64)), null)
  assert.equal(capabilityOrigin({ upstream_base: origin }, digest), origin)
  assert.equal(capabilityOrigin({ upstream_history: [{ ...origin, type: 'upstream_proposed' }] }, digest), null)
})
