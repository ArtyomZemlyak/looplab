import test from 'node:test'
import assert from 'node:assert/strict'

import {
  NODE_ACTIVITY, nodeActivityStatus, nodeActivityView, partitionNodeWork, primaryWorkingNode,
  rankWork, workingNodeIds,
} from '../src/nodeActivity.js'
import { workingId } from '../src/util.js'

const node = (id, status, attempt = 0, startedAt = null) => ({ id, status: 'pending', attempt,
  activity: { schema: 1, status, generation: attempt,
    ...(startedAt == null ? {} : { started_at: startedAt }) } })

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

// ------------------------------------------------- WHICH experiment is running (BACKLOG §0.14)
// The one-subject answer used to be `Math.max` over the live ids, preferring any build. These drive
// the replacement rule against the shape that was measured: several nodes live at once, only some of
// them actually executing, and the highest number belonging to none of them.

test('the running experiment is named by evidence, never by the highest id', () => {
  // The v9 shape: #5 and #6 training (a process each on the box), #7 being built, #9 queued behind
  // them. `Math.max` said 9, then 7 once queued nodes stopped counting as work; both name a node on
  // which nothing is running.
  const state = { engine_running: true, nodes: {
    5: node(5, 'evaluating', 0, 1_000), 6: node(6, 'evaluating', 0, 1_400),
    7: node(7, 'building', 0, 2_000), 9: node(9, 'queued'),
  } }
  assert.deepEqual([...workingNodeIds(state)].sort((a, b) => a - b), [5, 6, 7])
  assert.equal(primaryWorkingNode(state), 5, 'the longest-running evaluation is the running experiment')
  assert.equal(workingId(state), 5, 'the util barrel answers with the one rule, not a copy of it')
})

test('a build is named only when nothing is evaluating', () => {
  const building = { engine_running: true, nodes: {
    2: node(2, 'building', 0, 5_000), 4: node(4, 'building', 0, 3_000), 8: node(8, 'queued'),
  } }
  assert.equal(primaryWorkingNode(building), 4, 'the oldest open build, not the newest id')
  assert.equal(primaryWorkingNode({ engine_running: true, nodes: { 8: node(8, 'queued') } }), null,
    'a queued node is not work in progress and nothing is named')
  assert.equal(primaryWorkingNode({ engine_running: false, nodes: { 2: node(2, 'evaluating', 0, 1) } }),
    null, 'a dead engine has no running experiment')
  assert.equal(primaryWorkingNode(null), null)
})

test('a record with no start receipt never outranks one that carries a clock', () => {
  // Silence is not evidence — the same rule `pending` gets. An older server (or a windowed log whose
  // start row scrolled out) sends `evaluating` with no `started_at`; it stays nameable, but only when
  // nothing better is on offer.
  const mixed = { engine_running: true, nodes: {
    3: node(3, 'evaluating'), 6: node(6, 'evaluating', 0, 9_000),
  } }
  assert.equal(primaryWorkingNode(mixed), 6)
  const silent = { engine_running: true, nodes: { 3: node(3, 'evaluating'), 6: node(6, 'evaluating') } }
  assert.equal(primaryWorkingNode(silent), 6, 'nothing separates them, so the old tiebreak stands')
})

test('the ordering is statable on its own and sorts a live set the same way', () => {
  const rows = [
    { id: 9, lane: 0, started: null },      // live, but neither lane claims it
    { id: 1, lane: 1, started: 10 },        // building
    { id: 4, lane: 2, started: null },      // evaluating, no clock
    { id: 5, lane: 2, started: 50 },        // evaluating, started later
    { id: 2, lane: 2, started: 20 },        // evaluating, started first
  ]
  assert.deepEqual([...rows].sort(rankWork).map(row => row.id), [2, 5, 4, 1, 9])
  assert.equal(rankWork({ id: 3, lane: 2, started: 7 }, { id: 3, lane: 2, started: 7 }), 0)
})
