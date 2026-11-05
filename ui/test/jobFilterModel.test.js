import { test } from 'node:test'
import assert from 'node:assert/strict'
import { filterJobNodes, jobChoices, jobLabel, jobMatches } from '../src/jobFilterModel.js'
import { resourceSnapshot } from '../src/resourceSnapshotModel.js'

const nodes = {
  0: { id: 0, kind: 'artifact', job_role: 'profile', stage_attempts: [],
    extra_metrics: { warm_maxrankwall_s: 1.25 } },
  1: { id: 1, kind: null, job_role: 'fit', stage_attempts: [], extra_metrics: {} },
  2: { id: 2, kind: 'artifact', job_role: null, stage_attempts: [], extra_metrics: {} },
}

test('job choices, matching and labels separate roles from artifact kind', () => {
  assert.deepEqual(jobChoices(nodes), ['artifact', 'fit', 'profile'])
  assert.equal(jobLabel(nodes[0]), 'profile')
  assert.equal(jobLabel(nodes[2]), 'artifact')
  assert.equal(jobMatches(nodes[0], 'profile'), true)
  assert.equal(jobMatches(nodes[0], 'fit'), false)
  assert.deepEqual(filterJobNodes(nodes, 'profile'), [0])
  assert.deepEqual(filterJobNodes(nodes, 'artifact'), [2])
})

test('resource snapshot exposes job role and outputs with their evidence channel', () => {
  const snap = resourceSnapshot({ ...nodes[0],
    extra_metrics: { warm_maxrankwall_s: 1.25, stdout_tokens_s: 900 },
    extra_metrics_provenance: { warm_maxrankwall_s: 'declared', stdout_tokens_s: 'auto' },
    extra_metrics_backfill: { backfilled: true, keys: ['warm_maxrankwall_s'] },
  })
  assert.equal(snap.jobRole, 'profile')
  assert.deepEqual(snap.jobOutputs, [
    { key: 'stdout_tokens_s', value: 900, channel: 'auto', reconstructed: false },
    { key: 'warm_maxrankwall_s', value: 1.25, channel: 'declared', reconstructed: true },
  ])
  assert.equal(resourceSnapshot(nodes[2]).jobOutputs.length, 0, 'unknown values stay absent')
})
