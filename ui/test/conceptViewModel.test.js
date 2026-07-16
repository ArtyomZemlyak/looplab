// Unit tests for the pure Concept-view model (View 1). Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  experimentsByConcept, deltaTone, fmtCell, visibleConceptRows, conceptLeaf,
  CONCEPT_COLUMNS, DEFAULT_COLUMNS,
} from '../src/conceptViewModel.js'

test('experimentsByConcept inverts, canonicalizes, dedups, keeps many-to-many', () => {
  const nc = { 0: ['loss/dcl', 'arch/moe'], 1: ['loss/dcl'], 2: ['raw-syn'] }
  const rename = { 'raw-syn': 'data/synth' }
  const out = experimentsByConcept(nc, rename)
  assert.deepEqual(out['loss/dcl'], [0, 1])          // node 0 under BOTH its concepts (not divided)
  assert.deepEqual(out['arch/moe'], [0])
  assert.deepEqual(out['data/synth'], [2])           // canonicalized through the rename map
  assert.equal(out['raw-syn'], undefined)
})

test('experimentsByConcept is null-safe', () => {
  assert.deepEqual(experimentsByConcept(), {})
  assert.deepEqual(experimentsByConcept({ 0: null }), {})
})

test('deltaTone signs', () => {
  assert.equal(deltaTone(0.2), 'up')
  assert.equal(deltaTone(-0.1), 'down')
  assert.equal(deltaTone(0), 'flat')
  assert.equal(deltaTone(null), 'flat')
})

test('fmtCell formats ints, metrics, null', () => {
  assert.equal(fmtCell(3), '3')
  assert.equal(fmtCell(0.6789), '0.679')
  assert.equal(fmtCell(null), '·')
})

test('visibleConceptRows respects expansion + order', () => {
  const tree = {
    roots: ['loss'],
    nodes: {
      loss: { children: ['loss/contrastive'] },
      'loss/contrastive': { children: ['loss/contrastive/dcl'] },
      'loss/contrastive/dcl': { children: [] },
    },
  }
  assert.deepEqual(visibleConceptRows(tree, new Set()),
    [{ id: 'loss', depth: 0, hasChildren: true }])                 // collapsed: only the root
  const rows = visibleConceptRows(tree, new Set(['loss', 'loss/contrastive']))
  assert.deepEqual(rows.map(r => r.id), ['loss', 'loss/contrastive', 'loss/contrastive/dcl'])
  assert.deepEqual(rows.map(r => r.depth), [0, 1, 2])
  assert.equal(rows[2].hasChildren, false)
})

test('conceptLeaf + column config', () => {
  assert.equal(conceptLeaf('loss/contrastive/dcl'), 'dcl')
  assert.equal(conceptLeaf('loss'), 'loss')
  assert.ok(DEFAULT_COLUMNS.every(k => CONCEPT_COLUMNS.some(c => c.key === k)))
})
