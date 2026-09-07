// The cross-run TRAJECTORY overlay (doc 52 row 26): what one comparable group draws, what it counts
// instead of drawing, and the sentence that says so — then the chart itself rendered from the
// model's output through the mount harness, so the step shape a running best must have is asserted
// on real markup rather than on a source pin.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  crossRunGroups, runTrajectory, trajectoryClaim, trajectoryOverlay,
} from '../src/crossRunRank.js'
import { mountHarness } from './_mount.js'

const series = (points, evaluated, complete = true) => ({ version: 1, points, evaluated, complete })
const run = (run_id, best_metric, extra = {}) => ({
  run_id, task_id: 'repo_task', direction: 'max', best_metric, best_confirmed: null, nodes: 4,
  finished: true, phase: 'finished', label: run_id, ...extra,
})

const groupOf = runs => {
  const index = crossRunGroups(runs)
  assert.equal(index.groups.length, 1, 'precondition: one comparable group')
  return index.groups[0]
}

test('runTrajectory admits the server shape and refuses every malformed series as "no series"', () => {
  const good = series([[0, 0.4, 1], [2, 0.7, 3]], 3)
  assert.deepEqual(runTrajectory({ trajectory: good }),
    { points: good.points, evaluated: 3, complete: true })
  assert.equal(runTrajectory({ trajectory: series([[0, 0.4, 1]], 1, false) }).complete, false)
  for (const bad of [
    undefined, null, {}, series([], 0),
    series([[0, 0.4, 1], [0, 0.7, 2]], 2),        // experiment indices must strictly increase
    series([[0, 0.4, 1], [1, 'x', 2]], 2),        // a value must be a finite number
    series([[0, 0.4, 1], [1, 0.7]], 2),           // a point names its node
    series([[0, 0.4, 1], [3, 0.7, 2]], 3),        // `evaluated` covers the last index
    series([[-1, 0.4, 1]], 1),
  ]) assert.equal(runTrajectory({ trajectory: bad }), null, JSON.stringify(bad))
  assert.equal(runTrajectory(run('legacy', 0.5)), null, 'a row without the field is "no series"')
})

test('trajectoryOverlay draws ranked rows with a series in rank order and counts every exclusion', () => {
  const group = groupOf([
    run('runner-up', 0.6, { trajectory: series([[0, 0.2, 1], [3, 0.6, 4]], 4) }),
    run('leader', 0.8, { trajectory: series([[0, 0.5, 1], [1, 0.8, 2]], 2, false) }),
    run('no-series', 0.7),
    run('prefix', 0.9, {
      trajectory: series([[0, 0.9, 1]], 1),
      source_integrity: { complete: false, good_records: 20, corrupt_line: 21, dropped_lines: 1603 },
    }),
  ])
  const overlay = trajectoryOverlay(group)
  assert.deepEqual(overlay.runs.map(entry => entry.label), ['#1 leader', '#3 runner-up'],
    'rank order, and the legend carries the GROUP rank: the undrawn 0.7 row is #2, so the second line is #3')
  assert.deepEqual(overlay.runs[0], {
    run_id: 'leader', label: '#1 leader', points: [[0, 0.5, 1], [1, 0.8, 2]], evaluated: 2,
    complete: false,
  })
  assert.equal(overlay.drawn, 2)
  assert.equal(overlay.noSeries, 1, 'the row without a series is counted, not drawn flat')
  assert.equal(overlay.prefix, 1, 'the prefix-folded run is not drawn, as it holds no rank')
  assert.equal(overlay.capped, 1, 'the subsampled row is counted so the sentence can name it')
  assert.equal(overlay.beyondLimit, 0)
  assert.equal(trajectoryClaim(group, overlay),
    "Running best per evaluated experiment for 2 of 4 runs, on this group's own axis — one task, "
    + 'one direction, one evaluation, nothing rescaled; a line holds its value until the experiment '
    + 'that beat it. 1 carries no series (no feasible measured node, or a row served before the '
    + 'series existed); 1 prefix-folded run is not drawn, for the reason it holds no rank; '
    + '1 drawn coarser: more improvements than the row carries.')
})

test('the overlay stops at the eight lines the chart can tell apart and says how many are beyond', () => {
  const group = groupOf(Array.from({ length: 11 }, (_, i) => run(`run-${String(i).padStart(2, '0')}`,
    1 - i / 20, { trajectory: series([[0, 1 - i / 20, 1]], 1) })))
  const overlay = trajectoryOverlay(group)
  assert.equal(overlay.drawn, 8)
  assert.equal(overlay.beyondLimit, 3)
  assert.deepEqual(overlay.runs.map(entry => entry.run_id), Array.from({ length: 8 }, (_, i) => `run-0${i}`))
  assert.match(trajectoryClaim(group, overlay), / 3 beyond the 8 lines the chart can tell apart, in rank order\.$/)
  assert.equal(trajectoryOverlay(group, { limit: 2 }).beyondLimit, 9)
})

test('a group with nothing to draw says so instead of rendering an empty axis', () => {
  const group = groupOf([run('a', 0.5), run('b', 0.4)])
  const overlay = trajectoryOverlay(group)
  assert.equal(overlay.drawn, 0)
  assert.deepEqual(overlay.runs, [])
  assert.equal(trajectoryClaim(group, overlay),
    'No trajectory to draw for this group. 2 carry no series (no feasible measured node, or a row '
    + 'served before the series existed).')
})

test('MultiTrajectory renders each run as a STEP path from its change points, in rank order', async () => {
  const harness = await mountHarness()
  try {
    const { MultiTrajectory } = await harness.load('/src/charts.jsx')
    const group = groupOf([
      run('runner-up', 0.6, { trajectory: series([[0, 0.2, 1], [3, 0.6, 4]], 4) }),
      run('leader', 0.8, { trajectory: series([[0, 0.5, 1], [1, 0.8, 2], [3, 0.8, 2]], 4, false) }),
    ])
    const overlay = trajectoryOverlay(group)
    const markup = harness.render(MultiTrajectory, { runs: overlay.runs, title: 'Running best · repo_task · higher is better' })
    assert.match(markup, /Running best · repo_task · higher is better/)
    // x: the last experiment of the longest run sits at the right edge (34 + 716 = 750); a step is
    // one horizontal then one vertical segment per change point, never a slope.
    const paths = [...markup.matchAll(/<path d="([^"]+)"/g)].map(match => match[1])
    assert.equal(paths.length, 4, 'two runs, each drawn as an outline path plus a coloured path')
    assert.equal(paths[0], paths[1])
    assert.match(paths[0], /^M34\.0 [\d.]+ H272\.7 V[\d.]+ H750\.0 V[\d.]+$/, 'the leader: three change points')
    assert.match(paths[2], /^M34\.0 [\d.]+ H750\.0 V[\d.]+$/, 'the runner-up: two change points')
    assert.match(markup, /#1 leader<span title="more improvements than the row carries[^"]*"> \(coarser\)<\/span>/)
    assert.match(markup, /#2 runner-up<\/span>/)
    assert.match(markup, /aria-label="Export Running best · repo_task · higher is better data as CSV"/,
      'every exact point is exportable')
    assert.doesNotMatch(markup, /<polyline/, 'a running best is a step, not a polyline')
  } finally {
    await harness.close()
  }
})
