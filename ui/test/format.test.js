import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { fmtElapsedSeconds } from '../src/format.js'

test('elapsed seconds never rounds positive sub-second work down to zero', () => {
  assert.equal(fmtElapsedSeconds(0), '0s')
  assert.equal(fmtElapsedSeconds(Number.MIN_VALUE), '<1s')
  assert.equal(fmtElapsedSeconds(0.49), '<1s')
  assert.equal(fmtElapsedSeconds(0.999), '<1s')
  assert.equal(fmtElapsedSeconds(1), '1s')
  assert.equal(fmtElapsedSeconds(1.49), '1s')
  assert.equal(fmtElapsedSeconds(1.5), '2s')
  assert.equal(fmtElapsedSeconds(123.6), '124s')
})

test('elapsed seconds rejects invalid or negative measurements', () => {
  for (const value of [null, undefined, '0.5', NaN, Infinity, -Infinity, -0.1]) {
    assert.equal(fmtElapsedSeconds(value), '—')
  }
})

test('run summary surfaces share the elapsed formatter and retain a zero budget', async () => {
  const [runView, panels] = await Promise.all([
    readFile(new URL('../src/RunView.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8'),
  ])

  for (const source of [runView, panels]) {
    assert.match(source, /fmtElapsedSeconds\(evalSec\)/)
    assert.match(source, /maxEval != null/)
    assert.match(source, /fmtElapsedSeconds\(maxEval\)/)
    assert.doesNotMatch(source, /Math\.round\(evalSec\)/)
  }
})
