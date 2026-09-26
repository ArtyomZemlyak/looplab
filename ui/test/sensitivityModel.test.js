// `sensitivityModel.js::sensitivityBars`, the Parameter-sensitivity panel's bars, driven directly.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { sensitivityBars } from '../src/sensitivityModel.js'

test('an unmeasured probe is absent, not a zero that erases the measurement before it', () => {
  // Critic 2026-09-26, driven: the second ablation measured nothing (a parent with no metric), and
  // `Math.abs(null)` turned both parameters into bars of 0.
  assert.deepEqual(
    sensitivityBars([{ impacts: { x: 0.31, y: 0.02 } }, { impacts: { x: null, y: null } }]),
    [{ label: 'x', value: 0.31 }, { label: 'y', value: 0.02 }])
})

test('the latest measured impact wins per parameter, magnitudes, largest first', () => {
  assert.deepEqual(
    sensitivityBars([{ impacts: { x: 0.31, y: -0.5 } }, { impacts: { x: -0.9 } }]),
    [{ label: 'x', value: 0.9 }, { label: 'y', value: 0.5 }])
  // A measured zero IS a measurement: the parameter moved nothing.
  assert.deepEqual(sensitivityBars([{ impacts: { z: 0 } }]), [{ label: 'z', value: 0 }])
})

test('no ablation, a malformed row or a non-finite value draws nothing', () => {
  assert.deepEqual(sensitivityBars(undefined), [])
  assert.deepEqual(sensitivityBars([null, { impacts: null }, { impacts: 'x' }]), [])
  assert.deepEqual(sensitivityBars([{ impacts: { a: Infinity, b: NaN, c: '0.4' } }]), [])
})
