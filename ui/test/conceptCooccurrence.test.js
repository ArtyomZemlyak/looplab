// The global concept map's ONE non-derivable relation: which concepts a lab studied TOGETHER.
//
// This landed when `engine/concept_capsules.py::portfolio_concept_graph` was removed. That function
// also called itself the global cross-run concept map, and it read a different corpus — run-end
// capsules, 3 of them against the 15 tagged runs this list shows. The scope rule that survives is the
// one these tests drive: the pairs are folded from the SAME run array the tree is folded from, so a
// filter narrows both or neither, and no request can hand this view a population the list is not
// showing.
//
// `tests/test_concept_map.py` drives the same rules over `search/concept_lens.py::project_concept_map`
// for the Python population (`runs` here is its `n_runs`). A rule that holds in one and not the other
// is a bug in whichever half moved.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  buildConceptCooccurrence, buildConceptForest, partnersOf,
} from '../src/conceptForest.js'

const run = (run_id, ids, extra = {}) => ({
  run_id,
  concepts: Object.fromEntries(ids.map(id => [id, { count: 1, best_metric: null }])),
  task_id: 'blob_classification', direction: 'max', ...extra,
})

test('a pair is weighted by DISTINCT runs and needs the floor to become an edge', () => {
  const runs = [
    run('r1', ['loss/contrastive/dcl', 'arch/moe']),
    run('r2', ['loss/contrastive/dcl', 'arch/moe']),
    run('r3', ['loss/contrastive/dcl', 'data/aug']),
  ]
  const cooc = buildConceptCooccurrence(runs)

  assert.deepEqual(cooc.pairs, [{ a: 'arch/moe', b: 'loss/contrastive/dcl', runs: 2 }])
  assert.equal(cooc.minRuns, 2)
  // The single-run pair is COUNTED, not dropped in silence: a view that shows nothing must be able to
  // say whether nothing co-occurred or nothing reached the floor.
  assert.equal(cooc.pairCandidates, 2)
  assert.equal(buildConceptCooccurrence(runs, { minRuns: 1 }).pairs.length, 2)
})

test('one run cannot lift a pair over the floor by naming a concept twice', () => {
  // Two raw spellings that canonicalize to one id is the real way this happens, and
  // `runConceptEntries` merges them — so the weight stays a count of RUNS, not of tags.
  const runs = [{
    run_id: 'r1', task_id: 't', direction: 'max',
    concepts: {
      'Loss/DCL': { count: 3, best_metric: null }, 'loss/dcl': { count: 2, best_metric: null },
      'arch/moe': { count: 4, best_metric: null },
    },
  }]
  const cooc = buildConceptCooccurrence(runs)
  assert.deepEqual(cooc.pairs, [])
  assert.equal(cooc.pairCandidates, 1)
  assert.deepEqual(buildConceptCooccurrence(runs, { minRuns: 1 }).pairs,
    [{ a: 'arch/moe', b: 'loss/dcl', runs: 1 }])
})

test('the pairs are exactly the population handed in — the scope is not negotiable here', () => {
  // The property the removal of `portfolio_concept_graph` exists to protect. `buildConceptCooccurrence`
  // has no fetch, no store and no default corpus: narrowing `runs` (a project filter, a Compare
  // selection) can only narrow the pairs, and a run the list is not showing can never contribute one.
  const all = [
    run('r1', ['a/x', 'b/y']), run('r2', ['a/x', 'b/y']), run('r3', ['c/z', 'd/w']),
  ]
  assert.deepEqual(buildConceptCooccurrence(all).pairs,
    [{ a: 'a/x', b: 'b/y', runs: 2 }])
  const filtered = all.filter(row => row.run_id !== 'r2')
  assert.deepEqual(buildConceptCooccurrence(filtered).pairs, [])
  assert.equal(buildConceptCooccurrence(filtered).pairCandidates, 2)
})

test('a malformed or untagged run contributes no pair and does not crash the fold', () => {
  const cooc = buildConceptCooccurrence([
    run('r1', ['a/x', 'b/y']), run('r2', ['a/x', 'b/y']),
    { run_id: 'r3' }, { run_id: 'r4', concepts: null }, null, 'nonsense',
    { run_id: 'r5', concepts: { '///': { count: 1 } } },
  ])
  assert.deepEqual(cooc.pairs, [{ a: 'a/x', b: 'b/y', runs: 2 }])
})

test('an agent-authored prototype key cannot reach the prototype chain through the pair map', () => {
  // Concept ids are LLM-authored, and this fold builds a keyed map out of them. `conceptId.js` states
  // the rule; a pair key is one more place it has to hold.
  const cooc = buildConceptCooccurrence([
    run('r1', ['__proto__/x', 'constructor/y']), run('r2', ['__proto__/x', 'constructor/y']),
  ], { minRuns: 1 })
  assert.equal(cooc.pairs.length, 1)
  assert.equal(cooc.pairs[0].a, '__proto__/x')
  assert.equal(({}).x, undefined)
})

test('a materialized ancestor never pairs, because no run named it', () => {
  // `loss` exists because `loss/dcl` spells it. Letting it pair would attribute a child's partners to
  // a grouping row nobody authored — the same rule that keeps `directExperiments` off ancestors.
  const cooc = buildConceptCooccurrence([
    run('r1', ['loss/dcl', 'arch/moe']), run('r2', ['loss/dcl', 'arch/moe']),
  ], { minRuns: 1 })
  const ids = new Set(cooc.pairs.flatMap(pair => [pair.a, pair.b]))
  assert.ok(ids.has('loss/dcl') && !ids.has('loss'))
  assert.deepEqual(partnersOf(cooc, 'loss'), [])
})

test('partnersOf answers for the exact id, strongest first', () => {
  const cooc = buildConceptCooccurrence([
    run('r1', ['a/x', 'b/y', 'c/z']), run('r2', ['a/x', 'b/y', 'c/z']), run('r3', ['a/x', 'b/y']),
  ])
  assert.deepEqual(partnersOf(cooc, 'a/x'), [{ id: 'b/y', runs: 3 }, { id: 'c/z', runs: 2 }])
  assert.deepEqual(partnersOf(cooc, 'A/X'), partnersOf(cooc, 'a/x'))   // canonical boundary
  assert.deepEqual(partnersOf(cooc, ''), [])
  assert.deepEqual(partnersOf(null, 'a/x'), [])
})

test('a contract-violating floor falls back instead of opening the map', () => {
  // The floor arrives from a control and, eventually, from a restored view. A junk value must not
  // evaluate as "no floor", which would publish every single-run coincidence as an edge.
  const runs = [run('r1', ['a/x', 'b/y'])]
  for (const bad of [0, -1, 1.5, NaN, null, undefined, '2', {}]) {
    assert.deepEqual(buildConceptCooccurrence(runs, { minRuns: bad }).pairs, [], String(bad))
    assert.equal(buildConceptCooccurrence(runs, { minRuns: bad }).minRuns, 2)
  }
})

test('a complete population says so, and a bounded one keeps its loss UNKNOWN', () => {
  const small = buildConceptCooccurrence([run('r1', ['a/x', 'b/y']), run('r2', ['a/x', 'b/y'])])
  assert.equal(small.pairSourceComplete, true)
  assert.equal(small.pairSourceNodesPruned, 0)
  assert.equal(small.pairsOutsideProjectionUnknown, false)

  // 600 distinct tagged ids over two runs: past the 512-node pairing bound, so some ids are pruned
  // and every pair incident to them was never materialized. That count is UNKNOWN, never zero.
  const wide = [0, 1].map(index => run(`r${index}`,
    Array.from({ length: 600 }, (_, i) => `axis/c${String(i).padStart(4, '0')}`)))
  const bounded = buildConceptCooccurrence(wide, { minRuns: 1 })
  assert.equal(bounded.pairSourceComplete, false)
  assert.equal(bounded.pairSourceNodesIncluded, 512)
  assert.equal(bounded.pairSourceNodesPruned, 88)
  assert.equal(bounded.pairsOutsideProjectionUnknown, true)
  assert.ok(bounded.pairs.length <= 2000)
  assert.ok(bounded.pairsOmitted > 0)
})

test('the fold reuses a forest it is handed rather than building a second one', () => {
  // The component memoizes the forest; recomputing it here would double the per-poll cost and, worse,
  // let the pairs be scoped to a different run array than the tree beside them.
  const runs = [run('r1', ['a/x', 'b/y']), run('r2', ['a/x', 'b/y'])]
  const forest = buildConceptForest(runs)
  assert.deepEqual(buildConceptCooccurrence(runs, { forest }).pairs,
    buildConceptCooccurrence(runs).pairs)
  // A forest built from a WIDER array is a real mistake, and it must not widen the pairs: the run
  // rows are what the pairs are counted from, so `runs` alone decides the population.
  const wider = buildConceptForest([...runs, run('r3', ['c/z', 'd/w'])])
  assert.deepEqual(buildConceptCooccurrence(runs, { forest: wider }).pairs,
    [{ a: 'a/x', b: 'b/y', runs: 2 }])
})
