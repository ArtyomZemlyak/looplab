// The browser half of the RUN-CONSTANT split — `nodeProjection.js::runConstantConcepts`, the rule the
// DAG's on-node chips read. It mirrors `search/concept_lens.py::run_constant_split` the way
// conceptForest.js mirrors project_concept_map: one vocabulary, two runtimes.
//
// Driven, not pinned: every case below builds a real wire-state shape and folds it. The v9 case is the
// shape measured off `runs/rubertlite-dr-unified-v9` on 2026-08-17 — eight experiments, forty-eight tag
// slots, forty of them the same five ids on every node — and the v8 case is the NEGATIVE CONTROL,
// sixteen classifier-tagged experiments whose intersection is empty and whose rendering must not move.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { runConstantConcepts, authoritativeNodeConcepts } from '../src/nodeProjection.js'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')
const sourceOf = name => readFileSync(join(SRC, name), 'utf8')

const BASE = ['data/esci', 'eval/recall_at_k', 'loss/contrastive/dcl/nll_cos',
  'regularization/rdrop/symmetric-kl', 'training/negative_mining']

// v9's eight memberships exactly as the fold materializes them (base + each node's authored delta).
const V9 = {
  0: [...BASE],
  1: [...BASE, 'training/epochs'],
  2: [...BASE, 'loss/contrastive/dcl/hard-negative'],
  3: [...BASE, 'training/batch_size'],
  4: [...BASE],
  5: [...BASE, 'negative-mining/teacher-mined', 'training/epochs'],
  6: [...BASE, 'negative-mining/bm25-lexical', 'training/epochs'],
  7: [...BASE, 'training/epochs'],
}

const stateOf = (concepts, extra = {}) => ({
  nodes: Object.fromEntries(Object.keys(concepts).map(id => [id, { id: Number(id) }])),
  node_concepts: concepts,
  aborted_nodes: [],
  ...extra,
})

test('the run-wide half of v9 is its five constants, and nothing else', () => {
  const constant = runConstantConcepts(stateOf(V9))
  assert.deepEqual([...constant].sort(), [...BASE].sort())
  // 40 of the 48 slots are run-wide; 8 stay on the experiments.
  const slots = Object.values(V9).reduce((n, ids) => n + ids.length, 0)
  const runWide = Object.values(V9).reduce((n, ids) => n + ids.filter(c => constant.has(c)).length, 0)
  assert.equal(slots, 48)
  assert.equal(runWide, 40)
})

test('the experiments that carry NOTHING of their own become visible', () => {
  const constant = runConstantConcepts(stateOf(V9))
  const bare = Object.entries(V9).filter(([, ids]) => ids.every(c => constant.has(c))).map(([id]) => Number(id))
  // Node 0 is the run's hard-negative SCALING experiment and node 4 its baseline reproduction; today
  // both wear five chips and read as classified. The split says what is true: no recorded tag
  // separates them from each other or from the run.
  assert.deepEqual(bare, [0, 4])
})

test('a run whose tags already distinguish makes NO claim, so its chips do not move', () => {
  // v8's shape: every node classifier-tagged, no id on all sixteen.
  const v8 = {
    0: ['loss/contrastive/dcl', 'mining/hard_negative'],
    1: ['loss/contrastive/dcl', 'loss/uniformity'],
    2: ['optimizer/adamw', 'scheduler/onecycle'],
    3: ['loss/contrastive/nllcos', 'regularization/rdrop'],
  }
  assert.equal(runConstantConcepts(stateOf(v8)).size, 0)
})

test('one unclassified experiment refuses the claim — absence is not universality', () => {
  const withUntagged = stateOf({ ...V9 })
  withUntagged.nodes['8'] = { id: 8 }        // an experiment nobody classified
  assert.equal(runConstantConcepts(withUntagged).size, 0)
})

test('one WITHHELD row refuses the claim — a degraded membership is a subset, not the truth', () => {
  // A per-node materialization receipt makes `authoritativeNodeConcepts` withhold that row, and the
  // fold then meets that experiment with no membership at all — the SAME ground the untagged case
  // refuses on, which is why there is no second guard for it. A claim about EVERY experiment cannot
  // be made over a population one row short, whichever way the row went missing.
  const degraded = stateOf({ ...V9 }, {
    node_concept_materialization_receipts: { 3: { status: 'partial', reasons: ['invalid_concept_id'] } },
  })
  assert.equal(authoritativeNodeConcepts(degraded).withheld, 1)
  assert.equal(runConstantConcepts(degraded).size, 0)
})

test('a one-experiment run makes no claim — variation needs something to vary against', () => {
  assert.equal(runConstantConcepts(stateOf({ 0: [...BASE] })).size, 0)
})

test('a tombstoned or aborted experiment is outside the population', () => {
  const live = stateOf({ ...V9 })
  live.nodes['9'] = { id: 9, tombstoned: true }      // no membership, but not a live experiment
  live.aborted_nodes = [10]
  live.nodes['10'] = { id: 10 }
  assert.deepEqual([...runConstantConcepts(live)].sort(), [...BASE].sort())
})

test('the DAG orders the chips by the split and never drops one', () => {
  const dag = sourceOf('Dag.jsx')
  // The strip renders `conceptTags`, which is `allConceptTags` re-ORDERED — both halves are spread in,
  // so a concept cannot be lost by the reorder. A regression here is a chip the operator stops seeing.
  assert.ok(dag.includes(
    '[...allConceptTags.filter(c => !runConstant.has(c)), ...allConceptTags.filter(c => runConstant.has(c))]'),
  'the DAG must reorder by the split, keeping BOTH halves')
  assert.ok(dag.includes('runConstantConcepts'), 'the DAG must read the shared rule, not re-derive one')
  // …and the run-wide chips are LABELLED, so the strip cannot be re-read as "this experiment is about ESCI".
  assert.ok(dag.includes("carried by every experiment in this run"))
})
