// The browser half of the run stream's state deltas (doc 52 row 29): the ops apply exactly as the
// server's differ means them, and a delta is refused — never guessed — when it is not against the
// snapshot the client holds.
import test from 'node:test'
import assert from 'node:assert/strict'

import { STATE_DELTA_VERSION, applyOps, applyStateDelta, deltaApplies } from '../src/stateDelta.js'

const held = {
  state: { nodes: { 0: { metric: null, status: 'pending' } }, best_node_id: null, rows: [1, 2] },
  seq: 4, generation: 'g'.repeat(64), event_count: 5,
}
const frame = (ops, extra = {}) => ({
  version: STATE_DELTA_VERSION, base_seq: 4, seq: 5, generation: held.generation, event_count: 6,
  ops, ...extra,
})

test('ops set leaves, add keys, replace lists whole and delete keys, without touching the base', () => {
  const before = JSON.stringify(held)
  const next = applyOps(held, [
    ['set', ['state', 'nodes', '0', 'metric'], 0.5],
    ['set', ['state', 'nodes', '0', 'status'], 'evaluated'],
    ['set', ['state', 'nodes', '1'], { status: 'pending' }],
    ['set', ['state', 'rows'], [1, 2, 3]],
    ['del', ['state', 'best_node_id']],
    ['set', ['seq'], 5],
  ])
  assert.deepEqual(next.state, {
    nodes: { 0: { metric: 0.5, status: 'evaluated' }, 1: { status: 'pending' } }, rows: [1, 2, 3],
  })
  assert.equal(next.seq, 5)
  assert.equal(JSON.stringify(held), before, 'the held payload is never mutated')
})

test('a delta applies only to the exact snapshot it was computed against', () => {
  const ops = [['set', ['seq'], 5], ['set', ['event_count'], 6]]
  const next = applyStateDelta(held, frame(ops))
  assert.equal(next.seq, 5)
  assert.equal(applyStateDelta(held, frame(ops, { base_seq: 3 })), null, 'wrong base')
  assert.equal(applyStateDelta(held, frame(ops, { version: 2 })), null, 'unknown version')
  assert.equal(applyStateDelta(held, frame([['set', ['seq'], 7]])), null, 'the ops must land on the declared seq')
  assert.equal(applyStateDelta(held, frame([['mov', ['seq'], 5]])), null, 'a malformed op')
  assert.equal(applyStateDelta(null, frame(ops)), null, 'nothing held yet')
  assert.equal(deltaApplies(frame(ops), 4), true)
  assert.equal(deltaApplies({ ops }, 4), false)
})

test('a root set replaces the whole payload and a root delete is refused', () => {
  const next = applyOps(held, [['set', [], { seq: 9, state: {} }]])
  assert.deepEqual(next, { seq: 9, state: {} })
  assert.throws(() => applyOps(held, [['del', []]]))
})
