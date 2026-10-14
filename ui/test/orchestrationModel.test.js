import test from 'node:test'
import assert from 'node:assert/strict'
import { forensicsView, gpuQueueView, wakeLedgerView } from '../src/orchestrationModel.js'

test('gpu queue view names the holder, the FIFO order and what each gate waits for', () => {
  const view = gpuQueueView({
    lease_held: true,
    holder: { pid: 7, run_id: 'run-a', acquired_at: 1000 },
    queue: [{ run_id: 'run-b', pid: 8, created_at: 1100 }, { run_id: 'run-c', pid: 9, created_at: 1150 }],
    gates: [{ run_id: 'run-d', hold: false, start_after_ids: ['run-a'], reason: 'after a',
              blockers: [{ kind: 'start_after', run_id: 'run-a' }] },
            { run_id: 'run-e', hold: false, start_after_ids: [], blockers: [] }],
  }, 1200)
  assert.equal(view.holder.runId, 'run-a')
  assert.equal(view.holder.since, '3m')
  assert.deepEqual(view.queue.map(r => [r.position, r.runId]), [[1, 'run-b'], [2, 'run-c']])
  assert.deepEqual(view.gates[0].waitingFor, ['run-a'])
  assert.equal(view.gates[0].open, false)
  assert.equal(view.gates[1].open, true)
  assert.equal(gpuQueueView(null).known, false)
})

test('wake ledger view keeps the newest-first rows and tones failures', () => {
  const view = wakeLedgerView({
    configured: true, config: { command: ['codex', 'queue'], debounce_s: 15, enabled: true },
    newest_event_id: 41,
    ledger: [{ ts: 990, status: 'failed', attempt: 2, kinds: ['node_failed'], runs: ['r'],
               event_ids: [40, 41], exit_code: 3, error: 'busy' },
             { ts: 900, status: 'delivered', attempt: 1, kinds: ['run_idle'], runs: ['r'], event_ids: [39] }],
  }, 1000)
  assert.equal(view.command, 'codex')
  assert.deepEqual(view.rows.map(r => [r.status, r.tone, r.events]), [['failed', 'warn', 2], ['delivered', 'ok', 1]])
  assert.equal(view.rows[0].when, '10s')
  assert.equal(wakeLedgerView(undefined).configured, false)
})

test('forensics view leads with the original exception and per-rank signals', () => {
  const view = forensicsView({
    headline: 'stage stage1: SIGSEGV on rank(s) 3,5',
    root_exception: { type: 'FileNotFoundError', message: 'x.json' },
    exceptions: [{ type: 'FileNotFoundError', message: 'x.json' }, { type: 'AssertionError', message: "('G0',)" }],
    signals: [{ signal: 'SIGSEGV', ranks: [3, 5], source: 'torchrun' }, { signal: 'SIGKILL', source: 'stderr' }],
    exit_codes: { 'rank 3': -11 },
    faulthandler: { error: 'Segmentation fault', stack: ['File "m.py", line 1'] },
    log_tails: { 'stage1.log': ['a', 'b'] }, rank_tails: { 'rank 3': ['step 5'] }, hints: ['nccl'],
  })
  assert.equal(view.root, 'FileNotFoundError: x.json')
  assert.deepEqual(view.wrappers, ["AssertionError: ('G0',)"])
  assert.deepEqual(view.signals, ['SIGSEGV · ranks 3, 5', 'SIGKILL · stderr'])
  assert.deepEqual(view.exitCodes, ['rank 3: -11'])
  assert.match(view.fault, /Segmentation fault/)
  assert.deepEqual(view.tails, [{ name: 'stage1.log', text: 'a\nb' }])
  assert.equal(forensicsView(null), null)
})
