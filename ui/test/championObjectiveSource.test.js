// THE RUN-LEVEL CLAIM ABOUT A NUMBER NOBODY MEASURED.
//
// The salvage/subject vocabulary (`trustSemantics.js`) was wired into four DISPLAY surfaces — the
// Metrics tab, the Pareto front, the archive elites and the cross-run champion row — and not into
// `report.js::trustCaveats`, which is the aggregator the Report headline, the model card and both
// exports are built from. Under `metric_salvage: "select"` — the rung that admits a salvaged metric
// into selection and therefore mints NO violation row — every branch of that aggregator found
// nothing: no reward-hack row, no infeasible node, and, for a multi-seed champion, not even the
// single-seed caveat. `verdict()` then computed `trust: 'unverified'` and published
//
//     "Improved the metric by 3 (30%) — champion #1 is robust across 3 seeds; no trust flags are
//      recorded, but detector coverage is not fully verified."
//
// about a number the protected scoring path never read, while the Pareto tab one click away marked
// that same node `salvaged`. The champion's own metric is not a detail of the win; it is the win's
// evidence, so it belongs in the list every downstream surface reads.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import { analyze, trustCaveats, verdict } from '../src/report.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// A two-node run whose champion is multi-seed confirmed: every OTHER caveat branch is silent here,
// which is exactly the state in which the aggregator used to return an empty list.
const runWith = (championOver = {}) => ({
  run_id: 'salvage-report', task_id: 'task', goal: 'Champion evidence', direction: 'min',
  phase: 'finished', reward_hacks: [], drifts: [], research: [], best_node_id: 1,
  nodes: {
    0: { id: 0, status: 'evaluated', metric: 10, feasible: true, parent_ids: [], idea: { params: {} } },
    1: {
      id: 1, status: 'evaluated', metric: 7, feasible: true, parent_ids: [0], idea: { params: {} },
      operator: 'manual', violations: [],
      confirmed_mean: 7, confirmed_std: 0.01, confirmed_seeds: 3,
      ...championOver,
    },
  },
})

// `metric_salvage: "select"` over operator-produced output: the metric was recovered from a failed
// eval, the node is admitted to selection, and the record's ONLY trace of it is `metric_provenance`.
const SALVAGE_ADMITTED = {
  metric_provenance: { salvaged: true, source: 'declared_reader', stage: 'train',
    producer: 'operator_stage' },
}

test('a salvaged champion is a run-level caveat, not just a table cell', () => {
  const run = runWith(SALVAGE_ADMITTED)
  const caveats = trustCaveats(run, run.nodes[1])
  const objective = caveats.find(c => c.kind === 'objective-source')
  assert.ok(objective, 'the aggregator every export reads must carry the champion’s own objective')
  assert.match(objective.text, /salvaged/)
  assert.equal(objective.panel, 'trust', 'the chip must deep-link to the panel that explains it')
  assert.equal(objective.severity, 'alarm')

  const v = verdict(run, analyze(run))
  assert.equal(v.trust, 'suspect', 'the champion’s number being unmeasured flags the win itself')
  assert.match(v.headline, /the win is flagged, treat with caution/)
  assert.doesNotMatch(v.headline, /no trust flags are recorded/,
    'the exact clean-win sentence this run used to publish')
})

test('an unbound subject reaches the headline through the same one row', () => {
  // Different channel, same rule: the number was measured and nothing records WHICH ARTIFACT it is
  // about. `trustSemantics.js` keeps those two words apart on purpose (nothing failed and nothing
  // was recovered here), and the aggregator quotes its label rather than inventing a second one.
  // `feasible: false` because the row is real: an enforced unbound subject excludes the node
  // (`feasible = not violations`), and a `trust_gate: "audit"` champion is how such a node still
  // reaches the headline. So the infeasibility caveat fires too — the two are separate facts and the
  // list carries both, which is the point of pushing a ROW rather than rewording an existing one.
  const run = runWith({
    feasible: false,
    violations: [{ name: 'metric_salvaged',
      salvage: { condition: 'metric_subject_unbound', unbound_reason: 'not_declared' } }],
  })
  const caveats = trustCaveats(run, run.nodes[1])
  const objective = caveats.find(c => c.kind === 'objective-source')
  assert.ok(objective)
  assert.match(objective.text, /measured, no subject/)
  assert.ok(caveats.some(c => c.kind === 'infeasible'), 'the constraint statement is not replaced')
})

test('a measured champion gains nothing — a caveat nobody recorded is not invented', () => {
  // The other half of `objectiveMetricSource` being total: this list must stay exactly as it was for
  // every run that has no salvage record at all, or the new row is just a second unverified banner.
  const run = runWith()
  assert.deepEqual(trustCaveats(run, run.nodes[1]), [])
  assert.equal(verdict(run, analyze(run)).trust, 'unverified')
  // A repaired DECLARATION is a MEASURED metric — the artifact contract was re-checked and passed,
  // and `engine/metric_salvage.py::declaration_repair_provenance` spells `salvaged: False` out for
  // exactly this reader. Softening it here would be the same error one direction over.
  const repaired = runWith({
    metric_provenance: { salvaged: false, declaration_repaired: true, stage: 'score' },
  })
  assert.deepEqual(trustCaveats(repaired, repaired.nodes[1]), [])
})

test('the champion CARD prints the same label as every other surface', async () => {
  // The Report's own champion card was the fifth surface and the one where the number IS the claim,
  // and it printed it bare. Rendered rather than pinned: a source pin cannot tell a rendered branch
  // from a commented one, and this branch sits inside a 3,000-line tree.
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ReportView } = await vite.ssrLoadModule('/src/Report.jsx')
    const run = runWith(SALVAGE_ADMITTED)
    const dom = new JSDOM(renderToStaticMarkup(React.createElement(ReportView, {
      state: run, runId: run.run_id, readOnly: true,
    })))
    try {
      const doc = dom.window.document
      const card = doc.querySelector('.champion-card')
      assert.ok(card)
      assert.match(card.textContent, /salvaged/, 'the champion’s number carries its own provenance')
      const mark = card.querySelector('.warn[title]')
      assert.ok(mark, 'and hovers the same sentence the Metrics tab and Pareto front hover')
      assert.match(mark.getAttribute('title'), /recovered it with its own declared reader/)
      assert.match(mark.getAttribute('title'), /competes for champion/)
      // FEASIBILITY IS UNTOUCHED, deliberately: under `select` the engine really does admit this
      // node (`feasible = not violations`, and that rung mints no row), so `yes` is the engine's own
      // answer to the question that row asks. `trustSemantics.js` states the split — feasibility
      // asks why a node is EXCLUDED, the objective source asks what the NUMBER is, and both
      // statements are true at once. Softening the word here would put this card back in
      // disagreement with the Trust tab, one vocabulary over.
      assert.match(card.textContent, /feasibleyes|feasible\s*yes/)
      // And the headline's caveat chip carries it too.
      assert.match(doc.querySelector('.verdict-banner').textContent, /objective is salvaged/)
    } finally {
      dom.window.close()
    }
  } finally {
    await vite.close()
  }
})
