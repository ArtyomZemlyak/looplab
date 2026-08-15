// THE METRICS TAB LABELLED A NUMBER `measured` THAT THE ENGINE REFUSES TO CALL MEASURED.
//
// `Inspector.jsx::Metrics` built the objective ★ row with a hardcoded `measured` and the tooltip
// "read by the operator's own metric spec on the protected score stage". That sentence is true of a
// clean node and false of three states the record already distinguishes:
//
//   * a `metric_salvaged` violation row — the eval FAILED and the run recovered the number with its
//     own declared reader, which `trustSemantics.nodeFeasibilityStatus` says on the Trust tab as
//     "Metric salvaged, not measured";
//   * the same row with `salvage.condition === "metric_subject_unbound"` — the eval SUCCEEDED and
//     nothing records which artifact the number is about;
//   * `metric_salvage: "select"`, the rung with NO violation row at all, where a salvaged number
//     COMPETES FOR CHAMPION and the only record is the folded `metric_provenance`.
//
// The salvage disclosure existed on a DIFFERENT TAB. So the tab an operator opens to read numbers
// flattened exactly the distinction the whole salvage/subject vocabulary exists to draw — the one
// that separates `runs/rubertlite-dr-unified-v6` node 4's recorded 0.225 (a human's checkpoint an
// absolute path in an editable config pointed at) from a number measured on what a node produced.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import {
  OBJECTIVE_SOURCE_HELP, OBJECTIVE_SOURCE_LABEL, OBJECTIVE_MEASURED, OBJECTIVE_SALVAGED,
  OBJECTIVE_SUBJECT_UNBOUND, objectiveMetricSource, objectiveSourceHelp,
} from '../src/trustSemantics.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// ---------------------------------------------------------------------------------------------
// The MODEL — the decision, driven directly. It used to be an inline `r.star ? … : …` in JSX,
// which is why nothing could reach it.
// ---------------------------------------------------------------------------------------------

const salvageRow = (salvage) => ({ name: 'metric_salvaged', value: 0.74325, max: null, min: null, salvage })

const salvaged = (extra = {}) => ({
  status: 'evaluated', metric: 0.74325, feasible: false,
  violations: [salvageRow({ salvaged: true, condition: 'stage_failed', source: 'declared_reader',
    reader: 'stdout_regex', stage: 'score', producer: 'agent_stage' })],
  ...extra,
})

const unbound = (reason = 'not_declared') => ({
  status: 'evaluated', metric: 0.74325, feasible: false,
  violations: [salvageRow({ salvaged: false, condition: 'metric_subject_unbound',
    unbound_reason: reason, detail: 'this metric names no SUBJECT' })],
})

const admitted = () => ({
  status: 'evaluated', metric: 0.74325, feasible: true, violations: [],
  metric_provenance: { salvaged: true, condition: 'artifact_contract', source: 'declared_reader',
    reader: 'stdout_regex', stage: 'score', producer: 'operator_stage' },
})

const measured = (extra = {}) => ({
  status: 'evaluated', metric: 0.74325, feasible: true, violations: [], ...extra,
})

test('a salvaged objective is not called measured', () => {
  const source = objectiveMetricSource(salvaged())
  assert.equal(source.channel, OBJECTIVE_SALVAGED)
  assert.equal(OBJECTIVE_SOURCE_LABEL[source.channel], 'salvaged')
  assert.match(objectiveSourceHelp(source), /recovered/)
  assert.match(objectiveSourceHelp(source), /NOT measured/)
  assert.match(objectiveSourceHelp(source), /stage “score”/, 'name where it failed')
  assert.match(objectiveSourceHelp(source), /excluded from winner selection/)
})

test('a salvaged objective ADMITTED for selection still reads salvaged', () => {
  // `select` removes the EXCLUSION, not the fact. This is the rung with no violation row — the one
  // where the number competes for champion, i.e. where mislabelling it costs the most.
  const source = objectiveMetricSource(admitted())
  assert.equal(source.channel, OBJECTIVE_SALVAGED)
  assert.match(objectiveSourceHelp(source), /competes for champion/)
  assert.doesNotMatch(objectiveSourceHelp(source), /excluded from winner selection/)
})

test('an unbound subject is NOT the same word as salvaged', () => {
  // Nothing failed and nothing was recovered: the number WAS measured by the protected scoring
  // path. What is missing is any record of what it is about. Calling that "salvaged" is a false
  // accusation one condition over — the exact trap `trustSemantics.js` documents.
  const source = objectiveMetricSource(unbound())
  assert.equal(source.channel, OBJECTIVE_SUBJECT_UNBOUND)
  assert.notEqual(OBJECTIVE_SOURCE_LABEL[OBJECTIVE_SUBJECT_UNBOUND],
    OBJECTIVE_SOURCE_LABEL[OBJECTIVE_SALVAGED])
  assert.equal(OBJECTIVE_SOURCE_LABEL[source.channel], 'measured, no subject')
  const help = objectiveSourceHelp(source)
  assert.match(help, /was measured/, 'the measurement is not in doubt — the referent is')
  assert.doesNotMatch(help, /recovered/)
  assert.match(help, /declares no eval\.metric\.subject/, 'the reason names the fix')
})

test('each unbound reason names its own fix', () => {
  assert.match(objectiveSourceHelp(objectiveMetricSource(unbound('missing'))),
    /the declared subject is missing/)
  assert.match(objectiveSourceHelp(objectiveMetricSource(unbound('stale'))),
    /the declared subject is stale/)
})

test('salvage outranks an unbound row when a node carries both', () => {
  // Same precedence `nodeFeasibilityStatus` applies, and for the same reason: "the number was never
  // measured" is the stronger statement, so it must not be softened into "measured, no subject".
  const both = salvaged()
  both.violations = [...both.violations, ...unbound().violations]
  assert.equal(objectiveMetricSource(both).channel, OBJECTIVE_SALVAGED)
})

test('a real constraint violation does not change what the NUMBER is', () => {
  // The two rows answer different questions and the split is deliberate: feasibility asks "why is
  // this node excluded" (an EVERY-row question — one clean row does not clear it), the source asks
  // "what is this number" (an ANY-row question). A latency bound was measured and breached; the
  // metric beside it was measured too.
  const bounded = measured({ feasible: false,
    violations: [{ name: 'latency_ms', value: 900, max: 500 }] })
  assert.equal(objectiveMetricSource(bounded).channel, OBJECTIVE_MEASURED)
  // …but a salvaged number under a breached bound is still salvaged.
  const both = measured({ feasible: false,
    violations: [{ name: 'latency_ms', value: 900, max: 500 },
      ...salvaged().violations] })
  assert.equal(objectiveMetricSource(both).channel, OBJECTIVE_SALVAGED)
})

test('a repaired DECLARATION reads measured, and says so', () => {
  // `declaration_repaired` is `metric_provenance` for a MEASURED metric: the contract was re-checked
  // and PASSED, so `salvaged: False` is spelled out and the engine's own docstring says a False here
  // "reads as measured everywhere". The label must not soften that — but the tooltip carries the one
  // thing this record uniquely proves, that the node's recorded code is not byte-for-byte what
  // produced its recorded metric.
  const repaired = measured({ metric_provenance: { salvaged: false, declaration_repaired: true,
    stage: 'train', reader: 'stdout_regex', producer: 'operator_stage' } })
  const source = objectiveMetricSource(repaired)
  assert.equal(source.channel, OBJECTIVE_MEASURED)
  assert.match(objectiveSourceHelp(source), /declaration was repaired/)
})

test('the negative control — a genuinely measured objective is untouched', () => {
  const source = objectiveMetricSource(measured())
  assert.equal(source.channel, OBJECTIVE_MEASURED)
  assert.equal(OBJECTIVE_SOURCE_LABEL[source.channel], 'measured')
  assert.equal(objectiveSourceHelp(source), OBJECTIVE_SOURCE_HELP[OBJECTIVE_MEASURED])
  assert.match(objectiveSourceHelp(source), /protected score stage/,
    'the historical sentence survives for the node it was always true about')
})

test('the model is TOTAL over junk — a hand-edited log never invents the guarded answer', () => {
  for (const junk of [null, undefined, {}, { violations: null }, { violations: 'nope' },
    { violations: ['metric_salvaged'] }, { metric_provenance: 'salvaged' },
    { metric_provenance: { salvaged: false } }]) {
    assert.equal(objectiveMetricSource(junk).channel, OBJECTIVE_MEASURED,
      `${JSON.stringify(junk)} must not resolve to a caveat nobody recorded`)
  }
  // …and a lookalike condition slug is not the unbound row.
  assert.equal(objectiveMetricSource({ violations: [salvageRow(
    { salvaged: true, condition: 'metric_subject_unbound_v2' })] }).channel, OBJECTIVE_SALVAGED)
})

// ---------------------------------------------------------------------------------------------
// The RENDER — the same four records driven through the real component, because the model being
// right is not the property the operator has. Nothing else in the suite mounts this file.
// ---------------------------------------------------------------------------------------------

test('the Metrics tab renders the label and the tooltip the record supports', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { Metrics } = await vite.ssrLoadModule('/src/Inspector.jsx')
    const draw = (node) => renderToStaticMarkup(React.createElement(Metrics, {
      n: { id: 3, ...node }, detail: {}, runId: 'demo',
      state: { nodes: { 3: { id: 3, ...node } }, best_node_id: 3 },
    }))

    const salvagedHtml = draw(salvaged())
    assert.match(salvagedHtml, />salvaged</, 'the objective cell must not say `measured`')
    assert.doesNotMatch(salvagedHtml, /title="The run&#x27;s objective: read by/)
    assert.match(salvagedHtml, /recovered/)

    const unboundHtml = draw(unbound())
    assert.match(unboundHtml, />measured, no subject</)
    assert.match(unboundHtml, /eval\.metric\.subject/)
    assert.doesNotMatch(unboundHtml, /salvaged/,
      'nothing failed and nothing was recovered — the accusation must not appear anywhere')

    const admittedHtml = draw(admitted())
    assert.match(admittedHtml, />salvaged</)
    assert.match(admittedHtml, /competes for champion/)

    // The negative control, rendered: a clean node keeps the word and the sentence it always had.
    const cleanHtml = draw(measured())
    assert.match(cleanHtml, />measured</)
    assert.match(cleanHtml, /protected score stage/)
    assert.doesNotMatch(cleanHtml, /salvaged/)
    assert.doesNotMatch(cleanHtml, /no subject/)

    // The `best #N` column is a DIFFERENT node's record and gets its own read. A measured node
    // compared against a SALVAGED champion must not leave the champion's number unmarked — under
    // `select` the champion is exactly the node that can be salvaged.
    const versusSalvagedChamp = renderToStaticMarkup(React.createElement(Metrics, {
      n: { id: 3, ...measured() }, detail: {}, runId: 'demo',
      state: { nodes: { 3: { id: 3, ...measured() }, 4: { id: 4, ...admitted() } }, best_node_id: 4 },
    }))
    assert.match(versusSalvagedChamp, />measured</, "this node's own row is unchanged")
    assert.match(versusSalvagedChamp, /· salvaged/, "the champion's number carries its own record")
    assert.match(versusSalvagedChamp, /competes for champion/)

    // A caveated objective is marked the way the caveated EXTRAS beside it already are — the row
    // must not be the only one whose warning is invisible.
    for (const html of [salvagedHtml, unboundHtml, admittedHtml]) {
      assert.match(html, /class="warn"/)
    }
    assert.doesNotMatch(cleanHtml, /class="warn"/)
  } finally {
    await vite.close()
  }
})
