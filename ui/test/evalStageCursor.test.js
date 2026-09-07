import test from 'node:test'
import assert from 'node:assert/strict'

import {
  evalStageFor, evalStageLabel, evalStageShortLabel, evalStages, livePhase, openPhases, phaseLabel,
} from '../src/buildingModel.js'
import { NODE_ACTIVITY, nodeActivityView } from '../src/nodeActivity.js'
import { nodeClass } from '../src/util.js'
import { groupAggregate } from '../src/grouping.js'

// One beacon row, in the shape `engine/eval_stages.py::_stage_progress_fn` appends.
const beacon = (over = {}) => ({
  type: 'phase_progress',
  ts: over.ts ?? 100,
  data: {
    stage: 'eval', phase: 'stage', status: 'started', node_id: 5, generation: 0,
    name: 'train', index: 1, total: 3, role: 'work', ...over,
  },
})

const evaluating = (id = 5, attempt = 0) => ({
  id, status: 'pending', attempt,
  activity: { schema: 1, status: 'evaluating', generation: attempt, evidence: 'node_eval_started' },
})

const live = { engine_running: true }

test('the cursor names the running step per node', () => {
  const stages = evalStages([beacon()])
  assert.equal(stages.size, 1)
  assert.equal(stages.get(5).name, 'train')
  assert.equal(stages.get(5).total, 3)
})

test('a closed beacon leaves no node claiming a running stage', () => {
  // The whole reason `openPhases` is a fold and not a scan for the last row: an unclosed cursor
  // reads as live work forever, so a CLOSED one must disappear entirely.
  assert.equal(evalStages([beacon(), beacon({ status: 'finished' })]).size, 0)
})

test('a stage that ends and another that begins replace, never accumulate', () => {
  const stages = evalStages([
    beacon({ name: 'mine', index: 0 }),
    beacon({ name: 'mine', index: 0, status: 'finished' }),
    beacon({ name: 'train', index: 1, ts: 200 }),
  ])
  assert.equal(stages.size, 1)
  assert.equal(stages.get(5).name, 'train')
})

test('a stale generation never claims the new lifecycle', () => {
  // Same rule `markerFor` applies to build markers: a reset's abandoned lifecycle and its
  // replacement are different experiments and must not inherit each other's cursor.
  const log = [beacon({ generation: 0 })]
  assert.equal(evalStageFor(evaluating(5, 0), log)?.name, 'train')
  assert.equal(evalStageFor(evaluating(5, 1), log), null)
})

test('a legacy beacon with no generation is still accepted', () => {
  const log = [beacon({ generation: undefined })]
  assert.equal(evalStageFor(evaluating(5, 2), log)?.name, 'train')
})

test('"Training" is claimed from the manifest role, never from the stage name', () => {
  // THE claim boundary. `eval_log_plan` refuses to infer training from a slug for kill authority; a
  // status surface must not quietly apply a looser rule to the same string.
  const named = evalStages([beacon({ role: 'work' })]).get(5)
  assert.equal(evalStageLabel(named), 'Stage train · 2 of 3')
  const declared = evalStages([beacon({ role: 'training' })]).get(5)
  assert.equal(evalStageLabel(declared), 'Training (train) · 2 of 3')
})

test('a single-command eval reads as training without a redundant name', () => {
  const record = evalStages([beacon({ name: 'eval', index: 0, total: 1, role: 'training' })]).get(5)
  assert.equal(evalStageLabel(record), 'Training')
  assert.equal(evalStageShortLabel(record), 'training')
})

test('an incoherent step count is dropped rather than rendered', () => {
  // "step 4 of 3" reads as a bug in the run rather than in the strip.
  const record = evalStages([beacon({ index: 5, total: 3 })]).get(5)
  assert.equal(evalStageLabel(record), 'Stage train')
  assert.equal(evalStageShortLabel(record), 'train')
})

test('the activity view names the step, and falls back to the old label without one', () => {
  const node = evaluating()
  const withLog = nodeActivityView(node, live, [beacon()])
  assert.equal(withLog.status, NODE_ACTIVITY.EVALUATING)
  assert.equal(withLog.label, 'Stage train · 2 of 3')
  assert.equal(withLog.shortLabel, 'train 2/3')
  // No cursor (an older engine, or a windowed log whose `started` scrolled out) must be exactly the
  // behaviour that shipped before it existed.
  const without = nodeActivityView(node, live)
  assert.equal(without.label, 'Training / evaluating')
  assert.equal(without.stage, null)
})

test('a stopped or paused run never reports a live stage', () => {
  // A durable cursor outlives the process that opened it; "training" on a dead engine is the lie the
  // run-context tones exist to prevent, and the stage refinement must not reintroduce it.
  const node = evaluating()
  const log = [beacon()]
  assert.equal(nodeActivityView(node, { engine_running: false }, log).tone, 'interrupted')
  assert.equal(nodeActivityView(node, { engine_running: true, paused: true }, log).tone, 'paused')
  assert.equal(nodeActivityView(node, { engine_running: null }, log).tone, 'historical')
})

test('livePhase can be scoped so a build fan-out is never reported about an evaluation', () => {
  const log = [
    { type: 'phase_progress', ts: 1, data: { stage: 'build', phase: 'implement', status: 'started', node_id: 9 } },
    beacon({ ts: 2 }),
  ]
  assert.equal(phaseLabel(livePhase(live, log)), 'Stage train · 2 of 3 — experiment #5…')
  assert.equal(phaseLabel(livePhase(live, log, 'build')), 'Writing code for experiment #9…')
})

test('the card class carries the lane, so queued and evaluating are not one wash', () => {
  // Both are lifecycle `pending`, which is why the body colour alone could never separate them.
  const state = { engine_running: true, best_node_id: null,
    nodes: { 5: evaluating(5), 6: { id: 6, status: 'pending', attempt: 0,
      activity: { schema: 1, status: 'queued', generation: 0 } } } }
  const evalCls = nodeClass(state.nodes[5], state, new Set([5]))
  const queuedCls = nodeClass(state.nodes[6], state, new Set([5]))
  assert.ok(evalCls.includes('a-evaluating') && evalCls.includes('working'))
  assert.ok(queuedCls.includes('a-queued'))
  // A queued node is deliberately NOT `.working`: nothing is running for it, and pulsing it amber is
  // the "the box looks busy" reading `narration.js::pendingWork` was written about.
  assert.ok(!queuedCls.split(' ').includes('working'))
  // The lifecycle class stays, because several surfaces still key on it.
  assert.ok(evalCls.includes('s-pending') && queuedCls.includes('s-pending'))
})

test('a collapsed group counts lanes instead of calling three training nodes "pending"', () => {
  const nodes = { 5: evaluating(5), 6: { id: 6, status: 'pending', attempt: 0,
    activity: { schema: 1, status: 'queued', generation: 0 } },
    7: { id: 7, status: 'evaluated', attempt: 0, metric: 1, feasible: true,
      activity: { schema: 1, status: 'evaluated', generation: 0 } } }
  const state = { engine_running: true, nodes }
  const agg = groupAggregate([5, 6, 7], nodes, 'max', state)
  assert.deepEqual(agg.activity,
    { building: 0, evaluating: 1, queued: 1, unplacedPending: 0 })
  // The lifecycle census is untouched: it is what several other callers sum.
  assert.equal(agg.status.pending, 2)
  assert.equal(agg.status.evaluated, 1)
})

test('the unplaced-pending dot is counted per member, not by subtracting mismatched censuses', () => {
  // The two tallies count DIFFERENT populations: a synthetic withBuilding member is `building` in
  // the lanes and ABSENT from `status.pending`, so the old JSX subtraction
  // max(0, pending - building - evaluating - queued) over-deducted one per synthetic member and
  // the floor silently ate a genuinely-unplaced pending node's ○ dot. One synthetic building
  // member + one legacy pending member (no activity evidence at all) must read ◐1 ○1 — not ◐1 ○0.
  const nodes = {
    7: { id: 7, status: 'building', attempt: 0 },              // withBuilding splice: not in pending
    5: { id: 5, status: 'pending', attempt: 0 },               // legacy: no activity row, no lane
  }
  const agg = groupAggregate([5, 7], nodes, 'max', { engine_running: true, nodes })
  assert.equal(agg.activity.building, 1)
  assert.equal(agg.status.pending, 1)
  assert.equal(agg.activity.unplacedPending, 1,
    'the legacy pending member must keep its dot beside the synthetic building one')
  // …and a pending member a lane DOES place is not unplaced.
  const placed = groupAggregate([5], {
    5: { id: 5, status: 'pending', attempt: 0,
      activity: { schema: 1, status: 'evaluating', generation: 0 } },
  }, 'max', { engine_running: true, nodes: {} })
  assert.deepEqual(placed.activity,
    { building: 0, evaluating: 1, queued: 0, unplacedPending: 0 })
})


test('the a-* activity rail does not outlive the run', () => {
  // The rail is styled "a process of ours is running", and the generation-scoped activity row it
  // keys on survives an engine crash — so a dead/paused/stopped run kept the amber rail while the
  // same card's chip (nodeActivityView, which consults runCanWork) said "interrupted". One card,
  // two opposite claims. The lifecycle wash stays either way.
  const node = evaluating(5)
  for (const deadState of [
    { engine_running: false, best_node_id: null, nodes: { 5: node } },
    { engine_running: true, paused: true, best_node_id: null, nodes: { 5: node } },
    { engine_running: true, stop_requested: true, best_node_id: null, nodes: { 5: node } },
    { engine_running: null, best_node_id: null, nodes: { 5: node } },
  ]) {
    const cls = nodeClass(node, deadState, new Set())
    assert.ok(!cls.includes('a-evaluating'), JSON.stringify([deadState, cls]))
    assert.ok(cls.includes('s-pending'), cls)
  }
  const liveCls = nodeClass(node, { engine_running: true, best_node_id: null,
    nodes: { 5: node } }, new Set())
  assert.ok(liveCls.includes('a-evaluating'), 'a live run keeps the rail')
})
