import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import { hyperImportance } from '../src/report.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// What the run-wide importance table is ALLOWED to claim. This lives beside `reportPresentation`'s
// other `hyperImportance` coverage rather than in it because that file imports `jsdom` at module
// scope, which does not load on every checkout — a test nobody can run is not coverage.
//
// The rule: a param every evaluated node set to the SAME value has an undefined correlation with
// the metric (the Pearson denominator is 0). Reporting that as `importance 0 · r +0` says
// "measured, does not matter" about a knob nobody varied — the opposite conclusion, on the one
// panel whose job is telling an operator what to tune next. Measured across the runs under `runs/`:
// EVERY `importance 0` row in the corpus is this degenerate case (`l2` was [0.01, 0.01, 0.01],
// `lr` was [0.03, 0.03, 0.03], `learning_rate` was [0.5, 0.5, 0.5]); not one is a real zero.
const varyingAndConstantParams = () => {
  const nodes = {}
  for (let id = 0; id < 4; id++) {
    nodes[id] = {
      id, metric: id + 1, operator: 'improve', parent_ids: id ? [id - 1] : [],
      feasible: true, status: 'evaluated',
      // `depth` moves with the metric (a real, measurable correlation); `lr` never moves.
      idea: { params: { depth: id, lr: 0.01 }, theme: `direction-${id}` },
    }
  }
  return {
    run_id: 'importance', task_id: 'task', goal: 'importance semantics', direction: 'max',
    phase: 'finished', nodes, best_node_id: 3, reward_hacks: [], drifts: [], research: [],
  }
}

test('a hyperparameter nobody varied reports an unmeasurable correlation, not a zero one', () => {
  const rows = Object.fromEntries(hyperImportance(varyingAndConstantParams()).map(row => [row.k, row]))

  assert.equal(typeof rows.depth.imp, 'number', 'a param that varies still gets a real correlation')
  assert.ok(rows.depth.imp > 0)
  assert.equal(rows.lr.imp, null, 'a constant param has no correlation to report')
  assert.equal(rows.lr.r, null)
  assert.equal(rows.lr.n, 4, 'the evidence count is still a fact and stays')

  // Unmeasurable rows sort last: they are not a small importance, they are no measurement at all.
  assert.deepEqual(hyperImportance(varyingAndConstantParams()).map(row => row.k), ['depth', 'lr'])
})

test('the importance table prints an unvaried knob as unknown rather than as 0% importance', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { HyperImportancePanel } = await vite.ssrLoadModule('/src/panels.jsx')
    const markup = renderToStaticMarkup(React.createElement(HyperImportancePanel, {
      state: varyingAndConstantParams(), onClose() {},
    }))
    const cells = name => {
      const at = markup.indexOf(`>${name}<`)
      assert.ok(at > 0, `param ${name} must be in the table`)
      return markup.slice(at + 1, markup.indexOf('</tr>', at))
        .split(/<[^>]*>/g).map(part => part.trim()).filter(Boolean)
    }

    // param | importance | r | n | relative importance
    assert.deepEqual(cells('lr').slice(0, 4), ['lr', '—', '—', '4'],
      'an unmeasurable correlation must not be signed, scored, or ranked')
    assert.match(markup, /not varied/, 'and the gauge cell must say why instead of reading 0%')

    const depth = cells('depth')
    assert.notEqual(depth[1], '—', 'a param that varies keeps its measured importance')
    assert.match(depth[2], /^[+-]/, 'and keeps its signed correlation')
  } finally {
    await vite.close()
  }
})
