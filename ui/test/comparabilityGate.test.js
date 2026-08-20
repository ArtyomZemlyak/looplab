// THE BROWSER HALF OF THE COMPARABILITY REFUSAL.
//
// THE INCIDENT: `runs/` on this box holds recall@100 values of 0.8776, 0.793426, 0.792082 and
// 0.774207, all under one `repo_task` header, and they were compared for a day. Some were measured
// on one test set and some on another; the product index also changed independently of it, and a
// bigger corpus makes recall@100 strictly harder. `crossRunRank.js` had already written down that it
// could not see this — "a shared task_id is an operational lookup key … two runs of `repo_task` may
// have optimized recall@100 against different corpora" — and ranked them anyway, because the row
// carried nothing to refuse on. It does now.
//
// The tests below drive the PROPERTY through the shipped model functions, not the digest's presence.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  COMPARABILITY_DIFFERENT, COMPARABILITY_SAME, COMPARABILITY_UNKNOWN, comparabilityConflict,
  comparabilityStatus, metricComparable, nodesSplitByComparability, sortRuns,
} from '../src/runIndex.js'
import { crossRunGroups, groupClaim } from '../src/crossRunRank.js'

const key = (digest, authority = 'measured') => ({
  version: 1, authority, keys: { [authority]: digest },
})
const run = (id, comparability, metric) => ({
  run_id: id, task_id: 'repo_task', direction: 'max', best_metric: metric, finished: true,
  best_metric_comparability: comparability,
})

test('two rows that recorded nothing have not agreed', () => {
  // THE INVERSION, and the whole defect. Every run row on this box has no key; a default of
  // absent-means-equal would certify the entire corpus as mutually comparable — which is exactly the
  // false statement that was being acted on. `unknown` is a third value, not a synonym for `same`.
  assert.equal(comparabilityStatus({}, {}), COMPARABILITY_UNKNOWN)
  assert.equal(comparabilityStatus(run('a', null, 1), run('b', null, 2)), COMPARABILITY_UNKNOWN)
  assert.equal(comparabilityStatus(run('a', key('x'), 1), run('b', null, 2)), COMPARABILITY_UNKNOWN)
})

test('an inferred match may refuse and may not certify', () => {
  // `e5small-dr-unified-v2` and `-v4` have byte-identical task snapshots, so the engine's weakest
  // authority calls them equal. That proves two DECLARATIONS match and nothing about the data — it
  // is precisely the pair this exists for, so equality there is UNKNOWN and inequality is DIFFERENT.
  const same = comparabilityStatus(run('v2', key('abc', 'inferred'), 1),
    run('v4', key('abc', 'inferred'), 2))
  assert.equal(same, COMPARABILITY_UNKNOWN)
  const other = comparabilityStatus(run('v2', key('abc', 'inferred'), 1),
    run('x', key('zzz', 'inferred'), 2))
  assert.equal(other, COMPARABILITY_DIFFERENT)
  assert.equal(comparabilityStatus(run('a', key('abc'), 1), run('b', key('abc'), 2)),
    COMPARABILITY_SAME)
})

test('the shared ranking predicate refuses a proven cross-key comparison', () => {
  // `metricComparable` is the ONE gate the run list's metric sort, `RegistryPanel`, `ParetoPanel`'s
  // cross-run rung and `crossRunRank.js` all ask — `crossRunRank` re-tests its own partitions with it
  // precisely so a tightening here reaches every surface on one commit.
  const corpusA = run('corpusA', key('1111'), 0.793426)
  const corpusB = run('corpusB', key('2222'), 0.774207)
  const corpusA2 = run('corpusA2', key('1111'), 0.792082)
  assert.equal(comparabilityConflict([corpusA, corpusB]), true)
  assert.equal(metricComparable([corpusA, corpusB]), false)
  assert.equal(metricComparable([corpusA, corpusA2]), true)
  // FAIL OPEN ON UNKNOWN. Every existing row has no key; refusing on absence would blank the whole
  // corpus and hide legitimate prior results, which is worse than the defect.
  assert.equal(metricComparable([run('old1', null, 0.8776), run('old2', null, 0.762048)]), true)
})

test('the metric sort will not order two runs measured against different corpora', () => {
  // The refusal has to reach the SORT and not only the predicate: this is the click an operator makes
  // to find out which configuration won.
  const rows = [run('corpusB', key('2222'), 0.774207), run('corpusA', key('1111'), 0.793426)]
  assert.deepEqual(sortRuns(rows, 'metric', 'asc').map(r => r.run_id), ['corpusB', 'corpusA'],
    'unsorted — the input order is returned unchanged, exactly as for a mixed-direction set')
  const ok = [run('a2', key('1111'), 0.792082), run('a1', key('1111'), 0.793426)]
  assert.deepEqual(sortRuns(ok, 'metric', 'asc').map(r => r.run_id), ['a1', 'a2'])
})

test('the cross-run panel partitions one task into one group per evaluation', () => {
  // NOT "drops the group": every subset stays on screen with its values shown, which is this
  // module's own rule. What changes is that a rank is published only inside a partition.
  const index = crossRunGroups([
    run('corpusA', key('1111'), 0.793426),
    run('corpusA2', key('1111'), 0.792082),
    run('corpusB', key('2222'), 0.774207),
  ])
  const partitions = index.groups.map(g => g.partition).sort()
  assert.deepEqual(partitions, ['measured:1111', 'measured:2222'])
  const big = index.groups.find(g => g.partition === 'measured:1111')
  assert.deepEqual(big.leaders, ['corpusA'])
  assert.equal(index.groups.find(g => g.partition === 'measured:2222').size, 1,
    'the run on the other corpus holds its own group and is not ranked against the first two')
})

test('a keyed run leaves the unkeyed partition rather than joining it', () => {
  // THE ASYMMETRY, and it is the inversion in the one place it changes what an operator sees. "We
  // have not been shown these are the same evaluation" is not "they are".
  const index = crossRunGroups([
    run('legacy1', null, 0.8776),
    run('legacy2', null, 0.762048),
    run('keyed', key('1111'), 0.793426),
  ])
  assert.equal(index.groups.length, 2)
  const legacy = index.groups.find(g => g.partition === '')
  assert.deepEqual(legacy.rows.map(r => r.runId).sort(), ['legacy1', 'legacy2'])
  assert.equal(legacy.comparability, COMPARABILITY_UNKNOWN)
})

test('the corpus as it stands today does not move by one row', () => {
  // THE INERTNESS PROOF. Every run directory here has no key, so the partition is one bucket and the
  // ranking is byte-for-byte what it was — which is the only reason this is safe to ship on a box
  // whose entire corpus predates the record.
  const corpus = [run('a', null, 0.8776), run('b', null, 0.793426), run('c', null, 0.774207)]
  const index = crossRunGroups(corpus)
  assert.equal(index.groups.length, 1)
  assert.equal(index.groups[0].size, 3)
  assert.deepEqual(index.groups[0].leaders, ['a'])
})

test('every group states its comparability, including when the answer is unknown', () => {
  // Silence is what caused the incident: an operator looking at four values in one group had nothing
  // on screen telling them the numbers came from more than one test set. The sentence is printed for
  // BOTH answers so its absence can never be read as assent.
  const unkeyed = groupClaim(crossRunGroups([run('a', null, 1), run('b', null, 2)]).groups[0])
  assert.ok(unkeyed.refusals.some(line => /No run in this group records a comparability key/.test(line)))
  assert.ok(unkeyed.refusals.some(line => /unknown is not the\s+same as yes/s.test(line)))
  const keyed = groupClaim(crossRunGroups([run('a', key('1111'), 1), run('b', key('1111'), 2)]).groups[0])
  assert.ok(keyed.refusals.some(line => /share a recorded comparability key \(measured:1111\)/.test(line)))
})

test('a run whose own nodes were measured differently is detectable from the node records', () => {
  // The within-run half, which `ParetoPanel` uses: dominance is a pairwise metric comparison, so a
  // front over nodes scored on different corpora is not a front. Absent keys contribute nothing.
  const node = (id, comparability) => ({ id, metric_provenance: comparability ? { comparability } : null })
  assert.equal(nodesSplitByComparability([node(1, key('a')), node(2, key('b'))]), true)
  assert.equal(nodesSplitByComparability([node(1, key('a')), node(2, key('a'))]), false)
  assert.equal(nodesSplitByComparability([node(1, null), node(2, null)]), false)
  assert.equal(nodesSplitByComparability([node(1, key('a')), node(2, null)]), false)
})
