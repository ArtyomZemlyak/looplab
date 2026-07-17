// Unit tests for the shared prototype-safe concept-id helpers. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import { canonicalId, normalizeConceptId, conceptMap, nodeTheme } from '../src/conceptId.js'

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
