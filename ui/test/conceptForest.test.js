// The GLOBAL concept tree's pure model. The properties under test are the ones that decide whether
// this surface tells the truth about a portfolio: that an untagged run is never silently dropped, that
// two runs disagreeing about the hierarchy are never merged into one invented taxonomy, and that a
// number is absent rather than wrong when the payload cannot honestly produce it.
//
// Shapes here mirror the real `/api/runs` rows measured against `runs/` on 2026-08-06 (46 runs, 15 of
// them tagged, 71 distinct ids) — including the actual spelling drift that corpus contains.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  buildConceptForest, forestCoverage, forestPathTo, runConceptEntries, spellingVariants,
  UNTAGGED, visibleForestRows,
} from '../src/conceptForest.js'

const run = (run_id, concepts, extra = {}) => ({
  run_id, concepts, task_id: 'blob_classification', direction: 'max', ...extra,
})
const tag = (count, best_metric = null) => ({ count, best_metric })

test('a run row is read through the canonical id boundary', () => {
  const { entries, dropped } = runConceptEntries(run('r', {
    'Optimization/LR': tag(6, 0.9), 'optimization/lr': tag(2, 0.95), '   ': tag(1), 'a/b': tag(3),
  }))
  // Two spellings of one id merge by SUMMING, because "experiments tagged here" is a count of
  // experiments and both spellings named the same concept.
  assert.deepEqual(entries, [
    { id: 'a/b', count: 3, bestMetric: null },
    { id: 'optimization/lr', count: 8, bestMetric: 0.95 },
  ])
  assert.equal(dropped, 1)
})

test('an unusable tag is REPORTED, never quietly turned into "never tagged"', () => {
  // Blaming a producer bug on the operator is the failure this counter exists to prevent.
  const forest = buildConceptForest([run('r', { '///': tag(2) })])
  assert.equal(forest.totals.untagged, 1)
  assert.equal(forest.totals.malformedRuns, 1)
  assert.equal(forest.totals.droppedIds, 1)
})

test('an untagged run is never dropped — it is a named bucket with its run ids', () => {
  const forest = buildConceptForest([
    run('tagged', { 'loss/contrastive': tag(3) }), run('bare', {}), run('missing', undefined),
  ])
  assert.equal(forest.untagged.id, UNTAGGED)
  assert.deepEqual(forest.untagged.runIds, ['bare', 'missing'])
  assert.equal(forest.totals.tagged, 1)
  assert.equal(forest.totals.untagged, 2)
})

test('the untagged bucket exists even when every run is tagged', () => {
  // Its absence would read as "everything is tagged"; a zero there is what makes the tree trustworthy.
  const forest = buildConceptForest([run('a', { 'loss/x': tag(1) })])
  assert.equal(forest.untagged.runs, 0)
  assert.deepEqual(forest.untagged.runIds, [])
})

test('ancestors are materialized from the id, and marked as NOT tagged', () => {
  const forest = buildConceptForest([run('r', { 'analysis/objective-landscape/sharpness': tag(1) })])
  assert.deepEqual(forest.roots, ['analysis'])
  assert.equal(forest.nodes['analysis'].tagged, false)
  assert.equal(forest.nodes['analysis/objective-landscape'].tagged, false)
  assert.equal(forest.nodes['analysis/objective-landscape/sharpness'].tagged, true)
  assert.deepEqual(forest.nodes['analysis'].children, ['analysis/objective-landscape'])
  assert.equal(forest.nodes['analysis/objective-landscape/sharpness'].depth, 2)
  // Only ids a run actually NAMED count as concepts studied; our grouping rows are not evidence.
  assert.equal(forest.totals.concepts, 1)
  assert.equal(forest.totals.nodes, 3)
})

test('picking an ancestor finds the run that only ever tagged a descendant', () => {
  const forest = buildConceptForest([run('deep', { 'loss/contrastive/in-batch': tag(4) })])
  assert.deepEqual(forest.nodes['loss'].runIds, ['deep'])
  assert.equal(forest.nodes['loss'].runs, 1)
})

test('a sibling PREFIX is not an ancestor', () => {
  // `loss` must never collect `lossless/codec`: the tree is built from real path segments.
  const forest = buildConceptForest([run('r', { 'lossless/codec': tag(1), 'loss/x': tag(1) })])
  assert.deepEqual(forest.nodes['loss'].runIds, ['r'])
  assert.deepEqual(forest.roots, ['loss', 'lossless'])
})

test('subtree run counts are DISTINCT runs, not a sum over tags', () => {
  // One run tagging two children of `optimization` is ONE run at `optimization`. Summing here is the
  // easiest way to publish a portfolio that looks twice as broad as it is.
  const forest = buildConceptForest([
    run('a', { 'optimization/lr': tag(6), 'optimization/weight-decay': tag(2) }),
    run('b', { 'optimization/lr': tag(1) }),
  ])
  assert.equal(forest.nodes['optimization'].runs, 2)
  assert.deepEqual(forest.nodes['optimization'].runIds, ['a', 'b'])
  assert.equal(forest.nodes['optimization/lr'].directRuns, 2)
  assert.equal(forest.nodes['optimization/lr'].directExperiments, 7)
})

test('runs that disagree about the hierarchy stay SEPARATE roots', () => {
  // The real corpus contains exactly this. Merging them under one root would be LoopLab inventing a
  // taxonomy — the stance ConceptView states as "LoopLab does not infer a taxonomy".
  const forest = buildConceptForest([
    run('a', { 'feature/polynomial': tag(6) }),
    run('b', { 'feature-engineering/polynomial': tag(2) }),
    run('c', { 'feature_engineering/polynomial_expansion': tag(1) }),
  ])
  assert.deepEqual(forest.roots, ['feature', 'feature-engineering', 'feature_engineering'])
  assert.equal(forest.nodes['feature'].runs, 1)
})

test('ids differing only in -/_ are REPORTED as variants and still not merged', () => {
  const forest = buildConceptForest([
    run('a', { 'optimization/hyperparameter-tuning': tag(3) }),
    run('b', { 'optimization/hyperparameter_tuning': tag(1) }),
  ])
  assert.deepEqual(forest.variants, [{
    key: 'optimization/hyperparametertuning',
    ids: ['optimization/hyperparameter-tuning', 'optimization/hyperparameter_tuning'],
  }])
  assert.equal(forest.nodes['optimization/hyperparameter-tuning'].runs, 1)
  assert.equal(forest.nodes['optimization/hyperparameter_tuning'].runs, 1)
  assert.equal(forest.nodes['optimization'].runs, 2)
})

test('a genuine difference of opinion about the hierarchy is not guessed at', () => {
  // `feature/polynomial` vs `feature-engineering/polynomial` differ by more than punctuation. We have
  // no evidence they mean the same thing, so we make no claim either way.
  assert.deepEqual(spellingVariants(['feature/polynomial', 'feature-engineering/polynomial']), [])
})

test('a best metric appears only when the contributing runs share one objective', () => {
  const forest = buildConceptForest([
    run('a', { 'model/gbm': tag(2, 0.90) }),
    run('b', { 'model/gbm': tag(1, 0.95) }),
  ])
  assert.deepEqual(forest.nodes['model/gbm'].best,
    { value: 0.95, direction: 'max', taskId: 'blob_classification', runs: 2 })
})

test('two objectives in one subtree yield NO metric, not a mixed one', () => {
  // 0.95 accuracy beside 0.10 RMSE in one column is the most convincing wrong number this surface
  // could print. `metricComparable` is the same predicate that gates the run list's metric sort.
  const forest = buildConceptForest([
    run('a', { 'model/gbm': tag(2, 0.95) }),
    run('b', { 'model/gbm': tag(1, 0.10) }, { task_id: 'dataset_task', direction: 'min' }),
  ])
  assert.equal(forest.nodes['model/gbm'].best, null)
  assert.equal(forest.nodes['model'].best, null)
})

test('a min objective reports the LOWEST value as best', () => {
  const forest = buildConceptForest([
    run('a', { 'objective/quadratic': tag(4, 0.5) }, { task_id: 'quad', direction: 'min' }),
    run('b', { 'objective/quadratic': tag(4, 0.0) }, { task_id: 'quad', direction: 'min' }),
  ])
  assert.equal(forest.nodes['objective/quadratic'].best.value, 0)
  assert.equal(forest.nodes['objective'].best.value, 0)
})

test('an untagged-but-scored run cannot lend its metric to a concept', () => {
  const forest = buildConceptForest([
    run('scored', {}, { best_metric: 0.99 }),
    run('tagged', { 'model/gbm': tag(1, 0.4) }),
  ])
  assert.equal(forest.nodes['model/gbm'].best.value, 0.4)
  assert.deepEqual(forest.nodes['model/gbm'].runIds, ['tagged'])
})

test('a concept with no scored experiment reports no metric rather than zero', () => {
  const forest = buildConceptForest([run('a', { 'model/gbm': tag(3, null) })])
  assert.equal(forest.nodes['model/gbm'].best, null)
  assert.equal(forest.nodes['model/gbm'].directExperiments, 3)
})

test('a concept id colliding with Object.prototype cannot reach the chain', () => {
  // LLM-authored ids make `__proto__` and `constructor` reachable tags.
  const forest = buildConceptForest([
    run('a', { '__proto__/x': tag(1), 'constructor/y': tag(2), 'toString': tag(1) }),
  ])
  assert.equal(forest.nodes['constructor/y'].runs, 1)
  assert.equal(forest.nodes['tostring'].runs, 1)
  // The node map is null-prototype, so an un-canonicalized key resolves to NOTHING. Over a plain
  // `{}` this same read returns Object.prototype.toString — truthy, and not a node, which is how a
  // weird tag turns into a phantom row instead of an error.
  assert.equal(forest.nodes['toString'], undefined)
  assert.equal(forest.nodes['__proto__'].children[0], '__proto__/x')
  assert.equal(({}).x, undefined)
  assert.ok(forest.roots.includes('constructor'))
  assert.ok(forest.roots.includes('tostring'))
})

test('rows are ordered by id and children appear only under an open parent', () => {
  const forest = buildConceptForest([
    run('a', { 'optimization/lr': tag(1), 'objective/quadratic': tag(1) }),
  ])
  assert.deepEqual(visibleForestRows(forest, new Set()).map(r => [r.id, r.depth, r.hasChildren]),
    [['objective', 0, true], ['optimization', 0, true]])
  assert.deepEqual(visibleForestRows(forest, new Set(['optimization'])).map(r => r.id),
    ['objective', 'optimization', 'optimization/lr'])
})

test('row order does not move when only counts change', () => {
  // This surface re-renders on the run list's 2.5s poll. Ranking by size would slide click targets
  // out from under the cursor every time an experiment finishes.
  const before = buildConceptForest([run('a', { 'a/x': tag(1), 'b/y': tag(99) })])
  const after = buildConceptForest([run('a', { 'a/x': tag(99), 'b/y': tag(1) })])
  const ids = forest => visibleForestRows(forest, new Set(['a', 'b'])).map(row => row.id)
  assert.deepEqual(ids(before), ids(after))
})

test('coverage states what fraction of the scope the tree actually describes', () => {
  const forest = buildConceptForest([
    run('a', { 'loss/x': tag(1) }), run('b', {}), run('c', {}),
  ])
  assert.deepEqual(forestCoverage(forest), {
    runs: 3, tagged: 1, untagged: 2, concepts: 1, roots: 1,
    complete: false, empty: false, malformedRuns: 0, droppedIds: 0,
  })
})

test('an all-untagged scope is EMPTY, which is not the same as having no runs', () => {
  const forest = buildConceptForest([run('a', {}), run('b', {})])
  assert.equal(forestCoverage(forest).empty, true)
  assert.equal(forestCoverage(forest).runs, 2)
  assert.equal(forestCoverage(buildConceptForest([])), null)
})

test('a fully tagged scope says so', () => {
  const forest = buildConceptForest([run('a', { 'loss/x': tag(1) })])
  assert.equal(forestCoverage(forest).complete, true)
})

test('the path to a concept is every ancestor a deep link must open', () => {
  assert.deepEqual(forestPathTo('loss/contrastive/in-batch'),
    ['loss', 'loss/contrastive', 'loss/contrastive/in-batch'])
  assert.deepEqual(forestPathTo('__proto__'), ['__proto__'])
  assert.deepEqual(forestPathTo('  '), [])
})

test('scope is the run array itself — a run outside it contributes nothing', () => {
  // The whole reason this model takes runs rather than fetching: there is no second scope to drift.
  const all = [run('in', { 'loss/x': tag(1) }), run('out', { 'data/y': tag(1) })]
  const scoped = buildConceptForest(all.slice(0, 1))
  assert.deepEqual(scoped.roots, ['loss'])
  assert.equal(scoped.totals.runs, 1)
  assert.deepEqual(buildConceptForest(all).roots, ['data', 'loss'])
})

test('malformed input degrades to an empty forest instead of throwing', () => {
  for (const input of [null, undefined, 'runs', [null, 7, 'x']]) {
    const forest = buildConceptForest(input)
    assert.deepEqual(forest.roots, [])
    assert.deepEqual(visibleForestRows(forest, new Set()), [])
  }
  assert.deepEqual(visibleForestRows(null, new Set()), [])
  assert.deepEqual(visibleForestRows({ nodes: {}, roots: ['ghost'] }, new Set()), [])
})
