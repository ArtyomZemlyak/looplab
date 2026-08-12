// "So make it possible to delete a whole pile of runs at once."
//
// A batch is a QUEUE of the existing one-run transaction, never a new one. Run deletion is
// operation-bound and durable — idempotency key, a generation+seq fence read from the exact row the
// operator inspected, a receipt, a recovery record that survives the tab. A bulk endpoint taking a
// list of ids would have to reinvent all of it, and would get the fences wrong first.
//
// So what is actually new is small and is all here: which of the selected runs CAN be deleted, and
// what a partly-drained queue is allowed to claim afterwards.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  bulkDeletionPlan, bulkDeletionSummary, bulkOutcomeNotice, bulkProgressLabel,
} from '../src/bulkDeleteModel.js'

const GEN = 'a'.repeat(64)
const run = (id, extra = {}) => ({ run_id: id, generation: GEN, seq: 7, label: '', ...extra })

test('the plan names what is deletable and why the rest is not', () => {
  const runs = [run('r1'), run('r2', { generation: 'short' }), run('r4', { seq: -2 })]
  const plan = bulkDeletionPlan(['r1', 'r2', 'r3', 'r4'], runs, new Map())
  assert.deepEqual(plan.ready.map(t => t.runId), ['r1'])
  assert.deepEqual(plan.blocked, [
    { runId: 'r2', reason: 'its exact deletion identity is unavailable' },
    { runId: 'r3', reason: 'not in the current run list' },
    { runId: 'r4', reason: 'its exact deletion identity is unavailable' },
  ])
})

test('a run with an unfinished deletion is never given a SECOND operation', () => {
  // That is precisely what the durable recovery record exists to prevent — submitting a competing
  // operation against a run another one already owns.
  const plan = bulkDeletionPlan(['r1'], [run('r1')], new Map([['r1', { kind: 'active' }]]))
  assert.deepEqual(plan.ready, [])
  assert.match(plan.blocked[0].reason, /already owns it/)
})

test('the fence is read from the run row, preferring the deletion generation', () => {
  const other = 'b'.repeat(64)
  const plan = bulkDeletionPlan(['r1'], [run('r1', { deletion_generation: other })], new Map())
  assert.equal(plan.ready[0].expectedGeneration, other)
  assert.equal(plan.ready[0].expectedSeq, 7)
})

test('junk in, empty plan out — never a throw inside a render', () => {
  assert.deepEqual(bulkDeletionPlan(null, null, null), { ready: [], blocked: [] })
  assert.deepEqual(bulkDeletionPlan(['', null], [], new Map()), { ready: [], blocked: [] })
})

test('the summary counts both halves, so a partly-blocked selection cannot read as whole', () => {
  const plan = bulkDeletionPlan(['r1', 'r2', 'gone'], [run('r1'), run('r2')], new Map())
  assert.equal(bulkDeletionSummary(plan), 'Delete 2 runs; 1 cannot be deleted right now.')
  assert.equal(bulkDeletionSummary({ ready: [1], blocked: [] }), 'Delete 1 run.')
  assert.match(bulkDeletionSummary({ ready: [], blocked: [1, 2] }), /None of the 2 selected runs/)
})

test('progress names the run in flight, not only a number', () => {
  // A bare "3 of 20" during a slow deletion is indistinguishable from a stall.
  const label = bulkProgressLabel({ running: true, total: 20, done: ['a', 'b'], current: 'rubert-v2' })
  assert.equal(label, 'Deleting 3 of 20 — rubert-v2…')
  assert.equal(bulkProgressLabel({ running: false, total: 20, done: [] }), '')
})

test('a batch that stops early reports BOTH what went and what did not', () => {
  // The failure mode this exists to prevent: reporting only the error, leaving the operator to
  // believe eight runs still exist that do not.
  const notice = bulkOutcomeNotice({
    total: 20, done: ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'], blocked: [],
    stoppedAt: { runId: 'run-9', reason: 'this run is still active' },
  })
  assert.equal(notice.kind, 'error')
  assert.match(notice.text, /^8 runs deleted/)
  assert.match(notice.text, /stopped at “run-9”: this run is still active/)
  assert.match(notice.text, /remaining 11 runs were not touched/)
})

test('a batch that deleted nothing says so plainly, and names the run that stopped it', () => {
  const notice = bulkOutcomeNotice({
    total: 3, done: [], blocked: [], stoppedAt: { runId: 'r1', reason: 'the lock is unavailable' },
  })
  assert.match(notice.text, /^Nothing was deleted\./)
  assert.match(notice.text, /“r1” stopped the batch: the lock is unavailable/)
})

test('a clean batch is a plain confirmation, and blocked runs are still disclosed', () => {
  assert.deepEqual(bulkOutcomeNotice({ total: 2, done: ['a', 'b'], blocked: [], stoppedAt: null }),
    { kind: 'status', retryRunId: '', text: '2 runs permanently deleted.' })
  const withBlocked = bulkOutcomeNotice({
    total: 2, done: ['a', 'b'], blocked: [{ runId: 'x' }], stoppedAt: null })
  assert.match(withBlocked.text, /1 selected run could not be deleted/)
})

test('a half-purged memory store survives the batch and carries its retry', () => {
  // THE defect: with the cascade ticked, `quiet` suppressed the per-run notice — and that notice is
  // the only surface that can offer to finish the purge, because the run's card is already gone.
  // Twenty runs, three locked stores, and the operator would never have learnt it happened.
  const notice = bulkOutcomeNotice({
    total: 3, done: ['a', 'b', 'c'], blocked: [], stoppedAt: null,
    memoryFailures: [{ runId: 'b', memory: { ok: false } }, { runId: 'c', memory: { ok: false } }],
  })
  assert.equal(notice.kind, 'error', 'a clean-looking status would hide it')
  assert.equal(notice.retryRunId, 'b', 'the retry handle is what renders the button')
  assert.match(notice.text, /3 runs permanently deleted/)
  assert.match(notice.text, /only partly removed for 2 runs/)
  assert.match(notice.text, /first: “b”/)
})

test('a batch that stops on its LAST run does not claim 0 were left untouched', () => {
  const notice = bulkOutcomeNotice({
    total: 3, done: ['a', 'b'], blocked: [], stoppedAt: { runId: 'c', reason: 'it is active' } })
  assert.ok(!/remaining 0 runs/.test(notice.text))
  assert.match(notice.text, /stopped at “c”: it is active\./, 'the sentence must end before the next')
})

test('nothing selected, nothing to say', () => {
  assert.equal(bulkOutcomeNotice({ total: 0, done: [], blocked: [], stoppedAt: null }), null)
  assert.equal(bulkOutcomeNotice(null), null)
})

test('the batch drains sequentially and re-reads the fence between deletions', () => {
  // Two properties the model cannot hold, both load-bearing:
  //   * sequential — each receipt is validated against a REFRESHED run list, and concurrent
  //     deletions would race that refresh;
  //   * the fence for run N+1 is read AFTER run N landed, from the live rows, not from the plan.
  const source = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  const at = source.indexOf('const runBulkDeletion')
  assert.ok(at > 0, 'the batch runner is gone')
  const body = source.slice(at, source.indexOf('const closeBulkDelete'))
  assert.match(body, /for \(const target of dialog\.plan\.ready\)/,
    'a for-of over the plan is what makes it sequential — Promise.all would not be')
  assert.ok(!/Promise\.all|\.map\(async/.test(body), 'the batch went concurrent')
  assert.match(body, /runsRef\.current/,
    'the fence must come from the CURRENT rows, not the ones the plan captured')
  assert.match(body, /await runDeletionRequest\(intent, \{ initialRequest: true, quiet: true \}\)/,
    'each run must go through the one durable transaction, awaited')
  assert.match(body, /break/, 'the batch must stop at the first failure')
})

test('the batch reuses the single-run transaction rather than a second write path', () => {
  const source = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  const at = source.indexOf('const runBulkDeletion')
  const body = source.slice(at, source.indexOf('const closeBulkDelete'))
  // The three things a deletion may not skip.
  assert.match(body, /createRunDeletionIntent\(/)
  assert.match(body, /saveRunDeletionIntent\(intent\)/)
  assert.match(body, /createIdempotencyKey\(\)/)
  assert.ok(!/submitRunDeletion\(/.test(body),
    'the batch must not call the API directly — that path skips the recovery record')
})
