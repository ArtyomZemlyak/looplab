// Unit tests for the shared prototype-safe concept-id helpers. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import { canonicalId, normalizeConceptId, conceptMap } from '../src/conceptId.js'

test('canonicalId applies the rename map', () => {
  assert.equal(canonicalId('raw', { raw: 'loss/x' }), 'loss/x')
  assert.equal(canonicalId('loss/x', { raw: 'loss/x' }), 'loss/x')   // unmapped -> itself
  assert.equal(canonicalId('loss/x'), 'loss/x')                       // no rename map
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

test('canonicalId is null/empty-safe and a falsy mapping falls back to raw', () => {
  assert.equal(canonicalId(null), '')
  assert.equal(canonicalId(undefined), '')
  assert.equal(canonicalId('x', { x: '' }), 'x')     // empty mapping -> keep raw (old `|| raw` intent)
})

test('conceptMap is a null-prototype map', () => {
  const m = conceptMap()
  assert.equal(Object.getPrototypeOf(m), null)
  m['__proto__'] = 5                                 // real own prop on a null-proto object (no setter)
  assert.equal(m['__proto__'], 5)
  assert.equal(m['toString'], undefined)             // no inherited props to leak
})
