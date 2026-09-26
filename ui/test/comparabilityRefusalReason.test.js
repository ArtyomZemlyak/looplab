// WHAT REFUSED A RANKING, named — and a refused partition's rows kept on screen.
//
// THE CRITIC'S CASES (2026-09-26, rendered in jsdom and driven through the model):
//   1. a split caused ONLY by the source tree was blamed on "their recorded comparability keys, or the
//      evaluation protocols they ran under" — false on every surface that printed it;
//   2. three runs of one task and one key, one of whose champions was full-scored, went WHOLE to the
//      unranked count: the panel said "No per-run metric observations for this task ID yet" over three
//      runs with values, and the two smoke runs that WERE comparable lost their ranking;
//   3. two runs of ONE task split only by protocol were told "different tasks or objectives" by the
//      compare view, the registry and the run list's sort option.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  COMPARABILITY_REFUSAL_SHORT, COMPARABILITY_REFUSAL_TEXT, CHAMPION_CAVEAT_MIXED_COMPARABILITY,
  bestMetricCaveatNotice, metricComparable, metricIncomparability, metricIncomparabilityText,
  nodesComparabilitySplit, nodesSplitByComparability,
} from '../src/runIndex.js'
import { crossRunGroups, groupClaim, rankCoverage, splitLabel } from '../src/crossRunRank.js'
import { comparableRunRanking } from '../src/portfolioModel.js'

const record = (extra = {}) => ({ version: 1, authority: 'measured', keys: { measured: 'k1' },
  ...extra })
const run = (id, metric, comparability, task = 'repo_task', direction = 'max') => ({
  run_id: id, task_id: task, direction, best_metric: metric, finished: true,
  best_metric_comparability: comparability,
})
const smoke = record({ protocol: { profile: 'aaaa1111' } })
const full = record({ protocol: { profile: 'bbbb2222' } })

test('each refusal is named by the discriminator that refused it', () => {
  assert.equal(metricIncomparability([]), '')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke)]), '')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 'other')]), 'objective')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 'repo_task', 'min')]),
    'objective')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, full)]), 'profile')
  assert.equal(metricIncomparability([
    run('a', 1, record({ substrate: 's1' })), run('b', 2, record({ substrate: 's2' }))]), 'substrate')
  assert.equal(metricIncomparability([
    run('a', 1, record({ protocol: { scorer: 'x' } })),
    run('b', 2, record({ protocol: { scorer: 'y' } }))]), 'scorer')
  assert.equal(metricIncomparability([
    run('a', 1, record()), run('b', 2, record({ keys: { measured: 'k2' } }))]), 'keys')
  // The substrate is asked FIRST, as the engine asks it: a pair differing on both names the tree.
  assert.equal(metricIncomparability([
    run('a', 1, record({ substrate: 's1', protocol: { profile: 'p1' } })),
    run('b', 2, record({ substrate: 's2', protocol: { profile: 'p2' } }))]), 'substrate')
  // Absence is silence, never a refusal: a run that recorded no profile is not refused against one
  // that did.
  assert.equal(metricIncomparability([run('a', 1, record()), run('b', 2, smoke)]), '')
  // …and the predicate every ranking surface asks is exactly "no refusal".
  assert.equal(metricComparable([run('a', 1, smoke), run('b', 2, full)]), false)
  assert.equal(metricComparable([run('a', 1, smoke), run('b', 2, smoke)]), true)
})

test('every refusal has a sentence and a short form, and only the objective says "tasks"', () => {
  for (const reason of ['substrate', 'profile', 'scorer', 'fingerprint', 'keys']) {
    assert.ok(COMPARABILITY_REFUSAL_TEXT[reason], reason)
    assert.ok(COMPARABILITY_REFUSAL_SHORT[reason], reason)
    const text = metricIncomparabilityText(reason)
    assert.match(text, /share one task and objective, but two of them/)
    assert.doesNotMatch(text, /different tasks/)
  }
  assert.match(metricIncomparabilityText('objective'), /different tasks or objectives/)
  assert.match(metricIncomparabilityText('substrate'), /source trees/)
  assert.match(metricIncomparabilityText('profile'), /eval profiles/)
  assert.equal(metricIncomparabilityText(''), '')
})

test('the compare view says WHICH refusal, not "different tasks or objectives"', () => {
  const ranking = comparableRunRanking([run('a', 1, smoke), run('b', 2, full)])
  assert.equal(ranking.status, 'incompatible')
  assert.equal(ranking.reason, 'profile')
  const tasks = comparableRunRanking([run('a', 1, smoke), run('b', 2, smoke, 'other')])
  assert.equal(tasks.reason, 'objective')
})

test('a within-run split names its cause, and the caveat sentence names the source tree', () => {
  const node = (id, comparability) => ({ id, metric: 0.5, metric_provenance: { comparability } })
  const tree = [node(1, record({ substrate: 's1' })), node(2, record({ substrate: 's2' }))]
  assert.equal(nodesComparabilitySplit(tree), 'substrate')
  assert.equal(nodesSplitByComparability(tree), true)
  assert.equal(nodesComparabilitySplit([node(1, smoke), node(2, full)]), 'profile')
  assert.equal(nodesComparabilitySplit([node(1, smoke), node(2, smoke)]), '')
  const notice = bestMetricCaveatNotice({ best_metric_caveats: [CHAMPION_CAVEAT_MIXED_COMPARABILITY] })
  assert.match(notice, /the source trees they ran on/)
  assert.match(notice, /evaluation protocols they ran under/)
})

test('a partition refused whole is split by what differs, and every row stays on screen', () => {
  const index = crossRunGroups([
    run('smoke-1', 0.61, smoke), run('smoke-2', 0.64, smoke), run('full-1', 0.72, full),
  ])
  // Nothing vanishes into the unplaced count any more…
  assert.equal(index.totals.unidentified, 0)
  assert.equal(index.totals.splitRuns, 3)
  assert.equal(index.totals.refusedRuns, 0)
  const shown = index.groups.flatMap(group => [...group.rows, ...group.unranked].map(r => r.runId))
  assert.deepEqual(shown.sort(), ['full-1', 'smoke-1', 'smoke-2'])
  // …the two smoke runs, comparable with each other, keep their ranking…
  const smokeGroup = index.groups.find(group => group.split?.profile === 'aaaa1111')
  assert.equal(smokeGroup.outcome, 'ranked')
  assert.deepEqual(smokeGroup.leaders, ['smoke-2'])
  // …and the full-scored run is its own group, never ordered against them.
  const fullGroup = index.groups.find(group => group.split?.profile === 'bbbb2222')
  assert.equal(fullGroup.size, 1)
  assert.equal(fullGroup.outcome, 'single')
  // The group SAYS why it is a part and not the whole.
  const line = groupClaim(smokeGroup).refusals.find(text => /provably differ from these/.test(text))
  assert.match(line, /eval profile aaaa1111/)
  const coverage = rankCoverage(index)
  assert.equal(coverage.comparableRuns, 2)
  assert.equal(coverage.splitRuns, 3)
})

test('a split by source tree alone says "source tree", and absence is a value of its own', () => {
  const index = crossRunGroups([
    run('a', 0.5, record({ substrate: 's1' })), run('b', 0.6, record({ substrate: 's2' })),
    run('c', 0.7, record({ substrate: 's2' })),
  ])
  const parts = index.groups.map(group => splitLabel(group.split)).sort()
  assert.deepEqual(parts, ['source tree s1', 'source tree s2'])
  // A partition that was never refused carries no split and ranks as it always did.
  const plain = crossRunGroups([run('a', 0.5, record()), run('b', 0.6, record())])
  assert.equal(plain.groups.length, 1)
  assert.equal(plain.groups[0].split, null)
  assert.equal(plain.totals.splitRuns, 0)
  assert.equal(splitLabel({ profile: '' }), 'eval profile (none recorded)')
})

test('a refused part is shown with no rank, and its claim says why', () => {
  const claim = groupClaim({ outcome: 'refused', size: 3, taskId: 't', direction: 'max',
    partition: 'measured:k1', leaders: [], ranked: 0, caveatedCount: 0, integrityExcluded: 0,
    provisionalCount: 0, confirmedCount: 0, split: null })
  assert.match(claim.claim, /provably disagree on their evaluation/)
  assert.match(claim.claim, /none holds a rank/)
})
