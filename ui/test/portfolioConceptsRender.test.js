// The global Concepts view, SSR-rendered over the REAL run-list payload shape.
//
// A new file on purpose: this one loads React and a component, and the suite's convention is that a
// module-scope import which can die at load time takes its whole file with it.
//
// Why render at all when `conceptForest.test.js` already drives the model: the model cannot see a JSX
// structure break, and this repo has a recorded near-miss where a dropped brace left `vite build`
// refusing the tree while 767 tests stayed green. It also cannot see the thing that actually makes
// this surface honest — whether the disclosures REACH the screen. A tree whose coverage sentence,
// Untagged bucket or spelling-variant notice is computed and then not rendered is exactly the
// dishonest surface those three exist to prevent, and only a render can tell the difference.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
// React comes through Node's own resolution, not `vite.ssrLoadModule('react')`: react is CJS and
// loading it as an inlined SSR module leaves it evaluating `module.exports` with no `module` in
// scope. Vite externalizes bare node_modules deps for SSR anyway, so the component below resolves
// the same instance this file imports.
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

// Mirrors the real `/api/runs` rows measured against runs/ on 2026-08-06: whole-id concept keys with
// {count, best_metric}, a majority of untagged runs, two tasks with opposite directions, and the
// -/_ spelling drift that corpus genuinely contains.
const RUNS = [
  {
    run_id: 'b2-validate', task_id: 'blob_classification', direction: 'max', label: 'b2 validate',
    concepts: {
      'feature/polynomial': { count: 6, best_metric: 0.925 },
      'optimization/lr': { count: 6, best_metric: 0.925 },
      'regularization/l2': { count: 6, best_metric: 0.925 },
    },
  },
  {
    run_id: 'live-cards-0804', task_id: 'toy_quadratic', direction: 'min',
    concepts: {
      'optimization/gradient-descent': { count: 4, best_metric: 0.0 },
      'objective/convex-quadratic': { count: 11, best_metric: 0.0 },
    },
  },
  {
    run_id: 'stagnation', task_id: 'stagnation', direction: 'max',
    concepts: {
      'optimization/hyperparameter-tuning': { count: 3, best_metric: 0.98 },
      'optimization/hyperparameter_tuning': { count: 1, best_metric: 0.97 },
    },
  },
  { run_id: 'glm_dataset', task_id: 'dataset_task', direction: 'min', concepts: {} },
  { run_id: 'live-nosignal', task_id: 'toy_quadratic', direction: 'min', concepts: {} },
]

async function render(props) {
  const vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
  })
  try {
    const { default: PortfolioConcepts } = await vite.ssrLoadModule('/src/PortfolioConcepts.jsx')
    assert.equal(typeof PortfolioConcepts, 'function')
    return renderToStaticMarkup(React.createElement(PortfolioConcepts, props))
  } finally {
    await vite.close()
  }
}

test('the Concepts view renders its tree and every disclosure that keeps it honest', async () => {
  const html = await render({ runs: RUNS, scopeLabel: 'All runs' })

  // The scope is named on the surface. An unlabelled tree is a tree about nobody knows what.
  assert.match(html, /Concepts · All runs/)

  // COVERAGE, above the tree. Without it a tree folded from 3 of 5 runs reads as the whole lab.
  assert.match(html, /<b>3 of 5 runs<\/b>/)
  assert.match(html, /<b>2 untagged<\/b>/)
  assert.match(html, /7 concepts across 4 roots/)

  // The UNTAGGED bucket reaches the screen, with the sentence that stops "not in the tree" from
  // reading as "nothing was learned there".
  assert.match(html, /2 runs in this scope carry\s*no concept at all/)
  assert.match(html, /gap in tagging, not evidence that nothing was learned/)

  // The spelling drift is DISCLOSED and the two ids are still two ids.
  assert.match(html, /spelled\s*more than one way — shown separately, not merged/)
  assert.match(html, /optimization\/hyperparameter-tuning/)
  assert.match(html, /optimization\/hyperparameter_tuning/)
  assert.match(html, /LoopLab does not\s*infer a taxonomy/)

  // Roots are collapsed by default and ordered by id, so the poll cannot move click targets.
  const order = ['feature', 'objective', 'optimization', 'regularization']
    .map(id => html.indexOf(`data-concept-id="${id}"`))
  assert.ok(order.every(index => index >= 0), 'every root must render')
  assert.deepEqual(order, [...order].sort((a, b) => a - b))
  assert.equal(html.includes('data-concept-id="optimization/lr"'), false)
})

test('a metric is printed only where one objective produced it', async () => {
  const html = await render({ runs: RUNS, scopeLabel: 'All runs' })
  // `feature` is reached by one max-objective run: a number is honest and is shown with its arrow.
  const feature = html.slice(html.indexOf('data-concept-id="feature"'))
  assert.match(feature.slice(0, 400), /↑ 0\.9250/)
  // `optimization` spans an accuracy (max) and a loss (min). One number there would be a comparison
  // between two objectives, so there must be none — and no zero standing in for one either.
  const optimization = html.slice(html.indexOf('data-concept-id="optimization"'))
  const row = optimization.slice(0, optimization.indexOf('</li>'))
  assert.match(row, /3 runs/)
  assert.equal(/[↑↓]/.test(row), false, 'no metric may be printed across two objectives')
})

test('an all-untagged scope says so instead of rendering an empty tree', async () => {
  const html = await render({
    runs: [{ run_id: 'a', task_id: 't', direction: 'min', concepts: {} }], scopeLabel: 'Project X',
  })
  assert.match(html, /No run in this scope carries a concept tag/)
  assert.match(html, /a fact about tagging and not about what was learned/)
  // Still discloses the bucket rather than leaving the operator with a bare refusal.
  assert.match(html, /1 run in this scope carry|1 run in this scope carries|1 run/)
})

test('co-occurrence reaches the screen as a threshold result, never as silence', async () => {
  // The corpus above has NO pair in two runs (every run is its own task), which is exactly the state
  // the real 46-run corpus is in — one pair at the floor, 248 candidates below it. A blank panel here
  // would read as "nothing co-occurs", so the empty state must name the threshold AND the candidates.
  const html = await render({ runs: RUNS, scopeLabel: 'All runs' })
  assert.match(html, /Studied together/)
  assert.match(html, /No pair of concepts appeared together in 2 or more runs/)
  assert.match(html, /5 pairing\(s\) were seen in a single run/)
  assert.match(html, /not yet evidence of a pattern/)
  // The floor is an operator control, defaulting to the same 2 the Python half ships.
  assert.match(html, /aria-pressed="true"[^>]*>2\+ runs|2\+ runs/)
  assert.match(html, /any run/)
})

test('a scope with nothing to pair says THAT, not "no pair reached the threshold"', async () => {
  // Two distinguishable absences. "Every run named one concept" is a fact about tagging depth;
  // "pairs exist but none repeats" is a fact about the lab. Rendering the second sentence for the
  // first case blames the threshold for something the threshold never saw. (Mutation-proved: the
  // zero-candidate branch survived until this test existed.)
  const html = await render({
    runs: [
      { run_id: 'r1', task_id: 't', direction: 'max', concepts: { 'a/x': { count: 1 } } },
      { run_id: 'r2', task_id: 't', direction: 'max', concepts: { 'b/y': { count: 2 } } },
    ],
    scopeLabel: 'All runs',
  })
  assert.match(html, /No run in this scope was tagged with two concepts, so there is nothing to pair/)
  assert.equal(/No pair of concepts appeared together in/.test(html), false)
})

test('a repeated pair is listed with the number of DISTINCT runs behind it', async () => {
  const paired = [
    { run_id: 'r1', task_id: 't', direction: 'max',
      concepts: { 'loss/contrastive': { count: 2, best_metric: 0.9 },
                  'data/hard-negatives': { count: 2, best_metric: 0.9 } } },
    { run_id: 'r2', task_id: 't', direction: 'max',
      concepts: { 'loss/contrastive': { count: 1, best_metric: 0.8 },
                  'data/hard-negatives': { count: 1, best_metric: 0.8 } } },
    { run_id: 'r3', task_id: 't', direction: 'max',
      concepts: { 'loss/contrastive': { count: 1, best_metric: 0.7 },
                  'arch/moe': { count: 1, best_metric: 0.7 } } },
  ]
  const html = await render({ runs: paired, scopeLabel: 'All runs' })
  const panel = html.slice(html.indexOf('Studied together'))
  assert.match(panel, /data\/hard-negatives/)
  assert.match(panel, /loss\/contrastive/)
  assert.match(panel, /2 runs/)
  // Below the floor and therefore absent from the list, not silently folded in at strength 1.
  assert.equal(panel.slice(0, panel.indexOf('</ol>')).includes('arch/moe'), false)
})

test('the selection scope offers only what the operator has actually checked', async () => {
  const idle = await render({ runs: RUNS, selectedRuns: [], scopeLabel: 'All runs' })
  assert.match(idle, /Selected · 0/)
  assert.match(idle, /disabled=""/)
  const chosen = await render({
    runs: RUNS, selectedRuns: [RUNS[0], RUNS[1]], scopeLabel: 'All runs',
  })
  assert.match(chosen, /Selected · 2/)
  // Both scope buttons name their size, so the operator can see the subset before choosing it.
  assert.match(chosen, /List scope · 5/)
})
