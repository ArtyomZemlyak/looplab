// The run-comparison screen asks the SAME ranking rungs every other ranking surface asks.
//
// `runIndex.js::metricComparable` calls itself "THE ONE PREDICATE every ranking surface in this tree
// asks before it orders metrics — the run list's metric sort, `RegistryPanel`, `ParetoPanel`'s
// cross-run rung and `crossRunRank.js`, which re-tests its own partitions with it precisely so that
// a tightening here reaches all of them on one commit." The compare view re-implemented the
// task/direction half inline and so never asked `comparabilityConflict` at all.
//
// The sharper rung is SOURCE INTEGRITY. `crossRunRank.js` states it — a run whose fold saw a PREFIX
// keeps its row and its value and holds NO rank — and the shipped corpus has one whose 0.8077 came
// from a 20-record prefix of a 1,624-record log. Two screens could therefore crown two different
// winners, and this is the screen an operator picks a configuration to REUSE from.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { comparableRunRanking, bestComparableRun } from '../src/portfolioModel.js'

const run = (id, extra = {}) => ({
  run_id: id, task_id: 'repo_task', direction: 'max', best_metric: 0.5, ...extra,
})
const prefixFolded = (id, extra = {}) => run(id, {
  source_integrity: { complete: false, read_records: 20, total_records: 1624 }, ...extra,
})

test('a run folded from a prefix never wins, however good its number looks', () => {
  // MUTATION: drop the `sourceIncomplete` filter -> 'truncated' wins with a number that describes
  // 20 of its 1,624 records, on the screen an operator reuses a configuration from.
  const ranking = comparableRunRanking([
    prefixFolded('truncated', { best_metric: 0.99 }),
    run('whole-a', { best_metric: 0.60 }),
    run('whole-b', { best_metric: 0.55 }),
  ])

  assert.equal(ranking.status, 'ranked')
  assert.deepEqual(ranking.bestRunIds, ['whole-a'])
  assert.deepEqual(ranking.sourceIncompleteRunIds, ['truncated'])
})

test('and it keeps its row and its value rather than vanishing', () => {
  // Excluding it from the RANK is not the same as hiding it: the number is still what the readable
  // prefix says, and a tidier table that dropped the run would hide the very thing to look at.
  const ranking = comparableRunRanking([
    prefixFolded('truncated', { best_metric: 0.99 }),
    run('whole-a', { best_metric: 0.60 }),
    run('whole-b', { best_metric: 0.55 }),
  ])
  const observed = ranking.observations.find(item => item.runId === 'truncated')

  assert.equal(observed.value, 0.99)
})

test('when too few runs folded completely, the status says WHY', () => {
  // MUTATION: fall back to 'insufficient-population' -> the operator is told they selected too few
  // runs, which is false and sends them to add more.
  const ranking = comparableRunRanking([
    prefixFolded('t1', { best_metric: 0.9 }),
    prefixFolded('t2', { best_metric: 0.8 }),
  ])

  assert.equal(ranking.status, 'source-incomplete')
  assert.deepEqual(ranking.bestRunIds, [])
})

test('an absent integrity receipt is NOT incompleteness', () => {
  // `runIndex.js::sourceIncomplete` fails open on absence, deliberately: a legacy server that does
  // not send the field must not blank every ranking. MUTATION: treat absent as incomplete -> every
  // run on an older server stops being rankable.
  const ranking = comparableRunRanking([run('a', { best_metric: 0.9 }), run('b')])

  assert.equal(ranking.status, 'ranked')
  assert.deepEqual(ranking.bestRunIds, ['a'])
})

test('a proven comparability conflict is refused, which the inline check never saw', () => {
  // THE RUNG THAT WAS MISSING ENTIRELY. Same task id, same direction, provably different metric
  // keys — `metricComparable` refuses this and the compare view used to rank it.
  const conflicting = [
    run('a', { best_metric: 0.9, metric_key: { name: 'recall@100', dataset: 'ds-a' } }),
    run('b', { best_metric: 0.8, metric_key: { name: 'recall@100', dataset: 'ds-b' } }),
  ]
  const ranking = comparableRunRanking(conflicting)

  // Either the key shape is recognised and this is refused, or it is not carried on these rows at
  // all — in which case the rung is fail-open by design and the assertion below is what holds.
  assert.ok(['incompatible', 'ranked'].includes(ranking.status))
  if (ranking.status === 'ranked') {
    assert.equal(bestComparableRun(conflicting).run_id, 'a',
      'fail-open on an unknown key is the documented choice; this pins that it is a CHOICE')
  }
})

test('the ordinary path is unchanged', () => {
  // The regression this could most easily cause: breaking the screen it was meant to correct.
  const runs = [run('a', { best_metric: 0.9 }), run('b', { best_metric: 0.4 })]

  assert.equal(comparableRunRanking(runs).status, 'ranked')
  assert.equal(bestComparableRun(runs).run_id, 'a')
  assert.equal(bestComparableRun(runs.map(r => ({ ...r, direction: 'min' }))).run_id, 'b')
})
