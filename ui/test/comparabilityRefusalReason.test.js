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
//
// AND THE SECOND PASS OVER THAT FIX (critic 2026-09-26), whose surviving mutants each have a test
// below that names it:
//   4. a refused part with NOTHING varying in it — task ids differing only by whitespace (the
//      bucket trimmed them, the predicate did not), or keys refused at a stronger authority than
//      the one the partition is grouped by — carried `split = {}`: an empty header label,
//      "provably differ from these on , so", a claim blaming a source tree or protocol, and
//      "2 prefix-folded runs are not drawn" over two complete runs;
//   5. a part set apart only because it recorded NO profile said other runs "provably differ" from
//      it, and the coverage line counted it among the runs that provably differ;
//   6. the run list's sort option said "tasks or directions differ" with one task selected, and
//      equal-size parts came out in the server's listing order.
// The only refused group any test drove used to be BUILT BY HAND, so "a part is never refused",
// "a refused part still ranks", "`integrityExcluded` counts refused rows" and "the comparable
// groups include refused ones" all survived. Every refused group below is reached from RUN ROWS.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  COMPARABILITY_REFUSAL_SHORT, COMPARABILITY_REFUSAL_TEXT, CHAMPION_CAVEAT_MIXED_COMPARABILITY,
  bestMetricCaveatNotice, metricComparable, metricIncomparability, metricIncomparabilityText,
  metricSortRefusal, nodesComparabilitySplit, nodesSplitByComparability,
} from '../src/runIndex.js'
import {
  crossRunGroups, groupClaim, groupTally, rankCoverage, splitClaims, splitLabel, trajectoryClaim,
  trajectoryOverlay, unrankedRowTitle,
} from '../src/crossRunRank.js'
import { comparableRunRanking } from '../src/portfolioModel.js'

const record = (extra = {}) => ({ version: 1, authority: 'measured', keys: { measured: 'k1' },
  ...extra })
const run = (id, metric, comparability, task = 'repo_task', direction = 'max', extra = {}) => ({
  run_id: id, task_id: task, direction, best_metric: metric, finished: true,
  best_metric_comparability: comparability, ...extra,
})
const smoke = record({ protocol: { profile: 'aaaa1111' } })
const full = record({ protocol: { profile: 'bbbb2222' } })
// A record grouped by its DECLARED key whose stronger MEASURED key may still differ: the engine
// always names the strongest family it carries (`comparability_record`), but a browser reading a
// hand-edited or foreign log cannot assume it, and `pairRefusal` asks the strongest COMMON one.
const declared = (measured, extra = {}) => ({ version: 1, authority: 'declared',
  keys: { declared: 'd1', measured }, ...extra })
const prefixFolded = { source_integrity: { complete: false, good_records: 20, dropped_lines: 9 } }

test('each refusal is named by the discriminator that refused it', () => {
  assert.equal(metricIncomparability([]), '')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke)]), '')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 'other')]), 'task')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 'repo_task', 'min')]),
    'direction')
  // A MISSING value is its own refusal, never "they differ".
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, '')]), 'task_unrecorded')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 7)]), 'task_unrecorded')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 'repo_task', null)]),
    'direction_unrecorded')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, full)]), 'profile')
  assert.equal(metricIncomparability([
    run('a', 1, record({ substrate: 's1' })), run('b', 2, record({ substrate: 's2' }))]), 'substrate')
  assert.equal(metricIncomparability([
    run('a', 1, record({ protocol: { scorer: 'x' } })),
    run('b', 2, record({ protocol: { scorer: 'y' } }))]), 'scorer')
  assert.equal(metricIncomparability([
    run('a', 1, record({ protocol: { fingerprint: 'x' } })),
    run('b', 2, record({ protocol: { fingerprint: 'y' } }))]), 'fingerprint')
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

test('MUTANT "protocol facet order": the facets are asked profile, scorer, fingerprint', () => {
  // The engine's `PROTOCOL_FACETS` order, so the browser and `looplab comparability` name the SAME
  // facet for one pair. Reordering the browser's list goes red here.
  const both = (profile, scorer, fingerprint) => record({ protocol: { profile, scorer, fingerprint } })
  assert.equal(metricIncomparability([run('a', 1, both('p1', 's1', 'f1')),
    run('b', 2, both('p2', 's2', 'f2'))]), 'profile')
  assert.equal(metricIncomparability([run('a', 1, both('p1', 's1', 'f1')),
    run('b', 2, both('p1', 's2', 'f2'))]), 'scorer')
  assert.equal(nodesComparabilitySplit([
    { id: 1, metric_provenance: { comparability: both('p1', 's1', 'f1') } },
    { id: 2, metric_provenance: { comparability: both('p1', 's2', 'f2') } }]), 'scorer')
})

test('MUTANT "refusal checked before objective": a set of two objectives is refused as that', () => {
  // Two tasks whose runs ALSO recorded different profiles were never one objective, and saying
  // "two of them were scored under different eval profiles" would imply they otherwise could be.
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, full, 'other')]), 'task')
  assert.equal(metricIncomparability([
    run('a', 1, record({ substrate: 's1' })),
    run('b', 2, record({ substrate: 's2' }), 'repo_task', 'min')]), 'direction')
  // …and a PROVEN difference is named before the absence beside it.
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 'other'),
    run('c', 3, smoke, '')]), 'task')
  assert.equal(metricIncomparability([run('a', 1, smoke), run('b', 2, smoke, 'repo_task', 'min'),
    run('c', 3, smoke, 'repo_task', null)]), 'direction')
})

test('MUTANT "absence not treated as silence": a facet one side never recorded refuses nothing', () => {
  // The protocol object present on BOTH sides, the facet on one: the early return for a missing
  // `protocol` does not cover it, only the per-facet "both sides carry it" check does.
  assert.equal(metricIncomparability([
    run('a', 1, record({ protocol: { scorer: 'x' } })),
    run('b', 2, record({ protocol: { scorer: 'x', profile: 'p1' } }))]), '')
  assert.equal(metricIncomparability([
    run('a', 1, record({ protocol: { profile: '' } })), run('b', 2, smoke)]), '')
  assert.equal(metricIncomparability([run('a', 1, record()),
    run('b', 2, record({ substrate: 's1' }))]), '')
})

test('task ids that differ only by whitespace are ONE task, ranked as one', () => {
  // The bucket trimmed and the predicate did not: the two landed in one bucket, the predicate
  // refused it as two tasks, and the refused part had nothing varying in it to split or name.
  const rows = [run('a', 0.5, record(), 'repo_task'), run('b', 0.6, record(), 'repo_task ')]
  assert.equal(metricIncomparability(rows), '')
  const index = crossRunGroups(rows)
  assert.equal(index.groups.length, 1)
  const [group] = index.groups
  assert.equal(group.outcome, 'ranked')
  assert.equal(group.split, null)
  assert.deepEqual(group.leaders, ['b'])
  assert.equal(index.totals.refusedRuns, 0)
  assert.equal(comparableRunRanking(rows).status, 'ranked')
})

test('every refusal has a sentence and a short form, and only a task refusal says "tasks"', () => {
  for (const reason of ['substrate', 'profile', 'scorer', 'fingerprint', 'keys']) {
    assert.ok(COMPARABILITY_REFUSAL_TEXT[reason], reason)
    assert.ok(COMPARABILITY_REFUSAL_SHORT[reason], reason)
    const text = metricIncomparabilityText(reason)
    assert.match(text, /share one task and objective, but two of them/)
    assert.doesNotMatch(text, /different tasks/)
  }
  assert.equal(metricIncomparabilityText('task'), 'these runs are of different tasks')
  assert.equal(metricIncomparabilityText('direction'),
    'these runs share one task, but one minimizes its metric where another maximizes it')
  assert.match(metricIncomparabilityText('task_unrecorded'), /not every one of these runs records a task id/)
  assert.match(metricIncomparabilityText('direction_unrecorded'), /not every one of them records a min\/max direction/)
  for (const reason of ['direction', 'direction_unrecorded', 'task_unrecorded']) {
    assert.doesNotMatch(metricIncomparabilityText(reason), /different tasks|tasks or/, reason)
  }
  assert.match(metricIncomparabilityText('substrate'), /source trees/)
  assert.match(metricIncomparabilityText('profile'), /eval profiles/)
  assert.equal(metricIncomparabilityText(''), '')
  // The SHORT words the sort option prints, pinned: each names only what differs.
  assert.deepEqual(
    ['task', 'task_unrecorded', 'direction', 'direction_unrecorded'].map(r => COMPARABILITY_REFUSAL_SHORT[r]),
    ['tasks differ', 'task not recorded', 'directions differ', 'direction not recorded'])
})

test('the run list\'s sort option says what differs, from the one derivation that also disables it', () => {
  const same = [run('a', 1, smoke), run('b', 2, smoke)]
  const directions = [run('a', 1, smoke), run('b', 2, smoke, 'repo_task', 'min')]
  assert.equal(metricSortRefusal(same, { taskSelected: false }), 'select one task')
  assert.equal(metricSortRefusal([], { taskSelected: true }), 'no runs')
  assert.equal(metricSortRefusal(same, { taskSelected: true }), '')
  // ONE task selected, its runs split only by direction: the option names the DIRECTION, and a
  // task difference the selection has ruled out is never named.
  assert.equal(metricSortRefusal(directions, { taskSelected: true }), 'directions differ')
  assert.equal(metricSortRefusal([run('a', 1, smoke), run('b', 2, full)], { taskSelected: true }),
    'eval profiles differ')
  assert.equal(metricSortRefusal([run('a', 1, smoke), run('b', 2, smoke, 'repo_task', null)],
    { taskSelected: true }), 'direction not recorded')
})

test('the compare view says WHICH refusal, not "different tasks or objectives"', () => {
  const ranking = comparableRunRanking([run('a', 1, smoke), run('b', 2, full)])
  assert.equal(ranking.status, 'incompatible')
  assert.equal(ranking.reason, 'profile')
  assert.equal(comparableRunRanking([run('a', 1, smoke), run('b', 2, smoke, 'other')]).reason, 'task')
  assert.equal(comparableRunRanking([run('a', 1, smoke),
    run('b', 2, smoke, 'repo_task', 'min')]).reason, 'direction')
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
  assert.equal(index.totals.splitProvenRuns, 3)
  assert.equal(index.totals.splitUnrecordedRuns, 0)
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
  const line = groupClaim(smokeGroup).refusals.find(text => /provably differ from/.test(text))
  assert.match(line, /recorded a different eval profile provably differ from these runs \(eval profile aaaa1111\)/)
  const coverage = rankCoverage(index)
  assert.equal(coverage.comparableRuns, 2)
  assert.equal(coverage.comparableGroups, 1)
  assert.equal(coverage.splitRuns, 3)
  assert.equal(coverage.splitProvenRuns, 3)
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

test('a run that recorded NO profile beside two that disagree is set apart, and never "provably"', () => {
  // THE ABSENCE PART. It is its own part — it cannot be placed on either side of two profiles that
  // disagree — and it says so in those words: nothing proves it differs from either.
  const index = crossRunGroups([run('p1', 0.5, record({ protocol: { profile: 'P1' } })),
    run('p2', 0.6, record({ protocol: { profile: 'P2' } })), run('none', 0.7, record())])
  assert.equal(index.groups.length, 3, 'absence IS a value where the facet splits: its own part')
  const none = index.groups.find(group => group.split?.profile === '')
  assert.deepEqual(none.rows.map(row => row.runId), ['none'])
  assert.deepEqual([none.splitProven, none.splitUnrecorded], [[], ['profile']])
  const [noneLine] = splitClaims(none)
  assert.match(noneLine, /^This run recorded no eval profile, while other runs of this task with the same comparability key recorded conflicting ones — not recorded, so not comparable with either side/)
  assert.doesNotMatch(splitClaims(none).join(' '), /provably/)
  assert.deepEqual(groupClaim(none).refusals.slice(-1), [noneLine])
  // A part that DID record one provably differs from the part that recorded the other, and is set
  // apart from the unrecorded one WITHOUT the word.
  const p1 = index.groups.find(group => group.split?.profile === 'P1')
  assert.deepEqual([p1.splitProven, p1.splitUnrecorded, p1.splitApart], [['profile'], [], ['profile']])
  const [proven, apart] = splitClaims(p1)
  assert.match(proven, /recorded a different eval profile provably differ from this run \(eval profile P1\)/)
  assert.match(apart, /recorded no eval profile are ranked apart from this run as well, though a missing eval profile proves no difference/)
  // Scoped to the facet: the none part's absence line never says its RUN is proven the same or not.
  assert.match(noneLine, /It is ranked apart from those runs, though a missing eval profile proves no difference\.$/)
  // COVERAGE counts only the proven ones as provably differing.
  const coverage = rankCoverage(index)
  assert.deepEqual([coverage.splitRuns, coverage.splitProvenRuns, coverage.splitUnrecordedRuns],
    [3, 2, 1])
})

test('a part proven on one facet and unrecorded on another says each on its own terms', () => {
  const index = crossRunGroups([
    run('a', 0.5, record({ substrate: 's1', protocol: { profile: 'P1' } })),
    run('b', 0.6, record({ substrate: 's1' })),
    run('c', 0.7, record({ substrate: 's2', protocol: { profile: 'P2' } })),
  ])
  const b = index.groups.find(group => group.rows.some(r => r.runId === 'b'))
  assert.equal(splitLabel(b.split), 'source tree s1, eval profile (none recorded)')
  assert.deepEqual([b.splitProven, b.splitUnrecorded], [['substrate'], ['profile']])
  const [proven, unrecorded, ...rest] = splitClaims(b)
  assert.match(proven, /recorded a different source tree provably differ from this run \(source tree s1\)/)
  assert.match(unrecorded, /^This run recorded no eval profile, .* a missing eval profile proves no difference\.$/)
  assert.deepEqual(rest, [])
  // It PROVABLY differs from the s2 run, so the coverage line counts it among those that do.
  assert.deepEqual([index.totals.splitProvenRuns, index.totals.splitUnrecordedRuns], [3, 0])
})

test('a facet that varies by ABSENCE alone splits nothing', () => {
  // `substrate` refuses (s1 vs s2); `profile` holds ONE recorded value beside none, which refuses
  // no pair — so a and b, which nothing separates, are ranked together, and the s1 part's label
  // names only what split it.
  const index = crossRunGroups([
    run('a', 0.5, record({ substrate: 's1', protocol: { profile: 'P1' } })),
    run('b', 0.6, record({ substrate: 's1' })),
    run('c', 0.7, record({ substrate: 's2', protocol: { profile: 'P1' } })),
  ])
  const s1 = index.groups.find(group => group.split?.substrate === 's1')
  assert.deepEqual(s1.split, { substrate: 's1' })
  assert.equal(s1.outcome, 'ranked')
  assert.deepEqual(s1.leaders, ['b'])
  assert.equal(index.groups.length, 2)
})

test('equal-size parts come out in ONE order, whatever order the server listed them in', () => {
  // MUTANT "no key tiebreak": two parts of one split share size, task and direction, and without
  // the key they kept the listing order — reversing the input reversed them.
  const rows = [run('x', 0.5, record({ substrate: 's1' })), run('y', 0.6, record({ substrate: 's2' })),
    run('z', 0.7, record({ substrate: 's3' }))]
  const keys = list => crossRunGroups(list).groups.map(group => group.split.substrate)
  assert.deepEqual(keys(rows), ['s1', 's2', 's3'])
  assert.deepEqual(keys([...rows].reverse()), ['s1', 's2', 's3'])
  assert.deepEqual(keys([rows[1], rows[2], rows[0]]), ['s1', 's2', 's3'])
})

test('a partition refused with NOTHING varying stays one group — refused, unranked, and worded from its refusal', () => {
  // THE MODEL PATH TO `refused`, from run rows. Keys at a stronger authority than the one the
  // partition is grouped by disagree; no source tree or protocol varies to split it by.
  const index = crossRunGroups([
    run('a', 0.5, declared('m1')), run('b', 0.7, declared('m2')),
    run('c', 0.9, declared('m1'), 'repo_task', 'max', prefixFolded),
    run('solo', 0.4, record(), 'other_task'),
  ])
  const refused = index.groups.find(group => group.taskId === 'repo_task')
  // MUTANT "part never refused":
  assert.equal(refused.outcome, 'refused')
  assert.equal(refused.refusal, 'keys')
  assert.deepEqual(index.refused.map(group => group.key), [refused.key])
  // …with NO split: nothing varied, so there is no part to name.
  assert.equal(refused.split, null)
  assert.deepEqual(splitClaims(refused), [])
  // MUTANT "refused part still ranks": no rank anywhere, every row still on screen with its value.
  assert.equal(refused.ranked, 0)
  assert.deepEqual(refused.rows, [])
  assert.deepEqual(refused.leaders, [])
  assert.deepEqual(refused.unranked.map(row => [row.runId, row.value, row.rank]),
    [['a', 0.5, null], ['b', 0.7, null], ['c', 0.9, null]])
  // MUTANT "`integrityExcluded` counts refused rows": only the prefix-folded one.
  assert.equal(refused.integrityExcluded, 1)
  assert.equal(index.totals.integrityExcluded, 1)
  // MUTANT "comparable groups include refused ones":
  assert.deepEqual(index.comparable, [])
  assert.equal(index.totals.comparableGroups, 0)
  assert.equal(index.totals.comparableRuns, 0)
  assert.equal(index.totals.refusedRuns, 3)
  assert.equal(index.totals.splitRuns, 0)
  // MUTANT "the old `singletonTasks` formula" (`groups - comparableGroups` = 2): one ONE-RUN group.
  const coverage = rankCoverage(index)
  assert.equal(coverage.singletonTasks, 1)
  assert.equal(coverage.comparableGroups, 0)
  assert.equal(coverage.refusedRuns, 3)
  // WORDED FROM ITS REFUSAL — the keys — and never from a split that did not happen.
  const claim = groupClaim(refused)
  assert.equal(claim.claim, 'None of these 3 runs of repo_task holds a rank: two of them were '
    + 'measured against different evaluation inputs (their recorded comparability keys differ). '
    + 'Each value is shown and is true of its own measurement; no ordering between them is.')
  assert.doesNotMatch([claim.claim, ...claim.refusals].join(' '), /source tree or protocol|provably differ from/)
  assert.ok(claim.refusals.some(line => line.startsWith('These 3 runs are grouped by a shared '
    + 'comparability key (declared:d1), but a pair of them is refused all the same')))
  // Each unranked row names the refusal; the prefix-folded one ALSO says it is a prefix.
  const [plainRow] = refused.unranked
  assert.match(unrankedRowTitle(refused, plainRow),
    /^no rank: two runs of this group were measured against different evaluation inputs/)
  assert.doesNotMatch(unrankedRowTitle(refused, plainRow), /PREFIX/)
  assert.match(unrankedRowTitle(refused, refused.unranked[2]), /; and this run's event log stops being readable/)
  // THE OVERLAY counts the prefix-folded run as prefix-folded and the other two as refused —
  // `prefix` used to count every unranked row.
  const overlay = trajectoryOverlay(refused)
  assert.deepEqual([overlay.drawn, overlay.prefix, overlay.refused], [0, 1, 2])
  const sentence = trajectoryClaim(refused, overlay)
  assert.match(sentence, /1 prefix-folded run is not drawn/)
  assert.match(sentence, /2 runs are not drawn, for the reason they hold no rank: a pair of this group's runs provably disagrees on its evaluation/)
  assert.doesNotMatch(sentence, /[23] prefix-folded/)
})

test('a part that STILL disagrees after the split keeps its split and is worded from its own refusal', () => {
  // The profile splits the partition; inside the P1 part two runs disagree on the stronger key.
  const index = crossRunGroups([
    run('a', 0.5, declared('m1', { protocol: { profile: 'P1' } })),
    run('b', 0.6, declared('m2', { protocol: { profile: 'P1' } })),
    run('c', 0.7, declared('m1', { protocol: { profile: 'P2' } })),
  ])
  const p1 = index.groups.find(group => group.split?.profile === 'P1')
  assert.equal(p1.outcome, 'refused')
  assert.equal(p1.refusal, 'keys')
  assert.equal(p1.ranked, 0)
  assert.match(groupClaim(p1).claim, /two of them were measured against different evaluation inputs/)
  assert.match(splitClaims(p1)[0], /provably differ from these runs \(eval profile P1\)/)
  const p2 = index.groups.find(group => group.split?.profile === 'P2')
  assert.equal(p2.outcome, 'single')
  assert.deepEqual([index.totals.splitProvenRuns, index.totals.refusedRuns], [3, 2])
})

test('ONE tally counts groups for the box and for one task, so the two cannot contradict', () => {
  // The toolbar said "3 comparable groups" (every group of the task) over a coverage line that
  // said "0 of them sit in 0 comparable groups" (only the rankable ones).
  const index = crossRunGroups([run('p1', 0.5, record({ protocol: { profile: 'P1' } })),
    run('p2', 0.6, record({ protocol: { profile: 'P2' } })), run('none', 0.7, record())])
  const task = groupTally(index.groups.filter(group => group.taskId === 'repo_task'))
  assert.deepEqual([task.groups, task.comparableGroups, task.singletonGroups], [3, 0, 3])
  assert.equal(task.comparableGroups, rankCoverage(index).comparableGroups)
  const two = crossRunGroups([run('s1', 0.61, smoke), run('s2', 0.64, smoke), run('f1', 0.72, full)])
  const tally = groupTally(two.groups)
  assert.deepEqual([tally.groups, tally.comparableGroups, tally.comparableRuns], [2, 1, 2])
  assert.deepEqual(tally, Object.fromEntries(Object.keys(tally).map(key => [key, two.totals[key]])))
})

test('a refused group handed no refusal code still reads as refused and names no cause it lacks', () => {
  // A hand-built legacy shape: the fallback may not invent a source tree, a protocol or a key.
  const group = { outcome: 'refused', size: 3, taskId: 't', direction: 'max',
    partition: 'measured:k1', leaders: [], ranked: 0, caveatedCount: 0, integrityExcluded: 0,
    provisionalCount: 0, confirmedCount: 0, split: null }
  const claim = groupClaim(group)
  assert.match(claim.claim, /^None of these 3 runs of t holds a rank: two of them provably disagree on their evaluation\./)
  assert.doesNotMatch(claim.claim, /source tree|protocol|keys/)
})
