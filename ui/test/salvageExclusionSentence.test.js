// THE SALVAGE TOOLTIP PRESCRIBED A SETTING THAT WAS ALREADY SET.
//
// `objectiveMetricSource` derives `admitted` as `violations.length === 0` — "the record minted NO
// row" — which is correct and deliberate: any row at all keeps a node out of `feasible_nodes`, so
// deriving admission from the rows rather than from `feasible` is what stops a node excluded for a
// DIFFERENT reason from being told it competes for champion.
//
// But the tooltip's not-admitted branch then attributed the exclusion to the salvage rung whatever
// the row actually was. `metric_salvage: "select"` over operator-produced output is precisely the
// rung that mints NO salvage row, so such a node enters the salvage branch through
// `metric_provenance` alone — and if it also carries any other violation (a breached constraint
// bound), it read: "It is excluded from winner selection until metric_salvage is set to `select`."
// That setting is already `select`. The operator was sent to change something that would do
// nothing, and the real exclusion went unnamed.
//
// `salvageRow` is the discriminator and the only one available: a salvage-PROPER row IS the salvage
// rung's own exclusion receipt, so its absence beside a live provenance means that rung already let
// the node through.
import assert from 'node:assert/strict'
import test from 'node:test'

import { objectiveMetricSource, objectiveSourceHelp } from '../src/trustSemantics.js'

const SALVAGE_ROW = {
  // `isSalvagedMetricViolation` keys on `name`. An earlier cut of this file wrote `kind` and every
  // salvage-row case silently took the provenance-only branch — a fixture that tests the wrong
  // state while looking like it tests the right one.
  name: 'metric_salvaged',
  salvage: { stage: 'score', condition: 'artifact_contract' },
}
const OTHER_ROW = { name: 'constraint_breached', detail: 'latency bound exceeded' }
const PROVENANCE = { salvaged: true, stage: 'score', condition: 'artifact_contract' }

test('salvaged with NO rows at all: admitted, and it says so', () => {
  const src = objectiveMetricSource({ violations: [], metric_provenance: PROVENANCE })
  assert.equal(src.channel, 'salvaged')
  assert.equal(src.admitted, true)
  assert.match(objectiveSourceHelp(src), /competes for champion/)
})

test('salvaged by a PROPER ROW: the salvage rung really is the exclusion', () => {
  const src = objectiveMetricSource({ violations: [SALVAGE_ROW] })
  assert.equal(src.channel, 'salvaged')
  assert.equal(src.admitted, false)
  assert.equal(src.salvageRow, true)
  assert.match(objectiveSourceHelp(src), /until metric_salvage is set/,
    'this is the one case where that prescription is the right one')
})

test('salvaged by PROVENANCE ALONE but excluded for something else: the defect', () => {
  // metric_salvage is ALREADY "select" — that rung mints no row — and the node is excluded by a
  // different violation entirely.
  const src = objectiveMetricSource({ violations: [OTHER_ROW], metric_provenance: PROVENANCE })
  assert.equal(src.channel, 'salvaged')
  assert.equal(src.admitted, false, 'a row exists, so it is not admitted')
  assert.equal(src.salvageRow, false, 'but the salvage rung minted none')
  const help = objectiveSourceHelp(src)
  assert.ok(!/until metric_salvage is set/.test(help),
    'prescribing a setting that is already applied sends the operator to change nothing while the '
    + 'real exclusion goes unnamed')
  assert.match(help, /DIFFERENT recorded violation/,
    'and it must say the exclusion belongs to something else')
})

test('a proper row BESIDE another violation still blames the salvage rung', () => {
  // Both rows present: the salvage rung DID mint its own exclusion, so its sentence is honest even
  // though another violation also excludes.
  const src = objectiveMetricSource({ violations: [SALVAGE_ROW, OTHER_ROW] })
  assert.equal(src.salvageRow, true)
  assert.match(objectiveSourceHelp(src), /until metric_salvage is set/)
})

test('the stage still rides on every salvage sentence', () => {
  const src = objectiveMetricSource({ violations: [OTHER_ROW], metric_provenance: PROVENANCE })
  assert.match(objectiveSourceHelp(src), /stage \u201cscore\u201d/,
    'the stage that failed its contract is the one fact all three states share')
})
