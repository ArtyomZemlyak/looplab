import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { UPSTREAM_LIVE_LABELS, upstreamHeldLabel, upstreamLiveLabel, upstreamLiveSummary } from '../src/upstreamLiveModel.js'

test('absent or malformed projections read as no live lane', () => {
  for (const value of [undefined, null, 'auto', {}, { mode: 'later' }]) {
    assert.equal(upstreamLiveSummary(value), null)
  }
})

test('malformed rows are dropped, never shown as receipts, and counts are not invented', () => {
  const out = upstreamLiveSummary({ mode: 'propose', queue: { pending: -1, total: 'x',
    rows: [null, { op: 'check', status: 'pending', idx: 0, action_id: 'a' }, { status: 'succeeded' },
      { op: 'check', idx: 1, action_id: { not: 'a string' } }] },
  authored: [{ outcome: 'drafted', seq: 1, source_node_id: 2 }, { seq: 2 }, { outcome: 'drafted', source_node_id: 3 }] })
  assert.equal(out.pending, 1)
  assert.equal(out.total, 1)
  assert.equal(out.recent.length, 1)
  assert.equal(out.authored.length, 1)
  assert.equal(out.author, false, 'only an explicit true says the author runs')
})

test('the newest rows come first and at most five', () => {
  const rows = Array.from({ length: 8 }, (_, idx) => ({ idx, op: 'check', status: 'succeeded', action_id: `a${idx}` }))
  const out = upstreamLiveSummary({ mode: 'auto', queue: { rows } })
  assert.deepEqual(out.recent.map(row => row.idx), [7, 6, 5, 4, 3])
})

test('an unknown or missing value reads as unknown, never an empty cell', () => {
  assert.equal(upstreamLiveLabel('status', 'refused'), 'refused by the lane')
  assert.equal(upstreamLiveLabel('track', 'champion'), 'champion')
  assert.equal(upstreamLiveLabel('outcome', 'brand-new'), 'unknown')
  assert.equal(upstreamLiveLabel('status', undefined), 'unknown')
})

test('a lane no engine confirmed yet says so', () => {
  assert.equal(upstreamLiveSummary({ mode: 'auto', configured: true }).configured, true)
  assert.equal(upstreamLiveSummary({ mode: 'auto' }).configured, false)
})

test('the kill switch, the held steps and the author spend read only from well-formed fields', () => {
  const out = upstreamLiveSummary({ mode: 'auto', auto_paused: true, author_spent_usd: 0.5,
    held: [{ seq: 4, op: 'advance', reason: 'rate_cap:2/h', proposal_id: 'up_x' }, { op: 'author' }, null] })
  assert.equal(out.autoPaused, true)
  assert.equal(out.authorSpentUsd, 0.5)
  assert.deepEqual(out.held.map(row => row.seq), [4])
  assert.equal(upstreamLiveLabel('held', 'advance'), 'advance held at the hourly cap')
  const old = upstreamLiveSummary({ mode: 'auto', auto_paused: 'yes', author_spent_usd: -1 })
  assert.equal(old.autoPaused, false, 'only an explicit true says the automation is stopped')
  assert.equal(old.authorSpentUsd, 0)
  assert.deepEqual(old.held, [])
})

test('a failed receipt says what its operation failed at, not always the check', () => {
  assert.equal(upstreamLiveLabel('status', 'failed', 'check'), 'check did not pass')
  assert.equal(upstreamLiveLabel('status', 'failed', 'propose'), 'proposal failed')
  assert.equal(upstreamLiveLabel('status', 'failed', 'advance'), 'advance failed')
  assert.equal(upstreamLiveLabel('status', 'failed'), 'check did not pass', 'an older row with no op')
  assert.equal(upstreamLiveLabel('status', 'refused', 'advance'), 'refused by the lane')
})

test('a held row is a cap or a refusal that retired the step', () => {
  assert.equal(upstreamHeldLabel({ op: 'advance', reason: 'rate_cap:2/h' }), 'advance held at the hourly cap')
  assert.equal(upstreamHeldLabel({ op: 'check', reason: 'refused:upstream_source_changed' }),
    'automatic check refused by the lane, not asked again')
  assert.equal(upstreamHeldLabel({ op: 'advance', reason: 'refused:upstream_evidence_changed' }),
    'automatic advance refused by the lane, not asked again')
  assert.equal(upstreamHeldLabel(null), 'unknown')
})

test('an armed mode with no engine alive reads as nobody serving the lane now', () => {
  assert.equal(upstreamLiveSummary({ mode: 'auto' }, false).idle, true)
  assert.equal(upstreamLiveSummary({ mode: 'auto' }, true).idle, false)
  assert.equal(upstreamLiveSummary({ mode: 'auto' }).idle, false, 'unknown liveness promises nothing')
  assert.equal(upstreamLiveSummary({ mode: 'auto', configured: true }, false).idle, false,
    'a configured mode already says no engine confirmed it')
})

test('the caps and the rebase marks read only from well-formed fields (doc 73 §4.3)', () => {
  const out = upstreamLiveSummary({ mode: 'auto', author_usd_cap: 2, advances_per_hour: 2, advances_last_hour: 1,
    authored: [{ seq: 1, source_node_id: 2, outcome: 'rebase_conflict', rebased: true },
      { seq: 2, source_node_id: 3, outcome: 'drafted', rebased: 'yes' }],
    held: [{ seq: 3, op: 'advance', reason: 'rate_cap:2/h', waiting: true }, { seq: 4, op: 'author', reason: 'x' }] })
  assert.equal(out.authorUsdCap, 2)
  assert.equal(out.advancesPerHour, 2)
  assert.equal(out.advancesLastHour, 1)
  assert.deepEqual(out.authored.map(row => row.rebased), [false, true], 'newest first; only true counts')
  assert.deepEqual(out.held.map(row => row.waiting), [undefined, true])
  assert.equal(upstreamLiveLabel('outcome', 'rebase_conflict'), 'its code conflicts with the newer base — skipped')
  assert.equal(upstreamLiveLabel('heldState', 'true'), 'still waiting')
  const old = upstreamLiveSummary({ mode: 'auto', author_usd_cap: -1, advances_per_hour: 1.5 })
  assert.equal(old.authorUsdCap, null)
  assert.equal(old.advancesPerHour, null)
  assert.equal(old.advancesLastHour, null)
})

test('a refused held row is retired: it carries no held state, so the panel says neither waiting nor released', () => {
  const out = upstreamLiveSummary({ mode: 'auto', held: [
    { seq: 3, op: 'advance', reason: 'rate_cap:2/h', proposal_id: 'up_x', waiting: false },
    { seq: 4, op: 'advance', reason: 'refused:upstream_base_conflict', proposal_id: 'up_x', waiting: false },
    { seq: 5, op: 'check', reason: 'refused:upstream_source_not_measured', proposal_id: 'up_y' }] })
  const [checkRow, refusedRow, capRow] = out.held
  assert.equal(refusedRow.refused, true)
  assert.equal('waiting' in refusedRow, false, 'a refusal is not "released"')
  assert.equal('waiting' in checkRow, false)
  assert.equal(capRow.refused, false)
  assert.equal(capRow.waiting, false, 'a cap row the lane let go of still reads released')
  assert.equal(upstreamHeldLabel(refusedRow), 'automatic advance refused by the lane, not asked again')
  assert.equal(upstreamHeldLabel(capRow), 'advance held at the hourly cap')
})

test('every label the live lane can draw has its Russian in ru.json', () => {
  const page = JSON.parse(fs.readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
  const labels = Object.values(UPSTREAM_LIVE_LABELS).flatMap(group => Object.values(group))
  const missing = [...labels, 'unknown'].filter(label => !Object.hasOwn(page.messages, label))
  assert.deepEqual(missing, [])
  assert.equal(page.messages[upstreamLiveLabel('heldState', 'false')], 'больше не ждёт')
})
