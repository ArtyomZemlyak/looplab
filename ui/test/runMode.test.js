import test from 'node:test'
import assert from 'node:assert/strict'

import {
  assertRunMutationAllowed, clearRunAccess, liveHistory, reconcileHistoricalSelection,
  historyMatches, rejectHistory, requestHistory, resolveHistory, runIdFromApiPath, setRunAccess,
} from '../src/runMode.js'

const GEN_A = 'a'.repeat(64)
const GEN_B = 'b'.repeat(64)

test('history resource never re-labels a stale response as the requested sequence', () => {
  const loading10 = requestHistory(10, GEN_A)
  const stale = resolveHistory(loading10, 9, GEN_A, {
    seq: 9, generation: GEN_A, state: { nodes: { 9: {} } },
  })
  assert.deepEqual(stale, loading10)

  const ready = resolveHistory(loading10, 10, GEN_A, {
    seq: 10, generation: GEN_A, state: { nodes: { 10: {} } },
  })
  assert.equal(ready.status, 'ready')
  assert.equal(ready.requestedSeq, 10)
  assert.equal(ready.requestedGeneration, GEN_A)
  assert.equal(ready.resolvedGeneration, GEN_A)
  assert.equal(ready.resolvedSeq, 10)
  assert.deepEqual(Object.keys(ready.data.nodes), ['10'])
})

test('failed history request contains no previous snapshot data', () => {
  const loading = requestHistory(29, GEN_A)
  const failed = rejectHistory(loading, 29, GEN_A, new Error('offline'))
  assert.equal(failed.status, 'error')
  assert.equal(failed.data, null)
  assert.equal(failed.error, 'offline')
})

test('history generation is part of request, response, and error identity', () => {
  const loadingB = requestHistory(10, GEN_B)

  // A callback formed for generation A cannot fill generation B's loading resource.
  const staleResponse = resolveHistory(loadingB, 10, GEN_A, {
    seq: 10, generation: GEN_A, state: { nodes: { stale: {} } },
  })
  const staleError = rejectHistory(loadingB, 10, GEN_A, new Error('stale A error'))
  assert.deepEqual(staleResponse, loadingB)
  assert.deepEqual(staleError, loadingB)
  assert.equal(historyMatches(loadingB, 10, GEN_A), false)
  assert.equal(historyMatches(loadingB, 10, GEN_B), true)

  // Even a callback with B identity fails closed if its payload was produced by A.
  const mismatchedPayload = resolveHistory(loadingB, 10, GEN_B, {
    seq: 10, generation: GEN_A, state: { nodes: { stale: {} } },
  })
  assert.equal(mismatchedPayload.status, 'error')
  assert.equal(mismatchedPayload.requestedGeneration, GEN_B)
  assert.equal(mismatchedPayload.resolvedGeneration, null)
  assert.equal(mismatchedPayload.data, null)
  assert.match(mismatchedPayload.error, /run changed/i)
})

test('historical selection is cleared when the node does not exist yet', () => {
  const snapshot = { nodes: { 0: { id: 0 }, 13: { id: 13 } } }
  assert.equal(reconcileHistoricalSelection(13, snapshot), 13)
  assert.equal(reconcileHistoricalSelection(14, snapshot), null)
  assert.equal(reconcileHistoricalSelection(null, snapshot), null)
})

test('run mutation guard blocks encoded run paths only while history is active', () => {
  const runId = 'demo run'
  assert.equal(runIdFromApiPath('/api/runs/demo%20run/control'), runId)
  clearRunAccess(runId)
  assert.doesNotThrow(() => assertRunMutationAllowed('/api/runs/demo%20run/control'))

  setRunAccess(runId, { readOnly: true, seq: 29 })
  assert.throws(
    () => assertRunMutationAllowed('/api/runs/demo%20run/control'),
    error => error.code === 'HISTORICAL_READ_ONLY' && error.seq === 29,
  )
  // Unrelated global mutations do not inherit a run-local history lock.
  assert.doesNotThrow(() => assertRunMutationAllowed('/api/settings'))

  setRunAccess(runId, { readOnly: false })
  assert.doesNotThrow(() => assertRunMutationAllowed('/api/runs/demo%20run/control'))
  clearRunAccess(runId)
  assert.deepEqual(liveHistory().status, 'live')
})

test('review access has its own terminal read-only error', () => {
  setRunAccess('demo', { readOnly: true, mode: 'review' })
  assert.throws(
    () => assertRunMutationAllowed('/api/runs/demo/control'),
    error => error.code === 'REVIEW_READ_ONLY' && /review link/i.test(error.message),
  )
  clearRunAccess('demo')
})

test('a generation-mismatched diagnostic link fails closed before any run mutation', () => {
  setRunAccess('demo', { readOnly: true, mode: 'stale-link' })
  assert.throws(
    () => assertRunMutationAllowed('/api/runs/demo/control'),
    error => error.code === 'STALE_LINK_READ_ONLY' && /earlier run generation/i.test(error.message),
  )
  clearRunAccess('demo')
})

// --- the start-over recovery lock (shipped by 208fb4b with no direct coverage of its own) -------
//
// `getRunAccess` falls back to this tab's durable start-over envelope when RunView is unmounted, so
// rename/move/delete cannot slip past an unresolved "start over" during Back, a route switch, or a
// reload on the list screen. Nothing exercised that fallback: its only observable effect was four
// unrelated test files whose storage stub answered EVERY key with one value, which the reader read
// as a corrupt envelope and the guard fails closed into a run-wide lock. Fixing those stubs removes
// the accident, so the property gets a real test here instead.

const START_OVER_KEY = runId => `ll.run-start-over.${encodeURIComponent(runId)}`

const withSessionStorage = (entries, fn) => {
  const previous = globalThis.sessionStorage
  const store = new Map(Object.entries(entries))
  // A real `Storage` answers `null` for a key nobody set — that distinction IS the contract here.
  globalThis.sessionStorage = {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: key => store.delete(key),
  }
  try { return fn() } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
}

const activeIntent = runId => JSON.stringify({
  schema: 3, runId, expectedGeneration: GEN_A,
  operationId: '11111111-2222-4333-8444-555555555555',
  replacementGeneration: null, phase: 'pending', updatedAt: 1,
})

test('an unresolved start-over blocks run mutations even with RunView unmounted', () => {
  withSessionStorage({ [START_OVER_KEY('demo')]: activeIntent('demo') }, () => {
    clearRunAccess('demo')       // exactly the state after leaving the run view
    assert.throws(() => assertRunMutationAllowed('/api/runs/demo/label'), error => {
      assert.equal(error.code, 'START_OVER_RECOVERY_LOCK')
      assert.match(error.message, /retry or finish that exact request/)
      return true
    })
  })
})

test('an unreadable start-over envelope fails CLOSED rather than opening the run', () => {
  // Corruption cannot prove the operation finished, and the operation is destructive. Both shapes
  // the reader classifies as corrupt — an empty value and unparseable bytes — must lock.
  for (const raw of ['', '{not json', JSON.stringify({ schema: 3, runId: 'other' })]) {
    withSessionStorage({ [START_OVER_KEY('demo')]: raw }, () => {
      clearRunAccess('demo')
      assert.throws(() => assertRunMutationAllowed('/api/runs/demo/label'),
        error => error.code === 'START_OVER_RECOVERY_LOCK',
        `a ${JSON.stringify(raw).slice(0, 20)} envelope must not open the run`)
    })
  }
})

test('a run with no envelope, and every OTHER run, stay mutable', () => {
  withSessionStorage({ [START_OVER_KEY('locked')]: activeIntent('locked') }, () => {
    clearRunAccess('locked')
    clearRunAccess('other')
    assert.throws(() => assertRunMutationAllowed('/api/runs/locked/label'))
    // The lock is per-run: one unresolved start-over must not freeze the whole list.
    assert.doesNotThrow(() => assertRunMutationAllowed('/api/runs/other/label'))
  })
})

test('storage being unavailable is not the same as a corrupt envelope', () => {
  // A browser with storage disabled or a quota-exceeded read reports `unavailable`. Treating that
  // as corruption would lock EVERY run for that user with no way to clear it.
  const previous = globalThis.sessionStorage
  globalThis.sessionStorage = { getItem: () => { throw new Error('SecurityError') } }
  try {
    clearRunAccess('demo')
    assert.doesNotThrow(() => assertRunMutationAllowed('/api/runs/demo/label'))
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previous
  }
})

test('a live RunView publication wins over the durable envelope', () => {
  withSessionStorage({ [START_OVER_KEY('demo')]: activeIntent('demo') }, () => {
    // The mounted view knows the operation resolved; its publication is the authority.
    setRunAccess('demo', { readOnly: false })
    assert.doesNotThrow(() => assertRunMutationAllowed('/api/runs/demo/label'))
    clearRunAccess('demo')       // ...and unmounting restores the durable fallback
    assert.throws(() => assertRunMutationAllowed('/api/runs/demo/label'))
  })
})
