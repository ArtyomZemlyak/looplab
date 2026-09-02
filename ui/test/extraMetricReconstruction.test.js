// A RECONSTRUCTION MUST NOT RENDER AS A MEASUREMENT.
//
// `maintenance/backfill_score_metrics.py` recovers objectives the score stage printed and the run
// threw away, and writes them through the `declared` channel — correctly, since the operator's own
// scoring program printed them. Until the fold carried a marker beside them, that was the whole
// record: `extraMetricIsDeclared` answered true and a value recovered from a log after the fact
// rendered exactly like one measured while the run was happening.
//
// The concrete harm is a TIE. The recovered suite is printed to TWO decimals while the objective is
// read at six, so `e5small-dr-unified-v4` nodes 0 and 1 — which differ by 0.006 on recall@100 —
// are identical on every recovered metric. "These two nodes are equal on nDCG" and "the print
// statement cannot tell them apart" are different claims.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  EXTRA_METRIC_RECONSTRUCTED_LABEL,
  extraMetricCaveated, extraMetricChannel, extraMetricIsBackfilled, extraMetricIsDeclared,
  extraMetricPrecision, extraMetricSourceHelp, extraMetricSourceLabel,
} from '../src/extraMetrics.js'

const live = { extra_metrics: { 'ndcg@100': 0.46 }, extra_metrics_provenance: { 'ndcg@100': 'declared' } }
const recovered = {
  ...live,
  extra_metrics_backfill: { backfilled: true, backfilled_at: 1.0, precision_decimals: { 'ndcg@100': 2 } },
}

test('absent marker means measured, never unknown', () => {
  assert.equal(extraMetricIsBackfilled(live), false)
  assert.equal(extraMetricIsBackfilled({}), false)
  assert.equal(extraMetricIsBackfilled(null), false)
  assert.equal(extraMetricIsBackfilled(undefined), false)
  // The safe direction here is the OPPOSITE of the channel map's: an absent channel must not read
  // as the guarded one, but an absent backfill marker means the fold never applied a
  // reconstruction — which no log written before the tool existed can contradict.
  assert.equal(extraMetricChannel(live, 'ndcg@100'), 'declared')
})

test('the channel stays declared and the reconstruction is a SECOND fact', () => {
  // Not a fourth channel value: `auto` ("the candidate scraped its own stdout") and `engine`
  // ("LoopLab wrote the print statement") are both false about a recovered value.
  assert.equal(extraMetricChannel(recovered, 'ndcg@100'), 'declared')
  assert.equal(extraMetricIsDeclared(recovered, 'ndcg@100'), true)
  assert.equal(extraMetricIsBackfilled(recovered), true)
})

test('a reconstruction is CAVEATED even though its channel is the guarded one', () => {
  // MUTATION: key the source cell on the channel alone -> a value recovered from a log renders
  // unmarked beside one measured live, which is the whole defect.
  assert.equal(extraMetricCaveated(live, 'ndcg@100'), false)
  assert.equal(extraMetricCaveated(recovered, 'ndcg@100'), true)
})

test('the label says both facts and the help says the precision', () => {
  assert.equal(extraMetricSourceLabel(live, 'ndcg@100'), 'declared')
  assert.equal(extraMetricSourceLabel(recovered, 'ndcg@100'),
    `declared · ${EXTRA_METRIC_RECONSTRUCTED_LABEL}`)
  assert.match(extraMetricSourceHelp(recovered, 'ndcg@100'), /2 decimal place/)
  assert.doesNotMatch(extraMetricSourceHelp(live, 'ndcg@100'), /decimal place/)
})

test('precision is null when nobody wrote it down, which is not full precision', () => {
  assert.equal(extraMetricPrecision(live, 'ndcg@100'), null)
  assert.equal(extraMetricPrecision(recovered, 'ndcg@100'), 2)
  assert.equal(extraMetricPrecision(recovered, 'map@100'), null)   // recovered, but unrecorded
  assert.equal(extraMetricPrecision({ extra_metrics_backfill: { backfilled: true } }, 'x'), null)
})

test('a malformed marker is not a claim', () => {
  // Hand-edited logs and old rows reach these readers with assignment validation off.
  for (const junk of [{ backfilled: false }, { backfilled: true, precision_decimals: [] },
    [], 'yes', 0]) {
    const node = { ...live, extra_metrics_backfill: junk }
    assert.equal(extraMetricPrecision(node, 'ndcg@100'), null, JSON.stringify(junk))
  }
  assert.equal(extraMetricIsBackfilled({ ...live, extra_metrics_backfill: { backfilled: false } }), false)
  assert.equal(extraMetricIsBackfilled({ ...live, extra_metrics_backfill: [] }), false)
})

test('two recovered nodes that render identically are still marked', () => {
  // v4 nodes 0 and 1: equal on every recovered row ONLY because the print statement cannot separate
  // them. Whatever a surface does with the tie, it must not present it as measured.
  const a = { ...recovered }
  const b = { ...recovered }
  assert.equal(a.extra_metrics['ndcg@100'], b.extra_metrics['ndcg@100'])
  assert.ok(extraMetricIsBackfilled(a) && extraMetricIsBackfilled(b))
})
