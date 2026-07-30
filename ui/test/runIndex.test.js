import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ALL_RUNS, UNASSIGNED_RUNS, dagEmptyPresentation, effectiveRunStatus, filterRuns, finalizationIncomplete,
  indexProjects, lifecyclePhaseLabel, metricComparable, projectAncestorCollapsed, projectDepth,
  runLifecycle, scopeRuns, sortRuns, terminalReady,
} from '../src/runIndex.js'

const projects = [
  { id: 'p', name: 'Parent', parent_id: null },
  { id: 'c', name: 'Child', parent_id: 'p' },
]
const runs = [
  { run_id: 'a', label: 'Alpha', task_id: 'min-task', direction: 'min', project_id: 'p', best_metric: 10, mtime: 1, engine_running: true },
  { run_id: 'b', label: 'Beta', task_id: 'min-task', direction: 'min', project_id: 'c', best_metric: 0, mtime: 2, finished: true, engine_running: false },
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

test('finalizing outranks paused/stalled and is filterable', () => {
  const finalizing = { run_id: 'f', finished: false, paused: true, phase: 'finalizing', engine_running: false }
  assert.equal(effectiveRunStatus(finalizing), 'finalizing')
  assert.equal(effectiveRunStatus({ finished: false, stop_requested: 'finalized', engine_running: false }), 'finalizing')
  assert.deepEqual(filterRuns([finalizing, ...runs], {
    project: ALL_RUNS, projects, status: 'finalizing',
  }).map(run => run.run_id), ['f'])
})

test('error-finished finalize remains recovery state across every UI surface', () => {
  const run = {
    run_id: 'error-finalize', finished: true, phase: 'finalizing', stop_requested: true,
    stop_reason: 'error', paused: true, engine_running: false,
  }
  assert.equal(finalizationIncomplete(run), true)
  assert.equal(terminalReady(run), false)
  assert.equal(runLifecycle(run).mode, 'finalization-stalled')
  assert.equal(effectiveRunStatus(run), 'finalizing')
  assert.equal(finalizationIncomplete({ ...run, phase: 'finished' }), true,
    'stop_requested + error remains authoritative even if a cached phase lags')
})

test('finished event does not become terminal-ready until the engine exits', () => {
  const writing = {
    finished: true, phase: 'finished', stop_requested: true,
    stop_reason: 'finalized', engine_running: true,
  }
  assert.equal(finalizationIncomplete(writing), true)
  assert.equal(terminalReady(writing), false)
  assert.equal(runLifecycle(writing).mode, 'finalizing')
  assert.equal(effectiveRunStatus(writing), 'finalizing')

  const complete = { ...writing, engine_running: false }
  assert.equal(finalizationIncomplete(complete), false)
  assert.equal(terminalReady(complete), true)
  assert.equal(runLifecycle(complete).mode, 'finished')
  assert.equal(effectiveRunStatus(complete), 'finished')
})

test('natural finish with a live process is terminal write-out, not Resume/Replay-ready', () => {
  const writing = { finished: true, phase: 'finished', engine_running: true }
  assert.equal(finalizationIncomplete(writing), false)
  assert.equal(terminalReady(writing), false)
  assert.equal(runLifecycle(writing).mode, 'finishing')
  assert.equal(effectiveRunStatus(writing), 'finalizing')
  assert.equal(lifecyclePhaseLabel(writing), 'finishing')
})

test('unknown engine ownership never becomes terminal-ready', () => {
  const active = { finished: false, phase: 'search', engine_running: null }
  assert.equal(runLifecycle(active).mode, 'unknown')
  assert.equal(effectiveRunStatus(active), 'unknown')
  assert.equal(lifecyclePhaseLabel(active), 'engine ownership unknown')

  const natural = { finished: true, phase: 'finished', engine_running: null }
  assert.equal(terminalReady(natural), false)
  assert.equal(runLifecycle(natural).mode, 'finishing')

  const finalized = {
    finished: true, phase: 'finished', stop_requested: true,
    stop_reason: 'finalized', engine_running: null,
  }
  assert.equal(finalizationIncomplete(finalized), true)
  assert.equal(terminalReady(finalized), false)
  assert.equal(runLifecycle(finalized).mode, 'finalizing')
})

test('canonical header phase never falls back to a stale folded finished label during finalization', () => {
  assert.equal(lifecyclePhaseLabel({
    finished: true, phase: 'finished', stop_requested: true, engine_running: true,
  }), 'finalizing')
  assert.equal(lifecyclePhaseLabel({
    finished: true, phase: 'finished', stop_requested: true, stop_reason: 'error', engine_running: false,
  }), 'finalization stalled')
})

test('zero-node DAG presentation is lifecycle-aware and read-only contexts win', () => {
  const present = (live, context = {}) => dagEmptyPresentation({
    displayed: live, live, resourceStatus: 'ready', connected: true, ...context,
  })
  const ids = value => value.actions.map(item => item.id)

  assert.equal(present({ nodes: { 0: { id: 0 } }, engine_running: true }), null)
  assert.equal(present({
    nodes: { 0: { id: 0, tombstoned: true } }, finished: true, engine_running: false,
  }).kind, 'finished', 'a tombstone-only projection needs an empty state instead of a blank canvas')
  assert.equal(present({
    nodes: { 0: { id: 0 } }, aborted_nodes: [0], phase: 'search', engine_running: false,
  }).kind, 'stalled', 'run-level aborts are excluded by the same projection as the DAG')
  assert.equal(present({
    nodes: { 0: { id: 0 }, 1: { id: 1, tombstoned: true } }, engine_running: true,
  }), null, 'one active node still suppresses the empty state')
  assert.equal(dagEmptyPresentation({ displayed: {}, resourceStatus: 'error' }), null,
    'resource failures are handled by RunView and must never masquerade as successful empty')
  assert.equal(dagEmptyPresentation({
    displayed: { nodes: {}, phase: 'setup', engine_running: true }, resourceStatus: 'ready',
  }).kind, 'preparing', 'displayed state remains authoritative when no separate live state is supplied')

  const history = present({ nodes: {}, engine_running: false }, { historyActive: true, sequence: 7 })
  assert.equal(history.kind, 'history')
  assert.deepEqual(ids(history), ['return-live'])
  assert.match(history.title, /seq 7/)

  const review = present({ nodes: {}, engine_running: false }, { reviewMode: true })
  assert.equal(review.kind, 'review')
  assert.deepEqual(ids(review), [], 'a live-looking review must never expose owner mutations')

  const cases = [
    [{ nodes: {}, phase: 'finalizing', engine_running: false }, 'finalization-stalled', ['finalize', 'events']],
    [{ nodes: {}, phase: 'finalizing', engine_running: true }, 'finalizing', []],
    [{ nodes: {}, finished: true, engine_running: true }, 'finishing', []],
    [{ nodes: {}, phase: 'spec_approval', paused: true, spec_approval_requested: true,
      engine_running: false }, 'approval', ['assistant']],
    [{ nodes: {}, phase: 'approval', engine_running: false }, 'approval-incomplete', ['events']],
    [{ nodes: {}, phase: 'approval', best_node_id: 4, engine_running: false },
      'approval-incomplete', ['events']],
    [{ nodes: {}, paused: true, engine_running: false }, 'paused', ['resume', 'finalize']],
    [{ nodes: {}, phase: 'search', engine_running: null }, 'unknown', ['events']],
    [{ nodes: {}, phase: 'search', engine_running: false }, 'stalled', ['resume', 'finalize', 'events']],
    [{ nodes: {}, finished: true, engine_running: false }, 'finished', ['report', 'resume']],
    [{ nodes: {}, phase: 'setup', engine_running: true }, 'preparing', ['events']],
  ]
  cases.forEach(([live, kind, actions]) => {
    const value = present(live)
    assert.equal(value.kind, kind)
    assert.deepEqual(ids(value), actions)
  })
})

test('zero-node fallback distinguishes a retained disconnected state', () => {
  const value = dagEmptyPresentation({
    displayed: { nodes: {}, engine_running: null }, live: { nodes: {}, engine_running: null },
    resourceStatus: 'ready', connected: false,
  })
  assert.equal(value.kind, 'unknown')
  assert.equal(value.liveRegion, 'assertive')
  assert.deepEqual(value.actions.map(item => item.id), ['events', 'retry-connection'])
  assert.match(value.body, /connection is interrupted/)
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

test('project walks survive a cyclic parent chain instead of freezing the render', () => {
  // Project parentage is stored assignment data, so a bad edit or an import can produce a -> b -> a.
  // The List view's subtree walk has always guarded that; the Map view's copies of depthOf /
  // ancestorCollapsed did not, and the same rows hung it forever.
  const cyclic = [{ id: 'a', name: 'A', parent_id: 'b' }, { id: 'b', name: 'B', parent_id: 'a' }]
  const { byId, subtree } = indexProjects(cyclic)

  assert.deepEqual([...subtree('a')].sort(), ['a', 'b'])
  assert.equal(projectDepth(byId, 'a'), 1)          // bounded by the visited set, not by the cycle
  assert.equal(projectDepth(byId, 'b'), 1)
  assert.equal(projectAncestorCollapsed(byId, 'a', new Set(['b'])), true)
  assert.equal(projectAncestorCollapsed(byId, 'a', new Set(['zzz'])), false)

  // A well-formed chain is unaffected: depth still counts real ancestors.
  const tree = [{ id: 'root', name: 'R', parent_id: null }, { id: 'mid', name: 'M', parent_id: 'root' },
                { id: 'leaf', name: 'L', parent_id: 'mid' }]
  const chain = indexProjects(tree)
  assert.equal(projectDepth(chain.byId, 'leaf'), 2)
  assert.equal(projectAncestorCollapsed(chain.byId, 'leaf', new Set(['root'])), true)
  assert.equal(projectAncestorCollapsed(chain.byId, 'leaf', new Set()), false)
})

test('a project with no name does not crash the index the map view builds from', () => {
  // The Map view sorted with a bare `a.name.localeCompare(b.name)`, so a null-named project threw
  // "Cannot read properties of null" and took the whole view down while the List view — which
  // coerces via String(name || '') — kept rendering. Order matters: the throw needs the null name to
  // reach the comparator's LEFT side, which it does for every position but first.
  const { byParent } = indexProjects([{ id: 'y', name: 'Y', parent_id: null },
                                      { id: 'x', name: null, parent_id: null },
                                      { id: 'z', name: 'Z', parent_id: null }])
  assert.deepEqual(byParent[null].map(project => project.id), ['x', 'y', 'z'])
})

test('cross-run ranking is direction-aware and refuses incomparable sets', () => {
  // The registry panel sorted by raw metric DESCENDING, so on a `direction: 'min'` task the BEST run
  // sorted LAST, and runs of different tasks/objectives/metric units were ranked against each other
  // on one unitless axis. `sortRuns(..., 'metric', 'asc')` is best-first and already encodes both
  // rules; `metricComparable` is the gate.
  const minRuns = [
    { run_id: 'worse', task_id: 't', direction: 'min', best_metric: 9 },
    { run_id: 'best', task_id: 't', direction: 'min', best_metric: 1 },
  ]
  assert.equal(metricComparable(minRuns), true)
  assert.deepEqual(sortRuns(minRuns, 'metric', 'asc').map(r => r.run_id), ['best', 'worse'])
  // A raw descending sort would have produced the opposite for this exact input.
  assert.notDeepEqual(
    [...minRuns].sort((a, b) => (b.best_metric ?? -Infinity) - (a.best_metric ?? -Infinity))
      .map(r => r.run_id),
    ['best', 'worse'])

  const maxRuns = minRuns.map(r => ({ ...r, direction: 'max' }))
  assert.deepEqual(sortRuns(maxRuns, 'metric', 'asc').map(r => r.run_id), ['worse', 'best'])

  // Mixed tasks or mixed objectives are not rankable at all — the caller must present them unranked.
  assert.equal(metricComparable([...minRuns, { run_id: 'x', task_id: 'other', direction: 'min', best_metric: 0 }]), false)
  assert.equal(metricComparable([minRuns[0], { ...minRuns[1], direction: 'max' }]), false)
})
