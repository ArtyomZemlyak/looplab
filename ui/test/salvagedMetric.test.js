// A node whose experiment SUCCEEDED must not be accused of a constraint violation.
//
// Metric salvage recovers a metric the eval already produced from a node that failed for some other
// reason — v5 node 0 trained 76 minutes, printed `RECALL@100: 0.743250`, and was discarded because
// its manifest named the checkpoint one directory over. The recovered value rides on the
// `violations` list, because `feasible = not violations` is what keeps an unmeasured number out of
// champion selection, and that exclusion is correct.
//
// But the list is also what the UI reads to say "Constraint violation", and it renders each row as
// `name | value | bound`. A salvaged metric has no bound, so the row came out
// `metric_salvaged | (blank) | (blank)` under a heading accusing a node that had done nothing
// wrong. The exclusion is real; the accusation is not, and they are separable.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { isSalvagedMetricViolation, nodeFeasibilityStatus } from '../src/trustSemantics.js'

const salvaged = (extra = {}) => ({
  status: 'evaluated', metric: 0.74325, feasible: false,
  violations: [{ name: 'metric_salvaged',
    salvage: { source: 'declared_reader', reader: 'stdout_regex', stage: 'train' } }],
  ...extra,
})

test('a salvaged node reads as salvaged, not as a violation', () => {
  const status = nodeFeasibilityStatus(salvaged())
  assert.equal(status.tone, 'warn', 'alarm is for a result that broke a constraint')
  assert.equal(status.label, 'Metric salvaged, not measured')
  assert.match(status.detail, /recovered it with its own declared reader/)
  assert.match(status.detail, /stage “train”/, 'name where it failed — that is the actionable part')
  assert.match(status.detail, /excluded from winner selection/, 'the exclusion is still stated')
})

test('a real constraint violation is untouched', () => {
  const status = nodeFeasibilityStatus(
    { status: 'evaluated', feasible: false, violations: [{ name: 'latency_ms', value: 900, max: 500 }] })
  assert.equal(status.tone, 'alarm')
  assert.equal(status.label, 'Constraint violation')
})

test('a salvaged node that ALSO broke a constraint is a violation', () => {
  // "Every violation is a salvage" is the condition, not "any". A node that genuinely broke a bound
  // must not be softened because its metric also happened to be recovered.
  const status = nodeFeasibilityStatus(salvaged({
    violations: [{ name: 'metric_salvaged', salvage: {} }, { name: 'latency_ms', value: 900, max: 500 }],
  }))
  assert.equal(status.tone, 'alarm')
  assert.equal(status.label, 'Constraint violation')
})

test('an ordinary feasible node is unchanged', () => {
  const ok = nodeFeasibilityStatus({ status: 'evaluated', feasible: true, violations: [] })
  assert.equal(ok.tone, 'ok')
  assert.equal(nodeFeasibilityStatus({ status: 'running' }).tone, 'unknown')
  assert.equal(nodeFeasibilityStatus(null).tone, 'unknown')
})

test('the predicate is exact — a lookalike name is not a salvage', () => {
  assert.equal(isSalvagedMetricViolation({ name: 'metric_salvaged' }), true)
  assert.equal(isSalvagedMetricViolation({ name: 'metric_salvaged_v2' }), false)
  assert.equal(isSalvagedMetricViolation({ name: 'latency_ms' }), false)
  assert.equal(isSalvagedMetricViolation(null), false)
})

test('the table gives the salvage row its own shape instead of two blank cells', () => {
  const source = readFileSync(new URL('../src/Inspector.jsx', import.meta.url), 'utf8')
  assert.ok(source.includes('isSalvagedMetricViolation(v)'),
    'the violations table must branch on the salvage row')
  assert.ok(!source.includes('caption="Constraint violations"'),
    'the heading accused every row in the table, including the one that broke nothing')
  assert.ok(source.includes('caption="Feasibility record"'))
  assert.ok(source.includes('recovered by'), 'the row must say where the number came from')
})

test('the UI name matches the engine name exactly', () => {
  // The violation name is a wire contract between `engine/metric_salvage.py` and this file. A
  // rename on either side would silently restore the accusing row.
  const engine = readFileSync(
    new URL('../../looplab/engine/metric_salvage.py', import.meta.url), 'utf8')
  assert.ok(engine.includes('"metric_salvaged"'),
    'the engine no longer emits the violation name this UI branches on')
})

test('under `select` the salvage is still visible — the rung with no violation row', () => {
  // Everything above hangs off the violation ROW, and `metric_salvage: "select"` is precisely the
  // rung that has none: the node is feasible, competes for champion, and read as a plain
  // "Feasible" with nothing anywhere saying its number was recovered rather than measured. The
  // operator who opted IN was the one shown least. `metric_provenance` is folded (a real field on
  // the node, served with it) — the commit's own "a provenance field alone would be 'can tell' and
  // not 'does'" argument applied to its own permissive rung.
  const admitted = {
    status: 'evaluated', metric: 0.74325, feasible: true, violations: [],
    metric_provenance: { salvaged: true, source: 'declared_reader', stage: 'train',
      producer: 'operator_stage' },
  }
  const status = nodeFeasibilityStatus(admitted)
  assert.equal(status.tone, 'warn', 'it competes, but it is not a measured result')
  assert.equal(status.label, 'Metric salvaged, admitted for selection')
  assert.match(status.detail, /stage “train”/)
  assert.match(status.detail, /competes for champion/)
  // A measured node is untouched by the new read.
  assert.equal(nodeFeasibilityStatus({ status: 'evaluated', feasible: true, violations: [],
    metric_provenance: null }).tone, 'ok')
})

test('a node excluded for a real bound says the metric was salvaged too, if it was', () => {
  const status = nodeFeasibilityStatus({
    status: 'evaluated', feasible: false,
    violations: [{ name: 'latency_ms', value: 900, max: 500 }],
    metric_provenance: { salvaged: true, source: 'declared_reader' },
  })
  assert.equal(status.label, 'Constraint violation')
  assert.match(status.detail, /SALVAGED rather than measured/)
})
