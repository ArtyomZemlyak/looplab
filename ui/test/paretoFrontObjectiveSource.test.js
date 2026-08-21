// THE PARETO FRONT RANKED A SALVAGED NUMBER BESIDE A MEASURED ONE, UNMARKED.
//
// `panels.jsx::ParetoPanel` populated its front with `paretoMetric(n) != null && n.feasible !== false`
// and printed the value bare. That filter is right about the `audit` rung — a salvage row makes the
// node infeasible and it never reaches the panel — and blind to the one that matters:
// `metric_salvage: "select"` mints NO violation row, so a node whose metric the run RECOVERED from a
// failed eval arrives feasible, competes for champion, and was rendered exactly like a measurement.
// Meanwhile `Inspector.jsx::Metrics` had, since 2026-08-15, labelled that same number `salvaged`.
// Two surfaces, one record, opposite stories — and the one WITHOUT the caveat is the one an operator
// reads to decide which configuration to reuse.
//
// A Pareto front does not merely rank, which is why this was worse than a missing word: the caveated
// point DOMINATES measured nodes off the table. Driven below, a run with a measured 0.51 and a
// salvage-admitted 0.81 rendered a single crowned row and the measured result nowhere at all.
//
// The decision is argued at `panels.jsx::ParetoPanel`: CAVEAT, not unrank. `crossRunRank.js`'s
// "shown, valued, NO rank" is a per-ROW fact and Pareto membership is a RELATION over a SET, so
// unranking would publish a front the record does not support; and under `select` the ENGINE ranks
// this node, so a front that dropped it would omit the run's own champion while drawing its crown.
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import {
  OBJECTIVE_MEASURED, OBJECTIVE_SALVAGED, OBJECTIVE_SUBJECT_UNBOUND,
  objectiveMetricSource, objectiveSourceCaveated,
} from '../src/trustSemantics.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// ONE dev server for the whole file. Standing one up costs ~15 s, and four of them is a test that
// times out rather than a test that fails — the SSR load itself is what these cases need.
let server = null
const panels = async () => {
  if (!server) {
    server = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
  }
  return server.ssrLoadModule('/src/panels.jsx')
}
after(async () => { if (server) await server.close() })

// ---------------------------------------------------------------------------------------------
// RECORDS. Shaped as the fold serves them, and deliberately the same four the Metrics tab's own
// test uses — the point of this change is that ONE vocabulary answers about both surfaces.
// ---------------------------------------------------------------------------------------------

const salvageRow = (salvage) => ({ name: 'metric_salvaged', value: 0.81, max: null, min: null, salvage })

// `metric_salvage: "select"` — the rung with NO violation row. Feasible, competes for champion, and
// the ONLY record of the salvage is the folded `metric_provenance`.
const admitted = (id, metric) => ({
  id, status: 'evaluated', metric, feasible: true, violations: [], operator: 'mutate',
  metric_provenance: { salvaged: true, condition: 'artifact_contract', source: 'declared_reader',
    reader: 'stdout_regex', stage: 'score', producer: 'operator_stage' },
})

const measured = (id, metric, extra = {}) => ({
  id, status: 'evaluated', metric, feasible: true, violations: [], operator: 'mutate', ...extra,
})

// The `audit` rung: a salvage ROW, therefore infeasible, therefore already off this panel.
const auditSalvaged = (id, metric) => ({
  id, status: 'evaluated', metric, feasible: false, operator: 'mutate',
  violations: [salvageRow({ salvaged: true, condition: 'stage_failed', source: 'declared_reader',
    reader: 'stdout_regex', stage: 'score', producer: 'agent_stage' })],
})

// A legacy row that carries the salvage ROW but no `feasible` field at all. The panel's own filter
// (`feasible !== false`) lets it through, so the LABEL has to come from the record and not from the
// flag — this is the case that proves the panel reads the vocabulary rather than re-deriving one.
const legacySalvaged = (id, metric) => ({
  id, status: 'evaluated', metric, operator: 'mutate',
  violations: [salvageRow({ salvaged: true, condition: 'stage_failed', source: 'declared_reader',
    reader: 'stdout_regex', stage: 'train', producer: 'agent_stage' })],
})

const st = (nodes, extra = {}) => ({
  nodes: Object.fromEntries(nodes.map(n => [n.id, n])),
  direction: 'max', run_id: 'demo', ...extra,
})

// ---------------------------------------------------------------------------------------------
// THE PREDICATE. It was the inline expression `objective.channel !== OBJECTIVE_MEASURED` inside
// `Inspector.jsx`; a second surface copying that expression is how one vocabulary becomes two.
// ---------------------------------------------------------------------------------------------

test('objectiveSourceCaveated marks exactly the non-measured channels', () => {
  assert.equal(objectiveSourceCaveated({ channel: OBJECTIVE_SALVAGED }), true)
  assert.equal(objectiveSourceCaveated({ channel: OBJECTIVE_SUBJECT_UNBOUND }), true)
  assert.equal(objectiveSourceCaveated({ channel: OBJECTIVE_MEASURED }), false)
})

test('the predicate never invents a caveat nobody recorded', () => {
  // Same rule `objectiveMetricSource` is total under. An absent record is not evidence of a defect,
  // and this predicate decides whether a SELECTION display accuses a node.
  for (const junk of [null, undefined, {}, { channel: '' }, { channel: null }, 'salvaged', 0]) {
    assert.equal(objectiveSourceCaveated(junk), false, `${JSON.stringify(junk)} must not be marked`)
  }
})

test('the predicate agrees with the model on every record this panel can see', () => {
  assert.equal(objectiveSourceCaveated(objectiveMetricSource(admitted(4, 0.81))), true)
  assert.equal(objectiveSourceCaveated(objectiveMetricSource(legacySalvaged(5, 0.9))), true)
  assert.equal(objectiveSourceCaveated(objectiveMetricSource(measured(3, 0.51))), false)
  // A repaired DECLARATION was re-checked and PASSED, so it reads measured and must stay unmarked.
  assert.equal(objectiveSourceCaveated(objectiveMetricSource(measured(6, 0.6, {
    metric_provenance: { salvaged: false, declaration_repaired: true, stage: 'train' },
  }))), false)
  // A breached BOUND is a fact about a bound; the metric beside it was measured.
  assert.equal(objectiveSourceCaveated(objectiveMetricSource(measured(7, 0.7, {
    feasible: false, violations: [{ name: 'latency_ms', value: 900, max: 500 }],
  }))), false)
})

// ---------------------------------------------------------------------------------------------
// THE FRONT ARITHMETIC — that a caveated point really does displace measured ones, which is the
// reason a label alone is not the whole fix.
// ---------------------------------------------------------------------------------------------

test('a caveated point dominates measured nodes OFF the front', async () => {
  const { paretoFront } = await panels()
    const nodes = [measured(3, 0.51), admitted(4, 0.81)]
    assert.deepEqual(paretoFront(nodes, 'max').front.map(n => n.id), [4],
      'the whole front is the salvaged node — the measured one is not shown at all')
    const measuredOnly = nodes.filter(n => !objectiveSourceCaveated(objectiveMetricSource(n)))
    assert.deepEqual(paretoFront(measuredOnly, 'max').front.map(n => n.id), [3],
      'and #3 is what the operator would be reading if only measured numbers counted')
})

// ---------------------------------------------------------------------------------------------
// AN AXIS NOBODY CAN ORIENT IS NOT AN AXIS.
//
// `paretoFront` treated every extra_metrics key as cost-like, which was harmless only because no
// run has ever recorded a real second objective: measured over every `*.jsonl` under `runs/` (131
// files, 15 run directories) the sole extras are the engine's four CUDA-probe CONSTANTS in the
// specgate toys, and a constant dimension can neither create nor break a domination.
//
// The objectives queued to be recorded are QUALITY metrics — one vecsearch score stage already
// prints nDCG@k, MAP@k, MRR@k, Precision@k and Recall@k at seven cutoffs, ~35 higher-is-better
// numbers, of which the record keeps one. Under the old rule the BETTER nDCG loses on its own axis
// and nothing on screen looks wrong: every number real, the ordering backwards.
// ---------------------------------------------------------------------------------------------

const withNdcg = (id, metric, ndcg, direction) => measured(id, metric, {
  extra_metrics: { 'nDCG_at_100': ndcg },
  ...(direction ? { extra_metrics_direction: { 'nDCG_at_100': direction } } : {}),
})

test('a higher-is-better axis is ranked the right way up once the record declares it', async () => {
  const { paretoFront } = await panels()
  // #5 wins the primary, #6 wins nDCG. Correctly oriented, NEITHER dominates: both are on the front.
  const oriented = [withNdcg(5, 0.79, 0.41, 'max'), withNdcg(6, 0.76, 0.44, 'max')]
  assert.deepEqual(paretoFront(oriented, 'max').front.map(n => n.id).sort(), [5, 6],
    'each node is strictly better on one objective, so the front is both of them')
  assert.deepEqual(paretoFront(oriented, 'max').keys, ['nDCG_at_100'],
    'and the declared axis really is one of the ranked dimensions')
})

test('an UNDECLARED axis is dropped from the ordering rather than assumed to be a cost', async () => {
  const { paretoFront } = await panels()
  // The same two nodes with no direction recorded — every log written before this shipped. The old
  // rule read nDCG as cost-like and let #5 (worse nDCG, better metric) dominate #6 off the table.
  const unoriented = [withNdcg(5, 0.79, 0.41), withNdcg(6, 0.76, 0.44)]
  assert.deepEqual(paretoFront(unoriented, 'max').keys, [],
    'an axis the record cannot orient is not ranked on at all')
  // And the CONSEQUENCE, stated rather than wished for: with no orientable second axis there is
  // one objective, so the better primary wins outright and #6 leaves the front. That is the honest
  // answer — a single-objective ranking the operator can read as one — and it is not the same as
  // the old behaviour, which reached the identical front by ranking nDCG upside down and would
  // have kept doing so as the numbers moved. The falsifier is `keys`, not the front's membership.
  assert.deepEqual(paretoFront(unoriented, 'max').front.map(n => n.id), [5],
    'the front is the primary metric alone')
})

test('two nodes disagreeing about a direction leaves the axis unorientable', async () => {
  const { paretoFront } = await panels()
  const split = [withNdcg(5, 0.79, 0.41, 'max'), withNdcg(6, 0.76, 0.44, 'min')]
  assert.deepEqual(paretoFront(split, 'max').keys, [],
    'a contradiction is not a direction — the record does not say which way is better')
})

// ---------------------------------------------------------------------------------------------
// THE RENDER. Nothing else in the suite MOUNTS a panel, which is why this survived.
// ---------------------------------------------------------------------------------------------

test('the Pareto front carries the record the vocabulary supports', async () => {
  const { ParetoPanel } = await panels()
    const draw = (state) => renderToStaticMarkup(React.createElement(ParetoPanel, { state }))

    // THE DEFECT, fixed. A salvage-admitted champion beside a measured node.
    const html = draw(st([measured(3, 0.51), admitted(4, 0.81)], { best_node_id: 4 }))
    assert.match(html, /0\.81<span class="warn"/, 'the caveat rides on the number itself')
    assert.match(html, /· salvaged/)
    assert.match(html, /title="NOT measured: the experiment produced this number and the run recovered/)
    assert.match(html, /competes for champion/, 'the tooltip names which salvage rung this is')
    // PRINTED, not only hovered — a tooltip is not discoverable and this table decides reuse.
    assert.match(html, /⚠ #4/)
    assert.match(html, /objective is <b>salvaged<\/b>/)
    // UNRANK WAS REFUSED, and the refusal has to be observable: the node keeps its row, its value
    // and its crown, because the engine really is selecting on it.
    assert.match(html, /#4/)
    assert.match(html, /0\.81/)
    assert.match(html, /#crown/)
    // …and the measured node the caveated point displaced is NAMED rather than silently gone.
    assert.match(html, /#3 \(0\.51\) is non-dominated on measured objectives alone/)
    assert.match(html, /this front shows the selection the engine is actually making/)

    // THE NEGATIVE CONTROL: two genuinely measured nodes are untouched — no label, no warn, no
    // footnote, and no measured-objectives-only sentence to imply one was needed.
    const clean = draw(st([measured(3, 0.51), measured(4, 0.81)], { best_node_id: 4 }))
    assert.doesNotMatch(clean, /salvaged/)
    assert.doesNotMatch(clean, /no subject/)
    assert.doesNotMatch(clean, /class="warn"/)
    assert.doesNotMatch(clean, /non-dominated on measured objectives alone/)
    assert.match(clean, /0\.81/, 'the number itself is unchanged')

    // The `audit` rung is excluded by the panel's own filter, as it always was — the engine keeps
    // it out of `feasible_nodes`, so a caveat here would contradict the exclusion.
    const audited = draw(st([measured(3, 0.51), auditSalvaged(4, 0.81)], { best_node_id: 3 }))
    assert.doesNotMatch(audited, /0\.81/, 'an infeasible salvaged node is not on the front at all')

    // The LABEL comes from the record, never from `feasible`: a legacy row with a salvage violation
    // and no `feasible` field passes the panel's filter and must still be marked.
    const legacy = draw(st([measured(3, 0.51), legacySalvaged(4, 0.81)], { best_node_id: 4 }))
    assert.match(legacy, /· salvaged/)
    assert.match(legacy, /stage “train” failed its contract/)
    assert.match(legacy, /excluded from winner selection/,
      'this rung IS excluded by the engine, and the tooltip says so rather than reusing `select`')

    // A breached BOUND is not a caveat on the NUMBER. Feasibility is an EVERY-row question, "what
    // is this number" an ANY-row one, and this surface asks the second.
    const bounded = draw(st([measured(3, 0.51), measured(4, 0.81, {
      violations: [{ name: 'latency_ms', value: 900, max: 500 }],
    })], { best_node_id: 4 }))
    assert.doesNotMatch(bounded, /class="warn"/)
    assert.doesNotMatch(bounded, /salvaged/)
})

test('the diversity archive reads the same vocabulary', async () => {
  const { ParetoPanel } = await panels()
    const archive = { niches: 2, elites: [{ node_id: 4, metric: 0.81, params: { lr: 0.1 } }] }
    const html = renderToStaticMarkup(React.createElement(ParetoPanel, {
      state: st([measured(3, 0.51), admitted(4, 0.81)], { best_node_id: 4, archive }),
    }))
    // Anchored on the elite row's own params cell so this cannot be satisfied by the front table
    // above it, which shows the same node.
    assert.match(html, /0\.81<span class="warn"[^>]*> · salvaged<\/span><\/td><td class="muted">\{&quot;lr&quot;:0\.1\}/,
      'the elite row is the other reuse-this-configuration table on this panel')

    // An elite naming a node the state does not hold gets NO caveat — the model is total, and an
    // absent record is not evidence of a salvage.
    const orphan = renderToStaticMarkup(React.createElement(ParetoPanel, {
      state: st([measured(3, 0.51)], {
        best_node_id: 3, archive: { niches: 1, elites: [{ node_id: 99, metric: 0.4, params: {} }] },
      }),
    }))
    assert.doesNotMatch(orphan, /class="warn"/)
})

test('the registry champion says what its own number is', async () => {
  const { RegistryPanel } = await panels()
    const draw = (state) => renderToStaticMarkup(React.createElement(RegistryPanel, { state }))

    // Under `select` the champion is PRECISELY the node that can be salvaged, and that rung mints
    // no violation row — so nothing else on this panel could have said so.
    const html = draw(st([measured(3, 0.51), admitted(4, 0.81)], { best_node_id: 4 }))
    assert.match(html, /· salvaged/)
    assert.match(html, /champion was selected on a number marked/)
    assert.match(html, /competes for champion/)

    // A promoted champion is read the same way — the claim is about the node's record, not about
    // which of the two fields named it.
    const promoted = draw(st([measured(3, 0.51), admitted(4, 0.81)], { champion: 4 }))
    assert.match(promoted, /\(promoted\)/)
    assert.match(promoted, /· salvaged/)

    // The negative control: a measured champion keeps the bare number it always had.
    const clean = draw(st([measured(3, 0.51), measured(4, 0.81)], { best_node_id: 4 }))
    assert.doesNotMatch(clean, /salvaged/)
    assert.doesNotMatch(clean, /class="warn"/)
    assert.match(clean, /0\.81/)
})
