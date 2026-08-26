// The declared-vs-applied divergence must REACH the operator, not just the wire.
//
// THE DEFECT. `serve/run_projections.py` publishes `best_metric_caveats` and `RunList.jsx` renders
// each slug as a chip, so the run row says `params_overridden` — a word — and stops. The node
// payload has carried the whole answer all along: `metric_provenance.applied_params.diverged`, one
// row per knob with `param`, `declared`, `applied`, `file`, `line` and `match`. Nothing in `ui/src`
// read it, while the SAME record's other members were read two files over
// (`runIndex.js::nodeComparabilityRecord` takes `.comparability`, `trustSemantics.js` takes
// `.salvaged`). #50 fixed the card pane; it did not fix this.
//
// WHY IT MATTERS: re-derived 2026-08-26 over all 45 event logs, 3 of the 42 champions are caveated
// and all three are `params_overridden` — including this box's best number (e5small-dr-unified-v2
// node 1, 0.793426) and its second best (v4 node 13, 0.793411). `champion_caveats.py` says the
// operator's question is "may I reuse this configuration". A slug cannot answer it.
//
// The fixture below is the REAL v4 node 13 record, read out of its event log, not invented.
// Every assertion has an input that makes it FAIL; the mutations are named in the messages.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  nodeAppliedParams, appliedParamsDivergences, appliedParamsChecked,
  appliedParamsUnsettled, appliedParamsNotice,
} from '../src/runIndex.js'

// Verbatim from runs/e5small-dr-unified-v4 node 13 (metric 0.793411), the run's champion.
const V4_NODE_13 = {
  metric_provenance: {
    applied_params: {
      authority: 'committed',
      checked: 4,
      diverged: [
        { param: 'train.training.batch_size', declared: 4096.0, applied: 2048.0,
          file: 'vectorsearch/configs/config.yaml', line: 265, match: 'exact' },
        { param: 'train.training.learning_rate', declared: 0.001, applied: 0.0005,
          file: 'vectorsearch/configs/config.yaml', line: 267, match: 'exact' },
        { param: 'train.training.n_epochs', declared: 3.0, applied: 1.0,
          file: 'vectorsearch/configs/config.yaml', line: 264, match: 'exact' },
      ],
      unresolved: { 'loss.dcl_threshold': 'absent', 'loss.rdrop_alpha': 'absent' },
    },
  },
}

test('the champion’s three diverged knobs are all named, in the engine’s order', () => {
  const rows = appliedParamsDivergences(nodeAppliedParams(V4_NODE_13))

  assert.equal(rows.length, 3, 'MUTATION: return [] — the operator is back to a bare slug')
  assert.deepEqual(rows.map(r => r.param), [
    'train.training.batch_size', 'train.training.learning_rate', 'train.training.n_epochs',
  ], 'MUTATION: sort the rows — the engine’s order is the record’s order')
  assert.equal(rows[0].declared, 4096)
  assert.equal(rows[0].applied, 2048)
  assert.equal(rows[1].declared, 0.001)
  assert.equal(rows[1].applied, 0.0005)
  assert.equal(rows[2].declared, 3)
  assert.equal(rows[2].applied, 1)
  assert.equal(rows[0].file, 'vectorsearch/configs/config.yaml')
  assert.equal(rows[0].line, 265)
})

test('a falsy declared or applied value survives — 0 and false are real coordinates', () => {
  // MUTATION: `declared: row.declared || null` — this is the whole reason the passthrough is bare.
  const rows = appliedParamsDivergences({
    diverged: [
      { param: 'loss.use_rdrop', declared: true, applied: false },
      { param: 'train.warmup_steps', declared: 500, applied: 0 },
    ],
  })

  assert.equal(rows[0].applied, false, 'a `false` that ran must not become null')
  assert.equal(rows[1].applied, 0, 'a `0` that ran must not become null')
  assert.equal(rows[1].declared, 500)
})

test('an unnamed row is dropped rather than rendered as an anonymous divergence', () => {
  // MUTATION: drop the `param` filter -> length 3, and two rows render with no knob name.
  const rows = appliedParamsDivergences({
    diverged: [
      { param: 'train.training.batch_size', declared: 8192, applied: 4096 },
      { declared: 1, applied: 2 },
      { param: '   ', declared: 1, applied: 2 },
    ],
  })

  assert.equal(rows.length, 1)
  assert.equal(rows[0].param, 'train.training.batch_size')
})

test('`checked` is null when absent and never 0 — the vacuous-green rule', () => {
  // MUTATION: `return Number(record?.checked) || 0` -> both absent cases answer 0, which publishes
  // "nothing was looked at" as "everything agreed".
  assert.equal(appliedParamsChecked({ checked: 4 }), 4)
  assert.equal(appliedParamsChecked({ checked: 0 }), 0, 'a real 0 must survive as 0')
  assert.equal(appliedParamsChecked({}), null, 'ABSENT is null, not 0')
  assert.equal(appliedParamsChecked({ checked: 'four' }), null)
  assert.equal(appliedParamsChecked(null), null)
})

test('unresolved and conflicted coordinates are NOT divergences', () => {
  // MUTATION: fold `unresolved` into `appliedParamsDivergences` -> the node is convicted of two
  // changes nobody made. v4 node 13 really carries these two unresolved keys.
  const record = nodeAppliedParams(V4_NODE_13)

  assert.equal(appliedParamsDivergences(record).length, 3, 'exactly the diverged rows')
  assert.deepEqual(appliedParamsUnsettled(record), { unresolved: 2, conflicts: 0 })
  // `unresolved` is a MAP and `conflicts` a LIST that is absent when empty — each in its own shape.
  assert.deepEqual(appliedParamsUnsettled({ conflicts: [{ param: 'x' }] }),
    { unresolved: 0, conflicts: 1 })
  assert.deepEqual(appliedParamsUnsettled({}), { unresolved: 0, conflicts: 0 })
})

test('a node with no record makes NO claim', () => {
  // MUTATION: return `{}` instead of null -> a node that was never checked renders a clean
  // "0 diverged", which is the vacuous green.
  assert.equal(nodeAppliedParams({}), null)
  assert.equal(nodeAppliedParams({ metric_provenance: null }), null)
  assert.equal(nodeAppliedParams({ metric_provenance: 'salvaged' }), null)
  assert.equal(nodeAppliedParams({ metric_provenance: [] }), null)
  assert.equal(nodeAppliedParams({ metric_provenance: { applied_params: [] } }), null)
  assert.equal(nodeAppliedParams({ metric_provenance: { applied_params: 'x' } }), null)
  assert.equal(appliedParamsDivergences(null).length, 0)
  assert.equal(appliedParamsNotice(null), '')
})

test('the notice names the count and the scope, and never accuses', () => {
  const notice = appliedParamsNotice(nodeAppliedParams(V4_NODE_13))

  assert.match(notice, /3 declared coordinates of 4 checked/,
    'MUTATION: drop `checked` from the sentence -> "3 diverged" with no denominator')
  assert.match(notice, /still ran and its number still counts/,
    'MUTATION: reword to accuse -> the Developer deviating is legitimate and documented')
  assert.equal(appliedParamsNotice({ diverged: [], checked: 9 }), '',
    'a node where everything agreed says nothing here')
  // Singular, and a record with no `checked` states the count without inventing a denominator.
  const one = appliedParamsNotice({ diverged: [{ param: 'a', declared: 1, applied: 2 }] })
  assert.match(one, /1 declared coordinate was not what ran/)
  assert.doesNotMatch(one, /of 0 checked/, 'MUTATION: `checked || 0` -> invents "of 0 checked"')
})
