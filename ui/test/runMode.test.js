import test from 'node:test'
import assert from 'node:assert/strict'

import {
  assertRunMutationAllowed, clearRunAccess, liveHistory, reconcileHistoricalSelection,
  rejectHistory, requestHistory, resolveHistory, runIdFromApiPath, setRunAccess,
} from '../src/runMode.js'

test('history resource never re-labels a stale response as the requested sequence', () => {
  const loading10 = requestHistory(10)
  const stale = resolveHistory(loading10, 9, { seq: 9, state: { nodes: { 9: {} } } })
  assert.deepEqual(stale, loading10)

  const ready = resolveHistory(loading10, 10, { seq: 10, state: { nodes: { 10: {} } } })
  assert.equal(ready.status, 'ready')
  assert.equal(ready.requestedSeq, 10)
  assert.equal(ready.resolvedSeq, 10)
  assert.deepEqual(Object.keys(ready.data.nodes), ['10'])
})

test('failed history request contains no previous snapshot data', () => {
  const loading = requestHistory(29)
  const failed = rejectHistory(loading, 29, new Error('offline'))
  assert.equal(failed.status, 'error')
  assert.equal(failed.data, null)
  assert.equal(failed.error, 'offline')
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
