// THE SERVER MEASURED IT AND THE CLIENT THREW IT AWAY.
//
// `serve/attention.py` builds a `train_overrun` detail from the engine's own numbers — "Experiment
// #6 is projected to overrun by 4.2h beyond the deadline grace against a 10.0h wall" — and
// `normalizeRunAttention` did `const [title, detail, actionLabel] = COPY[kind]`, reading the copy
// table and never the payload. The operator was told an experiment would miss its wall without
// being told BY HOW MUCH, on both the attention card and the desktop notification.
//
// The marker that stood here prescribed publishing "two bounded NUMERIC hour fields" and formatting
// them in the client. That was the wrong fix: the server had already done the arithmetic. What was
// missing was a reader.
//
// THE UNTRUSTED-PROSE BOUNDARY IS WHY THIS IS AN ALLOW-LIST AND NOT A BLANKET PASSTHROUGH. The
// server states the rule at that row: its numbers are "the engine's OWN measurement of its own
// stage — a span, an ETA and a declared wall — not model-authored log prose, so they may ride in
// the envelope where the health family's verdict text deliberately may not". `train_monitor` IS
// that other family — its detail quotes an LLM verdict about a candidate's own log — so it must
// keep the table copy no matter what a server sends.
import assert from 'node:assert/strict'
import test from 'node:test'

import { MEASURED_DETAIL_KINDS, normalizeRunAttention } from '../src/attentionModel.js'

const GEN = 'a'.repeat(64)
const row = (over = {}) => ({
  id: 'c'.repeat(64), kind: 'train_overrun', severity: 'warning',
  run_id: 'run-1', generation: GEN, seq: 7, created: 1,
  active: true, browser: true, derived: false, ...over,
})
const MEASURED = 'Experiment #6 is projected to overrun by 4.2h beyond the deadline grace '
  + 'against a 10.0h wall. It will be killed with nothing to show unless the wall is raised.'

test('a measured detail from the server REACHES the item', () => {
  const item = normalizeRunAttention(row({ detail: MEASURED }))
  assert.ok(item, 'precondition: the row normalizes')
  assert.equal(item.detail, MEASURED,
    'the server did the arithmetic; discarding it tells the operator an experiment will miss its '
    + 'wall without saying by how much')
})

test('train_monitor NEVER takes a server detail — the prose boundary', () => {
  const item = normalizeRunAttention(row({
    kind: 'train_monitor',
    detail: 'the model says the loss is pinned at ~23.0 and this run is wasted',
  }))
  assert.ok(item)
  assert.ok(!item.detail.includes('the model says'),
    "that family's detail quotes an LLM verdict about a candidate's own log — it may never ride "
    + 'in the envelope, and an allow-list is the only thing keeping it out')
  assert.ok(item.detail.includes('live-log monitor'), 'it keeps the copy-table sentence')
})

test('an ABSENT detail falls back to the copy table', () => {
  const item = normalizeRunAttention(row())
  assert.ok(item.detail.includes('projected to be killed by its own deadline'),
    'an older server sends no detail; the fallback row exists for exactly that and must survive')
})

test('a detail with CONTROL characters is refused, not rendered', () => {
  const item = normalizeRunAttention(row({ detail: 'overrun by 4.2h\u001b[31m' }))
  assert.ok(!item.detail.includes('\u001b'),
    'the numbers are trusted; the STRING is still untrusted text and gets the same treatment as '
    + 'every other envelope string')
  assert.ok(item.detail.includes('projected to be killed'), 'and it falls back rather than blanking')
})

test('an OVER-LONG detail is refused', () => {
  const item = normalizeRunAttention(row({ detail: 'x'.repeat(401) }))
  assert.ok(!item.detail.startsWith('xxx'), 'bounded like every other envelope string')
})

test('an EMPTY or whitespace detail falls back rather than blanking the card', () => {
  assert.ok(normalizeRunAttention(row({ detail: '   ' })).detail.includes('projected to be killed'))
  assert.ok(normalizeRunAttention(row({ detail: '' })).detail.includes('projected to be killed'))
})

test('the allow-list holds exactly the engine-measured kind', () => {
  assert.deepEqual([...MEASURED_DETAIL_KINDS], ['train_overrun'],
    'adding a kind here is a TRUST decision, not a formatting one — the test exists so it is made '
    + 'deliberately')
})
