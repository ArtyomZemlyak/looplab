import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { sharedVite } from './_mount.js'
import { conceptEffectValue, validConceptEffect } from '../src/conceptEffect.js'
import { applyConceptPolicy, buildConceptForest } from '../src/conceptForest.js'

const effect = {
  v: 1, method: 'matched_concept_contrast', status: 'matched', reason: 'observational_not_causal',
  estimate: 0.2, mean: 0.2, low: 0.2, high: 0.2, n_pairs: 1, n_contexts: 1,
  n_with: 1, n_without: 1, n_unknown: 0, positive: 1, negative: 0, neutral: 0, pairs_omitted: 0,
  pairs: [{ with_node: 1, without_node: 0, with_attempt: 2, without_attempt: 1,
    delta: 0.2, kind: 'parent', phase: 'search' }],
}
test('contribution is unknown for missing, corrupted or inconsistent evidence', () => {
  assert.equal(conceptEffectValue(effect), 0.2)
  for (const bad of [undefined, {}, { ...effect, estimate: NaN }, { ...effect, n_pairs: true },
    { ...effect, n_with: 0 }, { ...effect, pairs: [] },
    { ...effect, pairs: [{ ...effect.pairs[0], without_node: 1 }] }]) {
    assert.equal(validConceptEffect(bad), false)
    assert.equal(conceptEffectValue(bad), null)
  }
})
test('a measured zero remains a real comparison', () => {
  const zero = { ...effect, estimate: 0, mean: 0, low: 0, high: 0, positive: 0, neutral: 1 }
  assert.equal(conceptEffectValue(zero), 0)
})
test('global subtree effect comes from union receipt, never summed leaf contributions', () => {
  const run = { run_id: 'r', source_integrity: { complete: true }, concepts: {
    'loss/a': { count: 1, effect, subtree_effects: { loss: effect, 'loss/a': effect } },
    'loss/b': { count: 1, effect, subtree_effects: { 'loss/b': effect } },
  } }
  const forest = buildConceptForest([run])
  assert.equal(forest.nodes.loss.effects.size, 1)
  assert.equal(forest.nodes.loss.effects.get('r').estimate, 0.2)
  assert.equal(buildConceptForest([{ ...run, source_integrity: { complete: false } }])
    .nodes.loss.effects.size, 0)
  const policy = { canonical: { 'loss/a': 'loss/b' }, split_sources: [] }
  const governed = applyConceptPolicy([run], policy).runs
  assert.equal(buildConceptForest(governed).nodes.loss.effects.size, 0)
  const purged = applyConceptPolicy([run], {
    canonical: { 'loss/a': null }, split_sources: [],
  }).runs
  assert.equal(buildConceptForest(purged).nodes.loss.effects.size, 0)
})
test('operator sees pair evidence and single-comparison caveat, not a causal promise', async () => {
  const vite = await sharedVite()
  const { default: Component } = await vite.ssrLoadModule('/src/ConceptEffect.jsx')
  const html = renderToStaticMarkup(React.createElement(Component, { effect }))
  assert.match(html, /\+0\.2/)
  assert.match(html, /Only one comparison/)
  assert.match(html, /not a proven ablation/)
  assert.match(html, /With #1 \(attempt 2\) \/ without #0 \(attempt 1\)/)
  assert.match(renderToStaticMarkup(React.createElement(Component, {})), /unknown/)
})
