import test from 'node:test'
import assert from 'node:assert/strict'
import { upstreamLiveLabel, upstreamLiveSummary } from '../src/upstreamLiveModel.js'

test('absent or malformed projections read as no live lane', () => {
  for (const value of [undefined, null, 'auto', {}, { mode: 'later' }]) {
    assert.equal(upstreamLiveSummary(value), null)
  }
})

test('malformed rows are dropped, never shown as receipts, and counts are not invented', () => {
  const out = upstreamLiveSummary({ mode: 'propose', queue: { pending: -1, total: 'x',
    rows: [null, { op: 'check', status: 'pending', idx: 0 }, { status: 'succeeded' }] },
  authored: [{ outcome: 'drafted', seq: 1 }, { seq: 2 }] })
  assert.equal(out.pending, 1)
  assert.equal(out.total, 1)
  assert.equal(out.recent.length, 1)
  assert.equal(out.authored.length, 1)
  assert.equal(out.author, false, 'only an explicit true says the author runs')
})

test('the newest rows come first and at most five', () => {
  const rows = Array.from({ length: 8 }, (_, idx) => ({ idx, op: 'check', status: 'succeeded' }))
  const out = upstreamLiveSummary({ mode: 'auto', queue: { rows } })
  assert.deepEqual(out.recent.map(row => row.idx), [7, 6, 5, 4, 3])
})

test('labels read in both languages and an unknown value reads as itself', () => {
  assert.equal(upstreamLiveLabel('status', 'refused', true), 'отклонено')
  assert.equal(upstreamLiveLabel('track', 'champion', false), 'champion')
  assert.equal(upstreamLiveLabel('outcome', 'brand-new', true), 'brand-new')
})
