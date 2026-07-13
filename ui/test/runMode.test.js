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
