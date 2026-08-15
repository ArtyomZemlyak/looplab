// `/api/runs`'s `best_metric` COULD ALREADY BE A SALVAGED OR TRUST-FLAGGED NUMBER, AND SAID NOTHING.
//
// The row `serve/run_projections.py::run_summaries` publishes carries the champion's number and
// nothing else about it — no `violations`, no `metric_provenance`, no node id — while
// `RunState.best()` selects from whatever that run's own rungs were willing to crown. Two rungs crown
// a caveated number: `metric_salvage: "select"`, which mints NO violation row (so a metric recovered
// from a FAILED eval is feasible and competes), and `trust_gate: "audit"` — the shipped DEFAULT —
// which enforces nothing (so a hard reward-hack/leakage signal excludes nothing). Neither fact is
// derivable in the browser, which is why the fix was one server field (`best_metric_caveats`,
// `looplab/engine/champion_caveats.py`) and not thirteen client fixes.
//
// This file drives the two client surfaces wired to it: the reading rule + wording in `runIndex.js`
// (one home, as `sourceIncomplete`/`sourceIntegrityNotice` are for the integrity receipt beside it),
// and the CROSS-RUN LEADERBOARD, which is the surface that makes a claim an operator acts on — they
// go and reuse the winning configuration. The decision there is CAVEAT, NOT UNRANK, and it is the
// opposite of the `sourceIncomplete` rung one line above it in `buildGroup`: an incomplete log means
// the value shown is the best of a PREFIX and no ordering can use it, while a caveated value IS this
// run's best, crowned by the run's own selector under a rung the operator configured.
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CHAMPION_CAVEAT_PARAMS_OVERRIDDEN,
  CHAMPION_CAVEAT_SALVAGED, CHAMPION_CAVEAT_TRUST_FLAGGED, bestMetricCaveatLabel,
  bestMetricCaveatNotice, bestMetricCaveats, bestMetricCaveated,
} from '../src/runIndex.js'
import { crossRunGroups, groupClaim } from '../src/crossRunRank.js'

const run = (run_id, best_metric, extra = {}) => ({
  run_id, task_id: 'repo_task', direction: 'max', best_metric, best_confirmed: null, nodes: 4,
  finished: true, phase: 'finished', label: run_id, best_metric_caveats: [], ...extra,
})

// ---------------------------------------------------------------------------------------------
// THE READING RULE. Total over junk, and an ABSENT field is `[]` — a legacy server that does not
// send it must not paint every run on the box with a caveat.
// ---------------------------------------------------------------------------------------------

test('the caveat list is read, never invented, and never dropped', () => {
  assert.deepEqual(bestMetricCaveats(), [])
  assert.deepEqual(bestMetricCaveats({}), [], 'an absent field is not a caveat')
  assert.deepEqual(bestMetricCaveats({ best_metric_caveats: null }), [])
  assert.deepEqual(bestMetricCaveats({ best_metric_caveats: 'salvaged' }), [],
    'a bare string is not a list; a caveat nobody recorded must not be invented')
  assert.deepEqual(bestMetricCaveats({ best_metric_caveats: ['salvaged', '', 7, ' ', null] }),
    ['salvaged'], 'junk entries are dropped, the recorded one survives')
  assert.equal(bestMetricCaveated({ best_metric_caveats: [] }), false)
  assert.equal(bestMetricCaveated({ best_metric_caveats: [CHAMPION_CAVEAT_TRUST_FLAGGED] }), true)

  // A slug this browser has no word for is KEPT and printed raw. A server that learns a third
  // caveat must not read as a clean run to an older browser — dropping it republishes the number
  // as unqualified, which is the exact silence this field exists to break.
  assert.deepEqual(bestMetricCaveats({ best_metric_caveats: ['someday_rung'] }), ['someday_rung'])
  assert.equal(bestMetricCaveatLabel('someday_rung'), 'someday_rung')
  assert.match(bestMetricCaveatNotice({ best_metric_caveats: ['someday_rung'] }),
    /caveat this view has no sentence for: “someday_rung”/)
})

test('each caveat says what the number is AND that the run selected on it anyway', () => {
  assert.equal(bestMetricCaveatNotice({ best_metric_caveats: [] }), '')
  const salvaged = bestMetricCaveatNotice({ best_metric_caveats: [CHAMPION_CAVEAT_SALVAGED] })
  assert.match(salvaged, /NOT measured/)
  assert.match(salvaged, /recovered the number with its own declared reader/)
  assert.match(salvaged, /competes for champion like a measured result/)
  const flagged = bestMetricCaveatNotice({ best_metric_caveats: [CHAMPION_CAVEAT_TRUST_FLAGGED] })
  assert.match(flagged, /reward-hacking or leakage signal/)
  assert.match(flagged, /selected as this run’s best anyway/)
  // Both facts about one number, both said.
  const both = bestMetricCaveatNotice({
    best_metric_caveats: [CHAMPION_CAVEAT_SALVAGED, CHAMPION_CAVEAT_TRUST_FLAGGED] })
  assert.match(both, /NOT measured/)
  assert.match(both, /reward-hacking or leakage signal/)
  // THE THIRD MEMBER says something the other two cannot, and the wording has to keep the two
  // apart: the metric was measured normally, and what is in question is the recipe beside it. It
  // is the one caveat that is non-empty on this box's corpus (v8 node 3, the champion — declared
  // `batch_size 8192`, `train.py:31` assigns 4096).
  const overridden = bestMetricCaveatNotice({
    best_metric_caveats: [CHAMPION_CAVEAT_PARAMS_OVERRIDDEN] })
  assert.match(overridden, /assigns a different value to a parameter its own experiment record/)
  assert.match(overridden, /the declared configuration is not the one this result was produced/)
  assert.match(overridden, /metric itself was measured normally/)
  assert.doesNotMatch(overridden, /no sentence for/,
    'a server slug the browser knows must never fall through to the unknown-caveat sentence')
  // The label is a table cell's width, and `salvaged` is deliberately the same word
  // `trustSemantics.js::OBJECTIVE_SALVAGED` prints for the node inside the run.
  assert.equal(bestMetricCaveatLabel(CHAMPION_CAVEAT_SALVAGED), 'salvaged')
  assert.equal(bestMetricCaveatLabel(CHAMPION_CAVEAT_TRUST_FLAGGED), 'trust-flagged')
  assert.equal(bestMetricCaveatLabel(CHAMPION_CAVEAT_PARAMS_OVERRIDDEN), 'params overridden')
})

// ---------------------------------------------------------------------------------------------
// THE LEADERBOARD. A caveated run keeps its row, its value AND its rank — and says so.
// ---------------------------------------------------------------------------------------------

const LEADER_IS_CAVEATED = [
  run('v6', 0.727991),
  run('v9', 0.81, { best_metric_caveats: [CHAMPION_CAVEAT_SALVAGED] }),
  run('v2', 0.224975),
]

test('a caveated leader keeps its rank and the group refuses to be read as a clean win', () => {
  const group = crossRunGroups(LEADER_IS_CAVEATED).groups[0]
  assert.equal(group.outcome, 'ranked')
  assert.deepEqual(group.rows.map(r => [r.runId, r.rank]), [['v9', 1], ['v6', 2], ['v2', 3]],
    'the ordering is unchanged: this is the run the ENGINE crowned, on a rung its operator set')
  assert.deepEqual(group.rows.map(r => r.caveats),
    [[CHAMPION_CAVEAT_SALVAGED], [], []])
  assert.match(group.rows[0].caveatNotice, /NOT measured/)
  assert.equal(group.caveatedCount, 1)
  assert.equal(group.caveatedLeader, true)

  const { claim, refusals } = groupClaim(group)
  assert.match(claim, /v9 recorded the highest value/, 'the claim still names the leader')
  const caveat = refusals.find(line => /recorded a caveat about/.test(line))
  assert.ok(caveat, 'the refusal list must name it')
  assert.match(caveat, /1 run\(s\)/)
  assert.match(caveat, /salvaged, or from a trust-flagged node/)
  assert.match(caveat, /One of them leads this group\. Open it before reusing its configuration\./)
  // NARROWED, not deleted: the row still records nothing about WHICH ARTIFACT the number is about,
  // which is the half docs 31/35 measured, so that refusal keeps its example.
  assert.ok(refusals.some(line => /metric SUBJECT is not recorded on this row/.test(line)))
  assert.ok(refusals.some(line => /0\.224975 for a checkpoint neither of them trained/.test(line)))
})

test('a caveated also-ran is marked, and the group says it does not lead', () => {
  const group = crossRunGroups([
    run('v6', 0.9), run('v9', 0.1, { best_metric_caveats: [CHAMPION_CAVEAT_TRUST_FLAGGED] }),
  ]).groups[0]
  assert.equal(group.caveatedCount, 1)
  assert.equal(group.caveatedLeader, false)
  assert.match(groupClaim(group).refusals.find(l => /recorded a caveat about/.test(l)),
    /None of them leads this group\./)
})

test('NEGATIVE CONTROL: a measured group gains no marker, no count and no refusal', () => {
  const group = crossRunGroups([run('v6', 0.9), run('v2', 0.2)]).groups[0]
  assert.deepEqual(group.rows.map(r => r.caveats), [[], []])
  assert.deepEqual(group.rows.map(r => r.caveatNotice), ['', ''])
  assert.equal(group.caveatedCount, 0)
  assert.equal(group.caveatedLeader, false)
  assert.equal(groupClaim(group).refusals.filter(l => /recorded a caveat about/.test(l)).length, 0)
  // A LEGACY row that carries no such field at all is the same measured answer, never an unknown.
  const legacy = crossRunGroups([
    { run_id: 'old', task_id: 't', direction: 'max', best_metric: 1, nodes: 1, finished: true },
    { run_id: 'old2', task_id: 't', direction: 'max', best_metric: 2, nodes: 1, finished: true },
  ]).groups[0]
  assert.equal(legacy.caveatedCount, 0)
})

test('an unranked row still shows its caveat — the two rungs are independent', () => {
  const group = crossRunGroups([
    run('v6', 0.5),
    run('truncated', 0.99, {
      best_metric_caveats: [CHAMPION_CAVEAT_SALVAGED],
      source_integrity: { complete: false, good_records: 20, corrupt_line: 21, dropped_lines: 1603 },
    }),
  ]).groups[0]
  assert.deepEqual(group.unranked.map(r => [r.runId, r.rank, r.caveats]),
    [['truncated', null, [CHAMPION_CAVEAT_SALVAGED]]],
    'no rank because the log is a prefix; still caveated, because that is a different fact')
  assert.equal(group.caveatedCount, 1, 'counted over every entry, ranked or not')
  assert.equal(group.caveatedLeader, false, 'it holds no rank, so it leads nothing')
})
