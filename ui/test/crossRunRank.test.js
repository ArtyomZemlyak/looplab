// The cross-run metric overlay's pure model. The properties under test are the ones that decide
// whether a ranking on this screen is a FACT or a plausible-looking number: that two objectives never
// share an ordered column, that identical values never receive different ranks, that a run folded from
// a partial log never holds a rank, and that a run which cannot be ranked stays visible and counted
// instead of vanishing into a smaller, tidier table.
//
// The fixtures are the REAL corpus, measured 2026-08-14 by folding all 45 run directories under
// `runs/` through `events/replay.py::fold` — the same fold `/api/runs` serves. Values, group sizes,
// ties and the one incomplete log below are the ones actually on this box.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  crossRunGroups, groupClaim, rankCoverage, runMetric,
} from '../src/crossRunRank.js'

const run = (run_id, task_id, direction, best_metric, extra = {}) => ({
  run_id, task_id, direction, best_metric, best_confirmed: null, nodes: 4, finished: true,
  phase: 'finished', label: run_id, ...extra,
})

// The measured corpus, metric-bearing rows only (36 of 45; the other 9 carry no metric and three of
// them are appended here so the model has to account for them rather than drop them).
const CORPUS = [
  // toy_quadratic/min — 9 runs, 5 of them at exactly 0.0.
  run('live-cards-0804', 'toy_quadratic', 'min', 0.0),
  run('live-dr-check-0804', 'toy_quadratic', 'min', 0.0),
  run('live_toy', 'toy_quadratic', 'min', 7.8652931200000005),
  run('livetest-partv', 'toy_quadratic', 'min', 0.0),
  run('lt_quad', 'toy_quadratic', 'min', 5.57026258),
  run('lt_setup', 'toy_quadratic', 'min', 8.51019194),
  run('smoke_e1f', 'toy_quadratic', 'min', 0.0),
  run('smoke_edf', 'toy_quadratic', 'min', 0.0),
  run('spec-live-0804', 'toy_quadratic', 'min', 1.5604954),
  // dataset_task — 4 minimize runs, and `mnist-experiments` MAXIMIZES the same task id.
  run('glm_dataset', 'dataset_task', 'min', 0.10278059368413668),
  run('live_dataset', 'dataset_task', 'min', 0.05165833391750925),
  run('lt_dataset', 'dataset_task', 'min', 0.09397966453422642),
  run('lt_trace2', 'dataset_task', 'min', 0.09398),
  run('mnist-experiments', 'dataset_task', 'max', 0.9),
  // repo_task/max — the only real GPU group, and the leader's log is truncated at line 21.
  run('rubertlite-dense-retrieval', 'repo_task', 'max', 0.8077, {
    source_integrity: {
      complete: false, good_records: 20, corrupt_line: 21, dropped_lines: 1603, unreadable: null,
    },
  }),
  run('rubertlite-dr-unified-v2', 'repo_task', 'max', 0.224975),
  run('rubertlite-dr-unified-v6', 'repo_task', 'max', 0.727991),
  // dataset_example/max — 3 runs, all at exactly 1.0.
  run('smoke-glm-agentic', 'dataset_example', 'max', 1.0),
  run('smoke-phases', 'dataset_example', 'max', 1.0),
  run('verify-mem', 'dataset_example', 'max', 1.0),
  // blob_classification/max — 2 runs, both at exactly 0.925.
  run('b2-validate', 'blob_classification', 'max', 0.925),
  run('livetest-p0', 'blob_classification', 'max', 0.925),
  // Singleton tasks (10 of the corpus's 15; enough to prove they are grouped and not ranked).
  run('live-asha-0804', 'asha_curve_probe', 'max', 0.5),
  run('live-nosignal', 'nosignal', 'max', 0.5),
  run('live-periodic', 'periodic', 'max', 0.5),
  run('rubert-dr-0807', 'rubert_dr_0807', 'max', 0.6),
  run('task-g7', 'task-g7', 'max', 0.5),
  // No metric at all — a live run, a setup-only run, a dependency probe.
  run('rubertlite-dr-unified-v7', 'repo_task', 'max', null, { finished: false, phase: 'search' }),
  run('rubert-dr-0805', 'rubert_dr_0804', 'max', null),
  run('live-deps-0804', 'toy_quadratic', 'min', null),
]

const groupFor = (index, taskId, direction) =>
  index.groups.find(g => g.taskId === taskId && g.direction === direction)

test('the corpus partitions into the groups actually on this box, and the panel can state its coverage', () => {
  const index = crossRunGroups(CORPUS)
  // 5 groups can hold a ranking; the rest are one-run tasks. Measured on the full corpus: 5 groups /
  // 21 runs out of 45. This fixture carries the same 5 groups and 5 of the 15 singleton tasks.
  assert.deepEqual(index.comparable.map(g => [g.taskId, g.direction, g.size]), [
    ['toy_quadratic', 'min', 9],
    ['dataset_task', 'min', 4],
    ['dataset_example', 'max', 3],
    ['repo_task', 'max', 3],
    ['blob_classification', 'max', 2],
  ])
  // `mnist-experiments` shares a task id with four minimize runs and is NOT in their group. Grouping
  // on task_id alone would have ordered a minimize objective inside a maximize column.
  assert.deepEqual(groupFor(index, 'dataset_task', 'max').rows.map(r => r.runId), ['mnist-experiments'])
  assert.equal(groupFor(index, 'dataset_task', 'max').outcome, 'single')

  const coverage = rankCoverage(index)
  assert.equal(coverage.runs, CORPUS.length)
  assert.equal(coverage.comparableRuns, 21)
  assert.equal(coverage.comparableGroups, 5)
  // The three metric-less runs are OUT of scope and counted, never quietly absent.
  assert.equal(coverage.noMetric, 3)
  assert.equal(coverage.outOfScope, CORPUS.length - 21)
  assert.equal(coverage.complete, false)
  assert.equal(coverage.integrityExcluded, 1)
  assert.deepEqual(index.noMetric.map(r => r.run_id).sort(),
    ['live-deps-0804', 'rubert-dr-0805', 'rubertlite-dr-unified-v7'])
})

test('identical values share one rank — the majority case here, and the one a sort would fake', () => {
  const index = crossRunGroups(CORPUS)
  const toy = groupFor(index, 'toy_quadratic', 'min')
  assert.equal(toy.outcome, 'ranked')
  assert.equal(toy.distinctValues, 5)
  // Five runs at 0.0 all hold rank 1, and the next distinct value takes rank 6 — competition ranks,
  // so no gap in the numbers can be read as a margin that does not exist.
  assert.deepEqual(toy.rows.map(r => [r.runId, r.rank]), [
    ['live-cards-0804', 1], ['live-dr-check-0804', 1], ['livetest-partv', 1],
    ['smoke_e1f', 1], ['smoke_edf', 1],
    ['spec-live-0804', 6], ['lt_quad', 7], ['live_toy', 8], ['lt_setup', 9],
  ])
  assert.equal(toy.leaders.length, 5)
  // The render prints "(tie)" off this, so a rank number can never stand alone as a placing.
  assert.deepEqual(toy.rows.map(r => r.tied), [4, 4, 4, 4, 4, 0, 0, 0, 0])
  assert.match(groupClaim(toy).claim, /5 of 9 ranked runs share the lowest recorded value \(0\)/)
  assert.match(groupClaim(toy).claim, /none of them beat the others/)
})

test('a group whose runs all recorded the same number is a TIE, not a ranking', () => {
  const index = crossRunGroups(CORPUS)
  for (const [taskId, n, value] of [['dataset_example', 3, 1], ['blob_classification', 2, 0.925]]) {
    const group = groupFor(index, taskId, 'max')
    assert.equal(group.outcome, 'tied')
    assert.equal(group.distinctValues, 1)
    assert.equal(group.leaders.length, n)
    assert.match(groupClaim(group).claim,
      new RegExp(`All ${n} ranked runs of ${taskId} recorded exactly the same value \\(${value}\\)`))
    assert.match(groupClaim(group).claim, new RegExp(`no winner: it is a ${n}-way tie`))
  }
})

test('a run folded from a partial log holds NO rank, keeps its row, and changes who leads', () => {
  const index = crossRunGroups(CORPUS)
  const repo = groupFor(index, 'repo_task', 'max')
  // 0.8077 is the highest number in this group and it comes from 20 of 1,624 records. Ranking it
  // would crown a run on 1.2 % of its own evidence.
  assert.equal(repo.size, 3)
  assert.equal(repo.ranked, 2)
  assert.equal(repo.integrityExcluded, 1)
  assert.deepEqual(repo.unranked.map(r => [r.runId, r.value, r.rank]),
    [['rubertlite-dense-retrieval', 0.8077, null]])
  assert.deepEqual(repo.rows.map(r => [r.runId, r.rank]),
    [['rubertlite-dr-unified-v6', 1], ['rubertlite-dr-unified-v2', 2]])
  assert.deepEqual(repo.leaders, ['rubertlite-dr-unified-v6'])
  assert.equal(repo.bestValue, 0.727991)
  const claim = groupClaim(repo)
  assert.match(claim.claim, /rubertlite-dr-unified-v6 recorded the highest value \(0\.727991\)/)
  assert.ok(claim.refusals.some(line => /stops being readable.*best of a PREFIX/s.test(line)))
})

test('a group where every run has a partial log ranks nobody and says so', () => {
  const partial = { source_integrity: { complete: false, good_records: 3, dropped_lines: 9 } }
  const index = crossRunGroups([
    run('a', 't', 'max', 0.9, partial), run('b', 't', 'max', 0.1, partial),
  ])
  const group = index.groups[0]
  assert.equal(group.outcome, 'none')
  assert.equal(group.ranked, 0)
  assert.equal(group.bestValue, null)
  assert.equal(group.rows.length, 0)
  assert.equal(group.unranked.length, 2)
  assert.match(groupClaim(group).claim, /No run of t can hold a rank/)
  // An ABSENT receipt is not a corruption warning — a legacy server that never sends one must not
  // have every run struck from the ranking (`runIndex.js::sourceIncomplete` is the shared rule).
  const legacy = crossRunGroups([run('a', 't', 'max', 0.9), run('b', 't', 'max', 0.1)])
  assert.equal(legacy.groups[0].ranked, 2)
  assert.equal(legacy.groups[0].integrityExcluded, 0)
})

test('a single-run group is not a comparison, and never reads as a win', () => {
  const index = crossRunGroups(CORPUS)
  assert.equal(index.singletons.length, 6)      // 5 singleton tasks + dataset_task/max
  const solo = groupFor(index, 'asha_curve_probe', 'max')
  assert.equal(solo.outcome, 'single')
  assert.deepEqual(solo.rows.map(r => r.rank), [1])
  assert.match(groupClaim(solo).claim, /A single observation is not a comparison/)
})

test('a missing task id or an unreadable direction is never grouped', () => {
  // The panel already refused to group rows whose identity is absent. That rule survives the overlay:
  // an empty task id is a missing value, not a shared one, and rows encoding it identically are not
  // evidence of a shared objective.
  const index = crossRunGroups([
    run('legacy-1', '', 'max', 1), run('legacy-2', '', 'min', 2),
    run('legacy-3', '   ', 'max', 3), run('weird', 'ok', 'sideways', 4),
  ])
  assert.equal(index.groups.length, 0)
  assert.equal(index.totals.unidentified, 4)
  assert.equal(rankCoverage(index).comparableRuns, 0)
  assert.equal(rankCoverage(index).empty, true)
})

test('the confirmed mean wins over the raw best, and the table says which it showed', () => {
  // Mirrors `runIndex.js::sortRuns` so three surfaces cannot read two different numbers off one row.
  assert.deepEqual(runMetric({ best_metric: 0.5, best_confirmed: 0.4 }), { value: 0.4, confirmed: true })
  assert.deepEqual(runMetric({ best_metric: 0.5, best_confirmed: null }), { value: 0.5, confirmed: false })
  assert.equal(runMetric({ best_metric: null, best_confirmed: null }), null)
  // NaN/Infinity are not observations. They arrive from a malformed row, not from an experiment.
  assert.equal(runMetric({ best_metric: Number.NaN }), null)
  assert.equal(runMetric({ best_metric: Number.POSITIVE_INFINITY }), null)
  assert.equal(runMetric(null), null)
  // On the real corpus `best_confirmed` is null for all 36 metric-bearing runs, so a MIXED column is
  // the case nobody has seen yet — which is exactly why it has to be disclosed when it appears.
  const mixed = crossRunGroups([
    run('a', 't', 'max', 0.5, { best_confirmed: 0.45 }), run('b', 't', 'max', 0.4),
  ])
  const claim = groupClaim(mixed.groups[0])
  assert.ok(claim.refusals.some(line => /1 of 2 values are confirmed means/.test(line)))
})

test('an unfinished run ranks but is marked, because its best can still move', () => {
  const index = crossRunGroups([
    run('done', 't', 'min', 0.4), run('live', 't', 'min', 0.2, { finished: false, phase: 'search' }),
  ])
  const group = index.groups[0]
  assert.deepEqual(group.rows.map(r => [r.runId, r.rank, r.provisional]),
    [['live', 1, true], ['done', 2, false]])
  assert.ok(groupClaim(group).refusals.some(line => /have not finished.*best-so-far/s.test(line)))
})

test('every group states the two things a run row can never prove', () => {
  const index = crossRunGroups(CORPUS)
  for (const group of index.groups) {
    const { refusals } = groupClaim(group)
    // A shared task id is an operational lookup key; `/api/runs` carries no metric name/unit/dataset.
    assert.ok(refusals.some(line => /no metric name, unit, dataset or evaluation protocol/.test(line)))
    // And nothing on the row says the number is about this run's own artifact (docs 31/35: v2 node 4
    // trained to 0.737488 and recorded 0.224975 for a checkpoint it never produced).
    assert.ok(refusals.some(line => /not a claim that each number is about the artifact/.test(line)))
  }
})

test('order and bounds are deterministic, whatever order the server listed its runs in', () => {
  const shuffled = [...CORPUS].reverse()
  assert.deepEqual(crossRunGroups(shuffled).groups.map(g => g.key),
    crossRunGroups(CORPUS).groups.map(g => g.key))
  assert.deepEqual(groupFor(crossRunGroups(shuffled), 'toy_quadratic', 'min').rows.map(r => r.runId),
    groupFor(crossRunGroups(CORPUS), 'toy_quadratic', 'min').rows.map(r => r.runId))
  const many = Array.from({ length: 130 }, (_, i) => run(`r${i}`, 't', 'max', i))
  const group = crossRunGroups(many).groups[0]
  assert.equal(group.size, 130)
  assert.equal(group.rows.length, 100)
  assert.equal(group.omitted, 30)
  assert.equal(crossRunGroups(many, { limit: 5 }).groups[0].omitted, 125)
})

test('an empty or malformed payload produces no claim at all', () => {
  assert.equal(rankCoverage(crossRunGroups([])), null)
  assert.equal(rankCoverage(crossRunGroups(null)), null)
  assert.equal(crossRunGroups([null, 7, 'x']).totals.runs, 0)
  assert.equal(groupClaim(null), null)
})
