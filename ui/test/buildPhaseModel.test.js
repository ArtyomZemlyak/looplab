import test from 'node:test'
import assert from 'node:assert/strict'

import { livePhase, openPhases, phaseLabel } from '../src/buildingModel.js'

// The property under test throughout: a build and a resume must never be a blank panel. Every
// assertion below is about a way that could silently come back.

const beacon = (ts, data) => ({ type: 'phase_progress', ts, data })
const liveRun = { nodes: {}, engine_running: true }

test('a started beacon with no finished is the open phase; its finished closes it', () => {
  const log = [beacon(100, { stage: 'build', phase: 'implement', status: 'started', node_id: 7 })]
  assert.equal(openPhases(log).length, 1)
  assert.equal(openPhases(log)[0].nodeId, 7)
  log.push(beacon(160, { stage: 'build', phase: 'implement', status: 'finished', node_id: 7, seconds: 60 }))
  assert.deepEqual(openPhases(log), [])
})

test('the PROPOSAL is nameable, which is the whole point — it runs before node_building exists', () => {
  // This is the operator's reported failure. Until the beacon there was no marker, no node and no
  // state field during the Researcher call, so every surface fell through to "Planning…".
  const log = [beacon(100, { stage: 'build', phase: 'propose', status: 'started', count: 4 })]
  assert.equal(phaseLabel(livePhase(liveRun, log)), 'Proposing 4 experiments…')
})

test('a batch proposal carries a count and NO node id — it names no node it cannot name', () => {
  const log = [beacon(100, { stage: 'build', phase: 'propose', status: 'started', count: 3 })]
  assert.equal(openPhases(log)[0].nodeId, null)
  assert.equal(openPhases(log)[0].count, 3)
  // A single-idea proposal names its prospective node instead.
  const one = [beacon(100, { stage: 'build', phase: 'propose', status: 'started', node_id: 9, prospective: true })]
  assert.equal(phaseLabel(livePhase(liveRun, one)), 'Proposing experiment #9…')
})

test('phases NEST, and the innermost open one is what is happening right now', () => {
  // `novelty` runs inside `propose`; while both are open, "checking for a repeat" is the true answer
  // and "proposing" is merely still true. Naming the outer one would under-report progress.
  const log = [
    beacon(100, { stage: 'build', phase: 'propose', status: 'started', node_id: 3, prospective: true }),
    beacon(140, { stage: 'build', phase: 'novelty', status: 'started', node_id: 3, prospective: true }),
  ]
  assert.equal(livePhase(liveRun, log).phase, 'novelty')
  assert.equal(phaseLabel(livePhase(liveRun, log)), 'Checking experiment #3 is not a repeat…')
})

test('parallel builds keep separate open phases — the same phase for different nodes is not one key', () => {
  const log = [
    beacon(100, { stage: 'build', phase: 'implement', status: 'started', node_id: 1 }),
    beacon(101, { stage: 'build', phase: 'implement', status: 'started', node_id: 2 }),
    beacon(150, { stage: 'build', phase: 'implement', status: 'finished', node_id: 1 }),
  ]
  // Node 1 finishing must not close node 2's identically-named phase.
  assert.deepEqual(openPhases(log).map(r => r.nodeId), [2])
})

test('a windowed log that begins mid-build drops the orphan finished instead of inventing a phase', () => {
  // The Dock holds a bounded page of events, so the log can genuinely start after a `started` row.
  // Counting an unmatched `finished` as open would put a phase on screen that already ended.
  const log = [beacon(150, { stage: 'build', phase: 'implement', status: 'finished', node_id: 7 })]
  assert.deepEqual(openPhases(log), [])
  assert.equal(livePhase(liveRun, log), null)
})

test('a dead or finished run shows no phase, however many beacons are left open', () => {
  // An engine killed mid-build leaves every beacon open forever. This is the same guard `withBuilding`
  // applies for the same reason, and `finished` alone does not cover it: a STALLED run is not finished.
  const log = [beacon(100, { stage: 'build', phase: 'implement', status: 'started', node_id: 7 })]
  assert.equal(livePhase({ nodes: {}, engine_running: false }, log), null)
  assert.equal(livePhase({ nodes: {}, finished: true }, log), null)
  assert.equal(livePhase(null, log), null)
})

test('a resume names its two phases — the prologue runs before anything else can move', () => {
  const readLog = [beacon(100, { stage: 'resume', phase: 'read_log', status: 'started' })]
  assert.equal(phaseLabel(livePhase(liveRun, readLog)), 'Resuming — reading the run log…')
  const reconcile = [
    ...readLog,
    beacon(120, { stage: 'resume', phase: 'read_log', status: 'finished', events: 41203 }),
    beacon(121, { stage: 'resume', phase: 'reconcile', status: 'started' }),
  ]
  assert.equal(phaseLabel(livePhase(liveRun, reconcile)), 'Resuming — restoring run state…')
})

test('a speculative build says so — it is work that may be thrown away', () => {
  const log = [beacon(100, {
    stage: 'build', phase: 'propose', status: 'started', node_id: 5, speculative: true,
  })]
  assert.equal(phaseLabel(livePhase(liveRun, log)), 'Proposing experiment #5… (speculative)')
})

test('an unlabelled phase renders null, never "undefined", so the caller keeps its own text', () => {
  // A phase the engine adds before this table does must degrade to the previous behaviour, not to a
  // status strip reading "build undefined…".
  assert.equal(phaseLabel({ stage: 'build', phase: 'a_future_step', nodeId: 1 }), null)
  assert.equal(phaseLabel(null), null)
})

test('malformed and hostile beacon rows are ignored rather than crashing the strip', () => {
  const log = [
    { type: 'phase_progress' },
    { type: 'phase_progress', data: null },
    { type: 'phase_progress', data: { stage: 'build' } },
    { type: 'phase_progress', data: { stage: '__proto__', phase: 'constructor', status: 'started' } },
    { type: 'node_created', data: { node_id: 1 } },
    beacon(100, { stage: 'build', phase: 'implement', status: 'started', node_id: true }),
  ]
  const open = openPhases(log)
  // The prototype-keyed row is a real, harmless entry in a Map (not an object), and the boolean
  // node_id settles to null rather than to 1 — `Number(true)` is 1, which would name node #1.
  assert.equal(open.every(r => r.nodeId === null), true)
  assert.equal(openPhases(undefined).length, 0)
})
