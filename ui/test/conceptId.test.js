// Unit tests for the shared prototype-safe concept-id helpers. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import { canonicalId, conceptMap } from '../src/conceptId.js'

test('canonicalId applies the rename map', () => {
  assert.equal(canonicalId('raw', { raw: 'loss/x' }), 'loss/x')
  assert.equal(canonicalId('loss/x', { raw: 'loss/x' }), 'loss/x')   // unmapped -> itself
  assert.equal(canonicalId('loss/x'), 'loss/x')                       // no rename map
})

test('canonicalId never reads an inherited rename entry', () => {
  // "constructor" is an inherited property of every plain object; the guard must return the raw id,
  // NOT Object's constructor function.
  assert.equal(canonicalId('constructor', {}), 'constructor')
  assert.equal(canonicalId('toString', {}), 'toString')
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
