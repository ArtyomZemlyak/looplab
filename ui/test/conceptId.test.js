// Unit tests for the shared prototype-safe concept-id helpers. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import { canonicalId, normalizeConceptId, conceptMap, nodeTheme } from '../src/conceptId.js'
import { conceptMaterializationStatus } from '../src/nodeProjection.js'

test('canonicalId applies the rename map', () => {
  assert.equal(canonicalId('raw', { raw: 'loss/x' }), 'loss/x')
  assert.equal(canonicalId('loss/x', { raw: 'loss/x' }), 'loss/x')   // unmapped -> itself
  assert.equal(canonicalId('loss/x'), 'loss/x')                       // no rename map
  assert.equal(canonicalId(' /Regularization/R Drop/ '), 'regularization/r-drop')
  assert.equal(canonicalId(' Raw ID ', { 'raw-id': ' Canonical/ID ' }), 'canonical/id')
  assert.equal(canonicalId('a', { a: 'b', b: 'c' }), 'c')
})

test('canonicalId normalizes exactly like the server concept_id() (one vocabulary)', () => {
  // trim, case-fold, spaces->hyphens, strip leading/trailing slashes — so the client keys the SAME ids
  // the /concepts frame ships (which the server normalized).
  assert.equal(canonicalId('Regularization/R-Drop'), 'regularization/r-drop')
  assert.equal(canonicalId('  Data Augmentation '), 'data-augmentation')
  assert.equal(canonicalId('/loss/'), 'loss')
  assert.equal(normalizeConceptId('A B/C D'), 'a-b/c-d')
  // a rename retargets AND the result is normalized; the rename can be keyed by raw or normalized id
  assert.equal(canonicalId('Loss/Old', { 'loss/old': 'loss/new' }), 'loss/new')
})

test('concept id charset matches the replay and search gates', () => {
  for (const value of ['loss/decoupled-contrastive', 'hyperparameter/learning-rate',
    'данные/размер', 'architecture/resnet50', 'loss/r-drop', 'a/b_c.d', 'loss/x y']) {
    assert.ok(normalizeConceptId(value), value)
  }
  for (const value of ['a/b#c==', 'loss/💥', '<script>', 'a/..', '', 'a//b', '   ',
    'B3czR8YJ74OGBOyfVzhZ#Ea5og4_Pq3dkVsLy9ooaIRjQffav']) {
    assert.equal(normalizeConceptId(value), '', value)
    assert.equal(canonicalId(value), '', value)
  }
  assert.equal(normalizeConceptId(7), '')
  assert.equal(normalizeConceptId(null), '')
})

test('canonicalId never reads an inherited rename entry (returns the normalized id, not an Object member)', () => {
  // "constructor"/"toString"/"__proto__" are inherited members of every plain object; the guard must
  // return the NORMALIZED id string, never Object.prototype.constructor/toString.
  assert.equal(canonicalId('constructor', {}), 'constructor')
  assert.equal(canonicalId('toString', {}), 'tostring')              // normalized (lower-cased)
  assert.equal(canonicalId('__proto__', {}), '__proto__')
  // a real own-mapping still wins
  const rn = Object.create(null); rn.constructor = 'loss/c'
  assert.equal(canonicalId('constructor', rn), 'loss/c')
})

test('canonicalId fails closed on malformed ids and rename chains', () => {
  assert.equal(canonicalId(null), '')
  assert.equal(canonicalId(undefined), '')
  assert.equal(canonicalId('x', { x: null }), 'x')
  assert.equal(canonicalId('x', { x: '' }), '')
  assert.equal(canonicalId('x', { x: 'y', y: 'x' }), '')
  assert.equal(canonicalId('x//y'), '')
  assert.equal(canonicalId(`x${String.fromCharCode(0)}y`), '')
  assert.equal(canonicalId('a/'.repeat(12) + 'z'), '')
  const chain = Object.fromEntries(Array.from({ length: 18 }, (_, i) => [`c${i}`, `c${i + 1}`]))
  assert.equal(canonicalId('c0', chain), '')
})

test('conceptMap is a null-prototype map', () => {
  const m = conceptMap()
  assert.equal(Object.getPrototypeOf(m), null)
  m['__proto__'] = 5                                 // real own prop on a null-proto object (no setter)
  assert.equal(m['__proto__'], 5)
  assert.equal(m['toString'], undefined)             // no inherited props to leak
})

test('nodeTheme matches the server legacy-theme then first-concept-axis contract', () => {
  assert.equal(nodeTheme({ idea: { theme: 'legacy', concepts: ['loss/contrastive'] } }), 'legacy')
  assert.equal(nodeTheme({ idea: { theme: null, concepts: [' loss/contrastive ', 'architecture/moe'] } }), 'loss')
  assert.equal(nodeTheme({ idea: { concepts: ['', null, ' architecture/moe'] } }), 'architecture')
  assert.equal(nodeTheme({ idea: { concepts: [] } }), null)
  assert.equal(nodeTheme({ idea: { concepts: 'loss/contrastive' } }), null)
  assert.equal(nodeTheme(null), null)
})

test('nodeTheme prefers folded post-rename concepts and preserves explicit untagged truth', () => {
  const node = { id: 7, idea: { theme: 'stale-theme', concepts: ['stale/authored'] } }
  assert.equal(nodeTheme(node, {
    node_concepts: { 7: ['Loss/Old', 'architecture/moe'] },
    concept_consolidation: { 'loss/old': 'data/aug' },
  }), 'architecture', 'the deterministic primary axis is sorted after canonical renames')
  assert.equal(nodeTheme(node, { node_concepts: { 7: [] } }), null,
    'an explicit folded empty row must not resurrect frozen authoring')
  assert.equal(nodeTheme(node, { node_concepts: { 8: ['data/aug'] } }), 'stale-theme',
    'a genuinely missing row retains legacy compatibility')
  assert.equal(nodeTheme({ id: 8, idea: { concepts: [] } }, {
    node_concepts: { 8: ['optimization/adam'] },
  }), 'optimization', 'delta-authored nodes need no full idea.concepts list')
})

test('materialization receipts gate every authoritative concept projection', () => {
  const nodes = {
    1: { id: 1, idea: { theme: 'legacy', concepts: ['legacy/fallback'] } },
    2: { id: 2, idea: { theme: 'safe' } },
    3: { id: 3, tombstoned: true },
    4: { id: 4 },
  }
  const partial = { status: 'partial', reasons: ['invalid_concept_id', 'concepts_per_node_cap'] }
  const unavailable = { status: 'unavailable', reasons: ['delta_dependency_cycle'] }
  const state = {
    nodes, node_concepts: { 1: ['loss/retained'], 2: ['data/full'], 3: ['old/deleted'], 4: ['old/aborted'] },
    node_concept_materialization_receipts: { 1: partial, 3: unavailable, 4: unavailable },
    aborted_nodes: [4],
  }

  assert.equal(conceptMaterializationStatus(state, 1), 'partial')
  assert.equal(conceptMaterializationStatus(state, 2), 'complete')
  assert.equal(conceptMaterializationStatus(state), 'partial',
    'durable receipts for deleted/aborted nodes do not poison the current projection')
  assert.equal(nodeTheme(nodes[1], state), null, 'retained partial ids cannot become a theme')
  assert.equal(nodeTheme(nodes[2], state), 'data', 'a receipt on another node does not erase exact data')
  assert.equal(nodeTheme(nodes[1], {
    nodes, node_concepts: {}, node_concept_materialization_receipts: { 1: partial },
  }), null, 'a partial own receipt also blocks the legacy fallback')

  const basePartial = { ...state, run_base_concept_receipt: {
    status: 'partial', reasons: ['concepts_per_node_cap'],
  } }
  assert.equal(nodeTheme(nodes[2], basePartial), null, 'run-base partiality gates every node')
  assert.equal(conceptMaterializationStatus({ ...basePartial,
    node_concept_materialization_receipts: { 2: unavailable },
  }, 2), 'unavailable', 'an unavailable node wins over a partial run base')
})

test('materialization receipt parsing fails closed without rejecting future partial reasons', () => {
  const nodes = { 1: { id: 1 }, 2: { id: 2 } }
  assert.equal(conceptMaterializationStatus({ nodes }), 'complete')
  assert.equal(conceptMaterializationStatus({ nodes, run_base_concept_receipt: null }), 'complete')
  assert.equal(conceptMaterializationStatus({ nodes, node_concept_materialization_receipts: [] }),
    'unavailable')
  assert.equal(conceptMaterializationStatus({ nodes, node_concept_materialization_receipts: {
    9: { status: 'partial', reasons: ['concepts_per_node_cap'] },
  } }), 'unavailable', 'an orphan receipt cannot silently disappear')
  const exact = { status: 'partial', reasons: ['concepts_per_node_cap'] }
  for (const receipts of [
    { 1: exact, 9: exact },
    { 1: exact, 2: { ...exact, unexpected: true } },
    { 1: exact, 2: { status: 'partial', reasons: ['delta_dependency_cycle'] } },
  ]) {
    const state = { nodes, node_concepts: { 1: ['loss/retained'] },
      node_concept_materialization_receipts: receipts }
    assert.equal(conceptMaterializationStatus(state, 1), 'unavailable',
      'global receipt corruption must poison a node-specific projection too')
    assert.equal(nodeTheme(nodes[1], state), null)
  }
  assert.equal(conceptMaterializationStatus({
    nodes: { ...nodes, 3: { id: 3, tombstoned: true } },
    node_concept_materialization_receipts: { 3: null },
  }, 1), 'unavailable', 'malformed inactive receipts remain structural corruption')
  for (const receipt of [
    null,
    { status: 'partial', reasons: [] },
    { status: 'partial', reasons: [3] },
    { status: 'partial', reasons: ['delta_dependency_cycle'] },
  ]) assert.equal(conceptMaterializationStatus({ nodes, node_concept_materialization_receipts: {
    1: receipt,
  } }), 'unavailable')
  assert.equal(conceptMaterializationStatus({ nodes, node_concept_materialization_receipts: {
    1: { status: 'partial', reasons: ['future_bounded_reason'] },
  } }), 'partial', 'unknown reasons remain non-authoritative without breaking forward compatibility')
})
