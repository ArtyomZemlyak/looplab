import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ALL_RUNS, UNASSIGNED_RUNS, effectiveRunStatus, filterRuns, metricComparable,
  scopeRuns, sortRuns,
} from '../src/runIndex.js'

const projects = [
  { id: 'p', name: 'Parent', parent_id: null },
  { id: 'c', name: 'Child', parent_id: 'p' },
]
const runs = [
  { run_id: 'a', label: 'Alpha', task_id: 'min-task', direction: 'min', project_id: 'p', best_metric: 10, mtime: 1, engine_running: true },
  { run_id: 'b', label: 'Beta', task_id: 'min-task', direction: 'min', project_id: 'c', best_metric: 0, mtime: 2, finished: true },
  { run_id: 'c', label: 'Gamma', task_id: 'max-task', direction: 'max', project_id: null, best_metric: 0.9, mtime: 3, engine_running: false },
  { run_id: 'd', label: 'Delta', task_id: 'max-task', direction: 'max', project_id: null, best_metric: null, mtime: 4, engine_running: true },
]

test('nested project scope uses authoritative run.project_id', () => {
  assert.deepEqual(scopeRuns(runs, 'p', projects).map(run => run.run_id), ['a', 'b'])
  assert.deepEqual(scopeRuns(runs, UNASSIGNED_RUNS, projects).map(run => run.run_id), ['c', 'd'])
})

test('List and Map can share one combined filter result', () => {
  const result = filterRuns(runs, {
    project: ALL_RUNS, projects, query: 'ga', task: 'max-task', status: 'stalled',
  })
  assert.deepEqual(result.map(run => run.run_id), ['c'])
  assert.equal(effectiveRunStatus(runs[2]), 'stalled')
})

test('intentional pause and approval are not mislabeled as stalled', () => {
  const paused = { finished: false, paused: true, phase: 'paused', engine_running: false }
  const approval = { finished: false, phase: 'approval', engine_running: false }
  assert.equal(effectiveRunStatus(paused), 'paused')
  assert.equal(effectiveRunStatus(approval), 'approval')
  assert.equal(effectiveRunStatus({ finished: false, phase: 'search', engine_running: false }), 'stalled')
})

test('metric ordering is objective-aware and missing values stay last', () => {
  const minRuns = runs.filter(run => run.task_id === 'min-task')
  assert.deepEqual(sortRuns(minRuns, 'metric', 'asc').map(run => run.run_id), ['b', 'a'])
  assert.deepEqual(sortRuns(minRuns, 'metric', 'desc').map(run => run.run_id), ['a', 'b'])

  const maxRuns = runs.filter(run => run.task_id === 'max-task')
  assert.deepEqual(sortRuns(maxRuns, 'metric', 'asc').map(run => run.run_id), ['c', 'd'])
  assert.deepEqual(sortRuns(maxRuns, 'metric', 'desc').map(run => run.run_id), ['c', 'd'])
  assert.equal(metricComparable(maxRuns), true)
  assert.equal(metricComparable(runs), false)
  assert.deepEqual(sortRuns(runs, 'metric', 'asc'), runs) // incompatible tasks are not ranked
})
