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
  assert.deepEqual(out['loss/dcl'], [0, 1])
  assert.deepEqual(out['arch/moe'], [0])
  assert.deepEqual(out['data/synth'], [2])
  assert.equal(out['raw-syn'], undefined)
})

test('experimentsByConcept is null-safe and returns a prototype-free lookup', () => {
  for (const out of [experimentsByConcept(), experimentsByConcept({ 0: null })]) {
    assert.equal(Object.getPrototypeOf(out), null)
    assert.deepEqual(Object.keys(out), [])
  }
})

test('experimentsByConcept preserves hostile concept ids without prototype collisions', () => {
  const nc = JSON.parse('{"0":["__proto__","constructor"],"1":["alias","toString"]}')
  const rename = JSON.parse('{"alias":"__proto__"}')
  const out = experimentsByConcept(nc, rename)

  assert.equal(Object.getPrototypeOf(out), null)
  assert.deepEqual(out.__proto__, [0, 1])
  assert.deepEqual(out.constructor, [0])
  assert.deepEqual(out.toString, [1])
  assert.equal(Object.prototype.polluted, undefined)
})

test('experimentsByConcept fails closed on malformed records and rename targets', () => {
  const nc = {
    '00': ['leading-zero'],
    1: ['valid', '', null],
    2: 'not-an-array',
    9007199254740992: ['unsafe-number'],
  }
  const out = experimentsByConcept(nc, { valid: null })
  assert.deepEqual(Object.keys(out), [])
  assert.deepEqual(Object.keys(experimentsByConcept([], {})), [])
})

test('deltaTone signs only finite numeric metrics', () => {
  assert.equal(deltaTone(0.2), 'up')
  assert.equal(deltaTone(-0.1), 'down')
  assert.equal(deltaTone(0), 'flat')
  for (const value of [null, undefined, NaN, Infinity, -Infinity, '0.2', Symbol('metric')]) {
    assert.equal(deltaTone(value), 'flat')
  }
})

test('fmtCell formats only finite numeric metrics', () => {
  assert.equal(fmtCell(3), '3')
  assert.equal(fmtCell(0.6789), '0.679')
  for (const value of [null, undefined, NaN, Infinity, -Infinity, '3', Symbol('metric')]) {
    assert.equal(fmtCell(value), '\u00b7')
  }
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
  const collapsed = visibleConceptRows(tree, new Set())
  assert.deepEqual(collapsed, [{ id: 'loss', depth: 0, hasChildren: true }])
  assert.deepEqual(collapsed.projectionStatus, { state: 'current', reasons: [] })

  const rows = visibleConceptRows(tree, new Set(['loss', 'loss/contrastive']))
  assert.deepEqual(rows.map(row => row.id), ['loss', 'loss/contrastive', 'loss/contrastive/dcl'])
  assert.deepEqual(rows.map(row => row.depth), [0, 1, 2])
  assert.equal(rows[2].hasChildren, false)
  assert.equal(rows.projectionStatus.state, 'current')
})

test('visibleConceptRows rejects inherited phantom roots but supports own hostile ids', () => {
  const phantom = visibleConceptRows({ roots: ['constructor'], nodes: {} })
  assert.deepEqual(phantom, [])
  assert.deepEqual(phantom.projectionStatus, {
    state: 'unavailable', reasons: ['missing-node'],
  })

  const owned = JSON.parse('{"roots":["__proto__","constructor"],"nodes":{"__proto__":{"children":[]},"constructor":{"children":[]}}}')
  const rows = visibleConceptRows(owned)
  assert.deepEqual(rows.map(row => row.id), ['__proto__', 'constructor'])
  assert.equal(rows.projectionStatus.state, 'current')
})

test('visibleConceptRows terminates cycles and reports a partial projection', () => {
  const tree = {
    roots: ['a'],
    nodes: {
      a: { children: ['b'] },
      b: { children: ['a'] },
    },
  }
  const rows = visibleConceptRows(tree, new Set(['a', 'b']))
  assert.deepEqual(rows.map(row => row.id), ['a', 'b'])
  assert.equal(rows.projectionStatus.state, 'partial')
  assert.ok(rows.projectionStatus.reasons.includes('cycle'))
})

test('visibleConceptRows bounds adversarial depth without recursion', () => {
  const nodes = Object.create(null)
  const expanded = new Set()
  const depth = 2_000
  for (let i = 0; i < depth; i += 1) {
    const id = `n${i}`
    nodes[id] = { children: i + 1 < depth ? [`n${i + 1}`] : [] }
    expanded.add(id)
  }

  const rows = visibleConceptRows({ roots: ['n0'], nodes }, expanded)
  assert.ok(rows.length < depth)
  assert.equal(rows.projectionStatus.state, 'partial')
  assert.ok(rows.projectionStatus.reasons.includes('depth-limit'))
})

test('visibleConceptRows fails closed on malformed projection envelopes and children', () => {
  const unavailable = visibleConceptRows({ roots: 'root', nodes: {} })
  assert.deepEqual(unavailable, [])
  assert.equal(unavailable.projectionStatus.state, 'unavailable')

  const partial = visibleConceptRows({
    roots: ['root'],
    nodes: { root: { children: ['missing', 'constructor'] } },
  }, new Set(['root']))
  assert.deepEqual(partial, [{ id: 'root', depth: 0, hasChildren: false }])
  assert.equal(partial.projectionStatus.state, 'partial')
  assert.deepEqual(partial.projectionStatus.reasons, ['missing-node'])
})

test('conceptLeaf + column config', () => {
  assert.equal(conceptLeaf('loss/contrastive/dcl'), 'dcl')
  assert.equal(conceptLeaf('loss'), 'loss')
  assert.ok(DEFAULT_COLUMNS.every(key => CONCEPT_COLUMNS.some(column => column.key === key)))
})
