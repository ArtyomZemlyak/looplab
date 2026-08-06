// The global Concepts view, SSR-rendered — the two ways it could describe a population it is not
// showing. A new file for the same reason `portfolioConceptsRender.test.js` is one: this file loads
// React and a component, and a module-scope import that can die at load time takes its whole file.
//
// `conceptForest.test.js` and `conceptCooccurrence.test.js` already drive the co-occurrence rules IN
// THE MODEL — `conceptCooccurrence.test.js:139` even asserts `pairsOmitted > 0`. Neither could see
// the defect, because the defect was that the number is computed correctly and then never rendered.
// That is the same gap `portfolioConceptsRender.test.js` was written for: a disclosure that is
// computed and not shown is exactly the dishonest surface the disclosure was added to prevent.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const withComponent = async fn => {
  const vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
  })
  try {
    const { default: PortfolioConcepts } = await vite.ssrLoadModule('/src/PortfolioConcepts.jsx')
    const forest = await vite.ssrLoadModule('/src/conceptForest.js')
    return await fn({ PortfolioConcepts, forest })
  } finally { await vite.close() }
}

// ONE run carrying the server's own per-run cap. `serve/run_projections.py` bounds a run's rollup at
// MAX_ROLLUP_CONCEPTS = 64, and C(64,2) = 2,016 pairs — past the fold's MAX_PAIRS = 2,000 — from a
// single row. So this is not a corpus-size hypothetical: it is one wide-tagged run, and the `any
// run` floor button this very panel offers is what brings the pairs below the default threshold.
const WIDE_RUN = {
  run_id: 'wide', task_id: 'blob_classification', direction: 'max', label: 'wide',
  concepts: Object.fromEntries(Array.from({ length: 64 },
    (_, i) => [`axis${String(i).padStart(2, '0')}/leaf`, { count: 1, best_metric: 0.5 }])),
}

const TAGGED = (id, ids) => ({
  run_id: id, task_id: 'toy_quadratic', direction: 'min',
  concepts: Object.fromEntries(ids.map(c => [c, { count: 1, best_metric: 0 }])),
})

test('the ranking cap drops counted pairs that neither other absence covers', async () => {
  await withComponent(({ forest }) => {
    const co = forest.buildConceptCooccurrence([WIDE_RUN], { minRuns: 1 })
    assert.equal(co.pairCandidates, 2016)
    assert.equal(co.pairs.length, 2000)
    assert.equal(co.pairsOmitted, 16)
    // The premise of the fix: BOTH of the other two disclosures read "nothing was withheld" while
    // 16 pairs had been counted and dropped, so neither could have covered for the missing one.
    assert.equal(co.pairsOutsideProjectionUnknown, false)
    assert.equal(co.pairSourceNodesPruned, 0)
    // And the concept ranked last really does lose partners to it.
    assert.ok(forest.partnersOf(co, 'axis63/leaf').length < 63)
  })
})

test('the pairs panel states how many pairs it counted and did not rank', async () => {
  // Two identical wide runs, so all 2,016 pairs are attested by 2 distinct runs and the spill
  // happens at the panel's DEFAULT floor — no interaction needed to reach the state.
  const runs = [WIDE_RUN, { ...WIDE_RUN, run_id: 'wide2', label: 'wide2' }]
  const { html, co } = await withComponent(({ PortfolioConcepts, forest }) => ({
    co: forest.buildConceptCooccurrence(runs, { minRuns: 2 }),
    html: renderToStaticMarkup(React.createElement(PortfolioConcepts,
      { runs, scopeLabel: 'Two wide runs' })),
  }))
  assert.equal(co.pairsOmitted, 16, 'the fixture must actually spill the ranking cap')

  assert.match(html, /16 further pair\(s\) reached this threshold and were counted/,
    'the ranking cap dropped 16 counted pairs and the panel said nothing about them')
  // It must NOT be reported as merely unknown. The two absences are kept apart on purpose: a pruned
  // node's pairs were never materialized, these were counted and then discarded, and printing an
  // exact loss as an unknown one is the same class of error in the other direction.
  assert.ok(!/pairs touching the other/.test(html), html.slice(0, 600))
})

test('no ranking-cap sentence when nothing was dropped', async () => {
  const runs = [TAGGED('a', ['loss/contrastive', 'model/mlp']),
    TAGGED('b', ['loss/contrastive', 'model/mlp'])]
  const html = await withComponent(({ PortfolioConcepts }) =>
    renderToStaticMarkup(React.createElement(PortfolioConcepts, { runs, scopeLabel: 'Small' })))

  assert.match(html, /Studied together/)
  assert.ok(!/further pair\(s\) reached this threshold/.test(html),
    'an absence notice with nothing absent is noise, and trains the operator to ignore it')
})

// ---------------------------------------------------------------------------------------------
// The other population claim: the heading, when runs stay CHECKED after a filter hides them.
// `RunList.jsx::compareRuns` maps the checked ids over the whole payload, not over `visible`, so
// this state is one filter keystroke away from any Compare selection. The rule lives in the model
// because the restricted state is behind a click — which is why nothing was driving it.
test('the restricted heading counts the runs folded, not the boxes ticked', async () => {
  await withComponent(({ forest }) => {
    const { conceptScopeClaim } = forest

    // Unrestricted: the list scope's own label, and nothing is out of scope by definition.
    assert.deepEqual(conceptScopeClaim({ scopeLabel: 'All runs', selectedCount: 7, activeCount: 2 }),
      { name: 'All runs', outOfScope: 0 })
    // Restricted with nothing checked is not a restriction at all.
    assert.deepEqual(
      conceptScopeClaim({ scopeLabel: 'All runs', restrictToSelection: true, selectedCount: 0, activeCount: 4 }),
      { name: 'All runs', outOfScope: 0 })

    // THE DEFECT: 7 ticked, 4 still in scope. The heading must name 4 — the tree is folded from 4 —
    // and the 3 that fell out must be reported rather than quietly dropped.
    assert.deepEqual(
      conceptScopeClaim({ scopeLabel: 'Filtered', restrictToSelection: true, selectedCount: 7, activeCount: 4 }),
      { name: '4 selected runs', outOfScope: 3 })
    // Singular, and the fully-in-scope case discloses nothing.
    assert.deepEqual(
      conceptScopeClaim({ scopeLabel: 'x', restrictToSelection: true, selectedCount: 1, activeCount: 1 }),
      { name: '1 selected run', outOfScope: 0 })
    // A selection that has entirely left the scope still names its own emptiness rather than the 5.
    assert.deepEqual(
      conceptScopeClaim({ scopeLabel: 'x', restrictToSelection: true, selectedCount: 5, activeCount: 0 }),
      { name: '0 selected runs', outOfScope: 5 })
    // Never a negative disclosure, whatever two mismatched renders hand it.
    assert.equal(
      conceptScopeClaim({ restrictToSelection: true, selectedCount: 2, activeCount: 9 }).outOfScope, 0)
  })
})

// ---------------------------------------------------------------------------------------------
// The third population claim, and the only one that is about a loop that does not close.
test('the spelling-variant notice does not promise a merge this view would ever show', async () => {
  const runs = [TAGGED('a', ['optimization/hyperparameter-tuning']),
    TAGGED('b', ['optimization/hyperparameter_tuning'])]
  const html = await withComponent(({ PortfolioConcepts }) =>
    renderToStaticMarkup(React.createElement(PortfolioConcepts, { runs, scopeLabel: 'Drift' })))

  assert.match(html, /spelled\s*more than one way/)
  // It must not send the operator to a control that does not exist. The governed merge is
  // `looplab governance concept-merge` / `POST /api/cross-run/concept-alias`; the Atlas screen has
  // no concept-governance control at all.
  assert.ok(!/action in the Atlas/.test(html), 'the Atlas offers no concept merge')
  // And it must say the merge will not change this tree, which is the load-bearing part.
  assert.match(html, /does not read that registry yet/)
})

test('that notice stays true only while nothing here consumes the concept policy', async () => {
  // The premise of the sentence above: `GET /api/cross-run/concept-policy` exists and returns the
  // canonicalization the browser would have to APPLY (`concept_lens.py::project_concept_map` takes
  // concept sets and no governance, so the caller canonicalizes), and no browser module reads it.
  // When someone wires it, this goes red — which is the point: the copy has to change in the same
  // breath, or the view starts under-claiming instead of over-claiming.
  const { readdirSync, readFileSync } = await import('node:fs')
  const src = fileURLToPath(new URL('../src', import.meta.url))
  // A QUOTED path only: the comment above the notice names the route in prose, and a comment that
  // explains why the route is unused must not read as the route being used.
  const requested = /['"`]\/api\/cross-run\/concept-policy/
  const consumers = readdirSync(src)
    .filter(name => /\.(js|jsx)$/.test(name))
    .filter(name => requested.test(readFileSync(`${src}/${name}`, 'utf8')))
  assert.deepEqual(consumers, [],
    'a module now reads the concept policy — apply it in conceptForest and rewrite the '
    + 'spelling-variant notice, which currently tells the operator a merge will NOT show up here')
})

test('the view renders the heading and the out-of-scope notice from that one rule', async () => {
  const inScope = [TAGGED('a', ['loss/contrastive']), TAGGED('b', ['model/mlp'])]
  const html = await withComponent(({ PortfolioConcepts }) =>
    renderToStaticMarkup(React.createElement(PortfolioConcepts,
      { runs: inScope, selectedRuns: [...inScope, TAGGED('c', ['x/y'])], scopeLabel: 'Filtered' })))

  // Unrestricted (the initial state), so the heading is the list scope and the notice is absent —
  // the notice belongs to the restricted state and must not fire merely because boxes are ticked.
  assert.match(html, /Concepts · Filtered/)
  assert.match(html, /List scope · 2/)
  assert.match(html, /Selected · 3/)
  assert.ok(!/checked run\(s\) are outside/.test(html), html.slice(0, 600))

  // And the component must reach the heading THROUGH the rule, so the two cannot drift apart again.
  const source = await withComponent(({ PortfolioConcepts }) => PortfolioConcepts.toString())
  assert.match(source, /conceptScopeClaim/)
  assert.ok(!/selectedIds\.size\} selected run/.test(source),
    'the heading must not announce a population the tree does not contain')
})
