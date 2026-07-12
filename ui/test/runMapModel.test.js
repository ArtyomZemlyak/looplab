import test from 'node:test'
import assert from 'node:assert/strict'

import {
  MAP_COLLAPSE_THRESHOLD, UNASSIGNED_CLUSTER, defaultCollapsedClusters, packRunGrid,
} from '../src/runMapModel.js'

const makeRuns = (count, project_id = null) => Array.from({ length: count }, (_, index) => ({
  run_id: `run-${index}`, project_id,
}))

test('55 unassigned runs are packed into rows instead of an 11.7kpx line', () => {
  const packed = packRunGrid(makeRuns(55))
  assert.equal(packed.columns, 6)
  assert.equal(packed.rows, 10)
  const xs = [...packed.positions.values()].map(position => position.x)
  assert.ok(Math.max(...xs) <= 5 * 214)
})

test('19 filtered runs remain expanded and represented exactly once', () => {
  const runs = makeRuns(19)
  const packed = packRunGrid(runs)
  assert.equal(packed.positions.size, 19)
  assert.equal(defaultCollapsedClusters([], runs, () => new Set()).has(UNASSIGNED_CLUSTER), false)
})

test('large unassigned/project groups collapse by default', () => {
  const project = { id: 'p', name: 'Project' }
  const projectRuns = makeRuns(MAP_COLLAPSE_THRESHOLD + 1, 'p')
  const unassigned = makeRuns(MAP_COLLAPSE_THRESHOLD + 1)
  const collapsed = defaultCollapsedClusters([project], [...projectRuns, ...unassigned], () => new Set(['p']))
  assert.equal(collapsed.has('p'), true)
  assert.equal(collapsed.has(UNASSIGNED_CLUSTER), true)
})
