// A MERGED CHAMPION HAS ITS OWN SENTENCE — the fifth caveat slug.
//
// `search/operators.py::merge_idea` returns `Idea(operator='merge')` carrying the ARITHMETIC MEAN of
// its two parents' params, and the node trains nothing of its own: it averages their weights and
// scores the average. So `params_overridden` — "ships code that assigns a different value to a
// parameter its own experiment record declares" — is false in both halves for such a node, while
// the real problem is sharper: the number is filed at coordinates NO configuration ever occupied.
//
// Measured over every event log on this box (2026-08-29): 7 champions, exactly ONE is a merge
// (`e5small-dr-unified-v4` node 13, 0.793411, the second-best number here) and it was ALREADY
// caveated — so the engine-side swap re-labels one champion and newly caveats none.
//
// This file guards the half a Python test cannot see: that the browser has a real sentence for the
// new slug. `bestMetricCaveatNotice` falls through to "a caveat this view has no sentence for", by
// design, so a server that starts emitting a word the client does not know REPUBLISHES the number
// under a placeholder — which is exactly what shipping the engine half alone would have done.
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CHAMPION_CAVEAT_MERGED_COORDINATES, CHAMPION_CAVEAT_PARAMS_OVERRIDDEN,
  bestMetricCaveatLabel, bestMetricCaveatNotice, bestMetricCaveats, bestMetricCaveated,
} from '../src/runIndex.js'

const merged = { best_metric_caveats: [CHAMPION_CAVEAT_MERGED_COORDINATES] }

test('the merged-coordinates slug has a real sentence, not the unknown-slug fallback', () => {
  const notice = bestMetricCaveatNotice(merged)
  assert.ok(!notice.includes('has no sentence for'),
    'shipping the engine half without this one republishes the champion under a placeholder — the '
    + 'fallback is deliberate and is exactly what must NOT fire for a slug we emit')
  assert.ok(notice.includes('MEAN-MERGE'),
    'and it must name the mechanism: the operator has to know the coordinates are an average')
})

test('the sentence says the number was still SELECTED on', () => {
  assert.ok(/selected on it/i.test(bestMetricCaveatNotice(merged)),
    'every caveat sentence in this family says the run selected on the number anyway — the '
    + 'operator’s question is "may I reuse this configuration", not "is this run flagged"')
})

test('it does NOT claim the code contradicts a declaration', () => {
  const notice = bestMetricCaveatNotice(merged)
  assert.ok(!/assigns a different value/.test(notice),
    'that is `params_overridden`’s sentence and its premise is absent here: a merged Idea declares '
    + 'nothing, so borrowing the wording would republish the spurious config citation in prose')
})

test('the short label is spelled, not echoed raw', () => {
  assert.equal(bestMetricCaveatLabel(CHAMPION_CAVEAT_MERGED_COORDINATES), 'merged coordinates',
    'an unregistered slug renders as itself; a table cell reading `merged_coordinates` is the '
    + 'client admitting it does not know the word')
})

test('a merged champion still reads as caveated', () => {
  assert.deepEqual(bestMetricCaveats(merged), [CHAMPION_CAVEAT_MERGED_COORDINATES])
  assert.equal(bestMetricCaveated(merged), true,
    'the whole point of swapping the slug rather than suppressing it is that the number never goes '
    + 'from caveated to clean')
})

test('params_overridden keeps its own distinct sentence', () => {
  const other = bestMetricCaveatNotice({ best_metric_caveats: [CHAMPION_CAVEAT_PARAMS_OVERRIDDEN] })
  assert.ok(/assigns a different value/.test(other) && !/MEAN-MERGE/.test(other),
    'the two must not converge: one is about a file disagreeing with a declaration, the other '
    + 'about there being no declaration at all')
})
