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
  // `retryIdentity` rides beside `retryRunId` on every branch that carries a handle, and is EMPTY
  // here for the same reason the handle is: nothing needs finishing. Both are always present so a
  // consumer reads one shape — the button is gated on the handle, never on the identity's presence.
  //
  // And it NAMES the runs. This sentence also travels outside the dialog, where "2 runs deleted"
  // and "which two" are different answers and only one of them tells the operator what to do next.
  assert.deepEqual(bulkOutcomeNotice({ total: 2, done: ['a', 'b'], blocked: [], stoppedAt: null }),
    { kind: 'status', retryRunId: '', retryIdentity: { run_uid: '', memory_dir: '' },
      text: '2 runs permanently deleted: “a”, “b”.' })
  assert.match(bulkOutcomeNotice({
    total: 7, done: ['a', 'b', 'c', 'd', 'e', 'f', 'g'], blocked: [], stoppedAt: null,
  }).text, /“a”, “b”, “c”, “d”, “e” and 2 more\./, 'naming is bounded, never a wall of ids')
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
    memoryFailures: [
      { runId: 'b', memory: { ok: false, run_uid: 'u-b', memory_dir: '/m' } },
      { runId: 'c', memory: { ok: false, run_uid: 'u-c', memory_dir: '/m' } }],
  })
  assert.equal(notice.kind, 'error', 'a clean-looking status would hide it')
  assert.equal(notice.retryRunId, 'b', 'the retry handle is what renders the button')
  // The handle alone cannot finish the purge: the run is deleted, so the server can no longer read
  // its `run_uid`/`memory_dir` back and refuses to guess them. The identity has to come from the
  // receipt this notice was built from, or the button 400s on every press.
  assert.deepEqual(bulkOutcomeNotice({
    total: 1, done: ['b'], blocked: [], stoppedAt: null,
    memoryFailures: [{ runId: 'b', memory: { ok: false, run_uid: 'u-b', memory_dir: '/a' } }],
  }).retryIdentity, { run_uid: 'u-b', memory_dir: '/a' })
  assert.match(notice.text, /3 runs permanently deleted/)
  assert.match(notice.text, /only partly removed for 2 runs/)
  assert.match(notice.text, /first: “b”/)
})

test('a purge with no recorded identity offers no retry button, because none could work', () => {
  // The server refuses a body carrying NEITHER run_uid nor memory_dir
  // (`memory_purge_identity_required`), because with both empty the purge falls back to matching a
  // run's bare directory NAME — which the next run reuses. So a button offered for such a receipt
  // 400s on every press and the catch re-offers the same empty identity, forever.
  const notice = bulkOutcomeNotice({
    total: 1, done: ['b'], blocked: [], stoppedAt: null,
    memoryFailures: [{ runId: 'b', memory: { ok: false } }],
  })
  assert.equal(notice.kind, 'error', 'the half-purged store is still disclosed')
  assert.equal(notice.retryRunId, '', 'no handle, so no button')
  assert.deepEqual(notice.retryIdentity, { run_uid: '', memory_dir: '' }, 'one shape, still')
  assert.match(notice.text, /only partly removed/)
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

// -----------------------------------------------------------------------------------------------
// "Nothing was deleted" about a batch that deleted something.
//
// THE INCIDENT (2026-08-13). Two runs selected. `live-0806-features` was deleted — its receipt read
// `phase: succeeded`, `status: succeeded`, and its directory was gone. The batch said:
//
//   "Nothing was deleted. “live-0806-features” stopped the batch: the deletion did not complete."
//
// `runDeletionRequest` computed a verdict and never returned it. The batch — its only reader — saw
// `undefined` for every run, so `verdict?.outcome === 'deleted'` was never true, no run entered
// `done`, every run took the stop branch, and `verdict?.reason || 'the deletion did not complete'`
// wrote that sentence. A destructive operation that under-reports what it did is worse than one
// that fails loudly: the operator's next move is decided by it.
// -----------------------------------------------------------------------------------------------

test('the batch RETURNS its verdict, which is the only thing that made `done` reachable', () => {
  const source = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  const at = source.indexOf('const runDeletionRequest')
  assert.ok(at > 0, 'the single-run transaction is gone')
  const body = source.slice(at, source.indexOf('const confirmRunDeletion'))
  // Anchored to a whole line: a comment carrying the same words starts with `//` and cannot match.
  assert.match(body, /^\s*return verdict$/m,
    'without this the batch sees undefined for a run the server deleted')
  // ...and the verdict really is what the batch keys on, so the two halves cannot drift apart.
  const batch = source.slice(source.indexOf('const runBulkDeletion'),
    source.indexOf('const closeBulkDelete'))
  assert.match(batch, /verdict\?\.outcome === 'deleted'/)
  assert.match(batch, /state\.done\.push\(target\.runId\)/)
})

test('a stop after a success reports BOTH facts, because they are different facts', () => {
  // The branch the missing return made unreachable: `done` was empty for every batch, so every stop
  // — including one that followed a completed deletion — printed "Nothing was deleted."
  const notice = bulkOutcomeNotice({
    total: 2, done: ['live-0806-features'], blocked: [{ runId: 'rubertlite-dr-unified-v5' }],
    stoppedAt: { runId: 'run-b', outcome: 'blocked', reason: 'its storage must be resolved by hand' },
  })
  assert.match(notice.text, /^1 run deleted \(“live-0806-features”\)/)
  assert.match(notice.text, /then the batch stopped at “run-b”/)
  assert.ok(!/Nothing was deleted/.test(notice.text))
  assert.match(notice.text, /1 selected run could not be deleted/)
})

test('"stopped" and "nothing was deleted" are separated by EVIDENCE, not by tone', () => {
  // `unknown` is what the transaction returns when the receipt said `succeeded` and the tab could
  // not finish reading the refreshed list, or could not clear the recovery record. Claiming nothing
  // was deleted there is a statement about the filesystem that this branch cannot support.
  const settled = bulkOutcomeNotice({
    total: 2, done: [], blocked: [],
    stoppedAt: { runId: 'r1', outcome: 'rejected', reason: 'this run is still active' },
  })
  assert.match(settled.text, /^Nothing was deleted\./)
  assert.ok(!/outcome is not established/.test(settled.text))

  const unsettled = bulkOutcomeNotice({
    total: 2, done: [], blocked: [],
    stoppedAt: { runId: 'r1', outcome: 'unknown', reason: 'the refreshed run list could not be read' },
  })
  assert.match(unsettled.text, /^No deletion is confirmed\./)
  assert.match(unsettled.text, /check that run before assuming it still exists/)
  assert.ok(!/Nothing was deleted/.test(unsettled.text))
})

test('an unestablished stop is disclosed even when other runs DID go', () => {
  const notice = bulkOutcomeNotice({
    total: 3, done: ['a'], blocked: [],
    stoppedAt: { runId: 'b', outcome: 'unknown', reason: 'the outcome is not confirmed' },
  })
  assert.match(notice.text, /^1 run deleted \(“a”\), then the batch stopped at “b”/)
  assert.match(notice.text, /Its own outcome is not established/)
  assert.match(notice.text, /remaining 1 run were not touched|remaining 1 run was not touched/)
})

test('a stop the server says is NOT retryable does not get polled like a slow one', () => {
  // The server answers a wedged deletion with `retryable: false` — nothing in its own process will
  // ever touch what stands in the way. Treating that as "still working" is a progress bar over a
  // state machine that has stopped, so the transaction gives it its own verdict and the batch's
  // pending budget never sees it.
  const source = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  const finish = source.slice(source.indexOf('const finishRunDeletionReceipt'),
    source.indexOf('const runDeletionRequest'))
  assert.match(finish, /receipt\.retryable === false/)
  assert.match(finish, /outcome: 'blocked'/)
  const batch = source.slice(source.indexOf('const runBulkDeletion'),
    source.indexOf('const closeBulkDelete'))
  assert.match(batch, /verdict\?\.outcome === 'pending'\s*\n?\s*&& attempt < BULK_DELETION_PENDING_POLLS/,
    'only a genuinely pending verdict may be polled')
  // Only an explicit `false` means it: an older server that sends no flag keeps retrying.
  assert.match(source, /value\.retryable === false \? false : true/)
})
