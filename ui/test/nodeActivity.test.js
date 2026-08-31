import test from 'node:test'
import assert from 'node:assert/strict'

import {
  NODE_ACTIVITY, nodeActivityStatus, nodeActivityView, partitionNodeWork, workingNodeIds,
} from '../src/nodeActivity.js'

const node = (id, status, attempt = 0) => ({ id, status: 'pending', attempt,
  activity: { schema: 1, status, generation: attempt } })

test('generation-scoped activity distinguishes build, evaluation, and queue', () => {
  const state = { engine_running: true, nodes: {
    1: node(1, 'building'), 2: node(2, 'evaluating'), 3: node(3, 'queued'),
  } }
  assert.equal(nodeActivityStatus(state.nodes[1], state), NODE_ACTIVITY.BUILDING)
  assert.equal(nodeActivityStatus(state.nodes[2], state), NODE_ACTIVITY.EVALUATING)
  assert.equal(nodeActivityStatus(state.nodes[3], state), NODE_ACTIVITY.QUEUED)
  assert.deepEqual([...workingNodeIds(state)], [1, 2])
  assert.deepEqual(partitionNodeWork(state), {
    building: [state.nodes[1]], evaluating: [state.nodes[2]], queued: [state.nodes[3]], unknown: [],
  })
})

test('parallel evaluations all remain working instead of choosing the highest id', () => {
  const state = { engine_running: true, nodes: {
    4: node(4, 'evaluating'), 9: node(9, 'queued'), 5: node(5, 'evaluating'),
  } }
  assert.deepEqual([...workingNodeIds(state)], [4, 5])
})

test('stale activity from an abandoned generation is not presented as training', () => {
  const reset = { id: 7, status: 'pending', attempt: 2,
    activity: { schema: 1, status: 'evaluating', generation: 1 } }
  assert.equal(nodeActivityStatus(reset, { engine_running: true, nodes: { 7: reset } }),
    NODE_ACTIVITY.PENDING)
})

test('a stopped engine turns durable start evidence into interrupted, never live work', () => {
  const evaluating = node(2, 'evaluating')
  const state = { engine_running: false, nodes: { 2: evaluating } }
  const view = nodeActivityView(evaluating, state)
  assert.equal(view.label, 'Evaluation interrupted · engine stopped')
  assert.equal(view.active, false)
  assert.deepEqual([...workingNodeIds(state)], [])
})

test('raw build marker overrides a pending node during an in-place rebuild', () => {
  const rebuilding = node(3, 'queued', 4)
  const state = { engine_running: true, nodes: { 3: rebuilding },
    buildings: { 3: { node_id: 3, generation: 4, operator: 'debug' } } }
  assert.equal(nodeActivityStatus(rebuilding, state), NODE_ACTIVITY.BUILDING)
  assert.deepEqual([...workingNodeIds(state)], [3])
})

test('legacy pending is explicitly unknown rather than guessed queued', () => {
  const legacy = { id: 1, status: 'pending', attempt: 0 }
  assert.equal(nodeActivityView(legacy, { engine_running: true, nodes: { 1: legacy } }).label,
    'Pending · evaluation start unknown')
})
