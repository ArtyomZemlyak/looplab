import test from 'node:test'
import assert from 'node:assert/strict'
import { analyze, verdict, buildModelCard, toMarkdown } from '../src/report.js'

const node = (id, metric, extra = {}) => ({ id, metric, status: 'evaluated', feasible: true,
  parent_ids: id ? [0] : [], idea: { params: {} },
  metric_provenance: { comparability: { keys: { measured: 'same-inputs' } } }, ...extra })
const run = (over = {}) => ({ run_id: 'evidence', direction: 'min', best_node_id: 1,
  nodes: { 0: node(0, 10), 1: node(1, 7) }, ...over })
const result = state => verdict(state, analyze(state))

test('the verdict follows the engine-selected result rather than the numeric frontier', () => {
  const state = run({ nodes: { 0: node(0, 10), 1: node(1, 12), 2: node(2, 1) } })
  const v = result(state)
  assert.equal(v.outcome, 'regressed')
  assert.equal(v.gain, 2)
  assert.match(v.headline, /Selected #1: evaluation score 12/)
  assert.match(v.headline, /worse by 2/)
  assert.equal(buildModelCard(state).champion.node_id, 1)
  assert.equal(buildModelCard(state).verdict, v.headline)
  assert.ok(toMarkdown(state).includes(v.headline))
})

test('score improvement requires matching recorded conditions and a known direction', () => {
  const matching = result(run())
  assert.equal(matching.outcome, 'improved')
  assert.match(matching.headline, /evaluation score is better by 3/)
  for (const state of [
    run({ direction: null }), run({ objective_key: 'accuracy' }),
    run({ source_integrity: { complete: false } }),
    run({ nodes: { 0: node(0, 10), 1: node(1, 7, { metric_provenance: null }) } }),
    run({ nodes: { 0: node(0, 10), 1: node(1, 7, {
      metric_provenance: { comparability: { keys: { measured: 'other-inputs' } } } }) } }),
  ]) {
    const v = result(state)
    assert.equal(v.outcome, 'uncompared')
    assert.equal(v.gain, null)
    assert.match(v.headline, /Improvement.*not established/)
    assert.doesNotMatch(v.headline, /better by|robust/)
  }
})

test('confirmation means are reported separately from comparable evaluation scores', () => {
  const state = run({ nodes: { 0: node(0, 10, { confirmed_mean: 100 }),
    1: node(1, 7, { confirmed_mean: 200, confirmed_std: 0, confirmed_seeds: 3 }) } })
  const v = result(state)
  assert.match(v.headline, /confirmation mean 200/)
  assert.match(v.headline, /evaluation score is better by 3/)
  assert.equal(v.robustness, 'repeat-checked')
  assert.match(v.nextStep, /do not establish generalization or statistical significance/)
  assert.doesNotMatch(v.headline, /robust|single-seed/)
  const card = buildModelCard(state)
  assert.equal(card.deterministic_verdict.next_step, v.nextStep)
  assert.ok(toMarkdown(state).includes(`**Next step:** ${v.nextStep}`))
})

test('one result and a one-seed mean cannot establish an improvement or repeated evidence', () => {
  const v = result(run({ best_node_id: 0, nodes: { 0: node(0, 0, {
    confirmed_mean: 0, confirmed_std: 0, confirmed_seeds: 1 }) } }))
  assert.equal(v.outcome, 'baseline')
  assert.equal(v.robustness, 'mean recorded')
  assert.match(v.headline, /first eligible experiment/)
  assert.match(v.nextStep, /multiple seeds/)
  assert.ok(v.caveats.some(c => c.kind === 'single-seed'))
  assert.doesNotMatch(v.headline, /No improvement|robust/)
})

test('missing, excluded, retired and nonfinite selected results do not become winners', () => {
  for (const selected of [null, node(1, NaN), node(1, 7, { status: 'pending' }),
    node(1, 7, { feasible: false }), node(1, 7, { tombstoned: true })]) {
    const state = run({ nodes: { 0: node(0, 10), ...(selected ? { 1: selected } : {}) } })
    const v = result(state)
    assert.equal(v.outcome, 'none')
    assert.equal(v.best, null)
    assert.match(v.headline, /No completed eligible result is selected/)
    assert.doesNotMatch(v.headline, /every evaluated node violated/)
    assert.equal(buildModelCard(state).champion, null)
  }
})
