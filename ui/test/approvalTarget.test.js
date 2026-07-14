import test from 'node:test'
import assert from 'node:assert/strict'

import { approvalCommandFor, pendingApprovalTarget } from '../src/runIndex.js'

const exactState = (overrides = {}) => ({
  phase: 'approval',
  awaiting_approval: true,
  approval_subject: 7,
  approval_generation: 4,
  best_node_id: 5,
  nodes: {
    5: { id: 5, attempt: 2, status: 'completed' },
    7: { id: 7, attempt: 4, status: 'completed' },
  },
  ...overrides,
})

test('approval binds to the exact pending subject and attempt, never the current best', () => {
  const state = exactState()
  assert.deepEqual(pendingApprovalTarget(state), { nodeId: 7, nodeGeneration: 4 })
  assert.equal(approvalCommandFor(state), '/approve #7')

  const zero = exactState({
    approval_subject: 0,
    approval_generation: 0,
    best_node_id: 5,
    nodes: { 0: { id: 0, attempt: 0, status: 'completed' }, 5: { id: 5, attempt: 2 } },
  })
  assert.deepEqual(pendingApprovalTarget(zero), { nodeId: 0, nodeGeneration: 0 })
  assert.equal(approvalCommandFor(zero), '/approve #0')
})

test('missing approval identity never falls back to best_node_id', () => {
  const cases = [
    exactState({ approval_subject: undefined }),
    exactState({ approval_generation: undefined }),
    exactState({ approval_subject: '7' }),
    exactState({ approval_generation: -1 }),
    exactState({ awaiting_approval: false }),
    exactState({ phase: 'search' }),
    exactState({ nodes: { 5: { id: 5, attempt: 2 } } }),
  ]
  for (const state of cases) {
    assert.equal(pendingApprovalTarget(state), null)
    assert.equal(approvalCommandFor(state), null)
  }
})

test('reset, tombstone, and abort invalidate a stale pending approval target', () => {
  const cases = [
    exactState({ nodes: { 7: { id: 7, attempt: 5, status: 'completed' } } }),
    exactState({ nodes: { 7: { id: 7, attempt: 4, tombstoned: true } } }),
    exactState({ nodes: { 7: { id: 7, attempt: 4, status: 'aborted' } } }),
  ]
  for (const state of cases) {
    assert.equal(pendingApprovalTarget(state), null)
    assert.equal(approvalCommandFor(state), null)
  }
})

test('spec ratification is distinct and also fails closed when its request is incomplete', () => {
  assert.equal(approvalCommandFor({
    phase: 'spec_approval', spec_approval_requested: true, spec_confirmed: false,
    best_node_id: 9,
  }), '/ratify')
  assert.equal(approvalCommandFor({
    phase: 'spec_approval', spec_approval_requested: false, spec_confirmed: false,
    best_node_id: 9,
  }), null)
  assert.equal(approvalCommandFor({
    phase: 'spec_approval', spec_approval_requested: true, spec_confirmed: true,
    best_node_id: 9,
  }), null)
})
