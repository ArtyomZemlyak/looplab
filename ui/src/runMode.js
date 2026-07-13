// Historical run viewing is a capability boundary, not a presentation flag.  This module keeps the
// async snapshot identity explicit and provides a final client-side guard for every /api/runs/:id
// mutation.  Server authorization remains authoritative; this guard prevents the current UI from
// accidentally targeting the live run while a historical snapshot is on screen.

export const liveHistory = () => ({
  status: 'live', requestedSeq: null, requestedGeneration: null,
  resolvedSeq: null, resolvedGeneration: null, data: null, error: null,
})

const historyGeneration = generation => generation == null ? null : String(generation)

export const requestHistory = (seq, generation = null) => ({
  status: 'loading', requestedSeq: Number(seq),
  requestedGeneration: historyGeneration(generation),
  resolvedSeq: null, resolvedGeneration: null, data: null, error: null,
})

export function historyMatches(resource, requestedSeq, requestedGeneration = null) {
  return resource?.requestedSeq === Number(requestedSeq)
    && resource?.requestedGeneration === historyGeneration(requestedGeneration)
}

export function resolveHistory(resource, requestedSeq, requestedGeneration, payload) {
  if (!historyMatches(resource, requestedSeq, requestedGeneration)) return resource
  const expectedGeneration = historyGeneration(requestedGeneration)
  const actualGeneration = historyGeneration(payload?.generation)
  if (actualGeneration !== expectedGeneration) {
    return {
      status: 'error', requestedSeq: Number(requestedSeq),
      requestedGeneration: expectedGeneration,
      resolvedSeq: null, resolvedGeneration: null, data: null,
      error: 'The run changed while this historical snapshot was loading. Retry the snapshot.',
    }
  }
  return {
    status: 'ready',
    requestedSeq: Number(requestedSeq),
    requestedGeneration: expectedGeneration,
    resolvedSeq: Number(payload?.seq ?? requestedSeq),
    resolvedGeneration: actualGeneration,
    data: payload?.state ?? null,
    error: null,
  }
}

export function rejectHistory(resource, requestedSeq, requestedGeneration, error) {
  if (!historyMatches(resource, requestedSeq, requestedGeneration)) return resource
  return {
    status: 'error', requestedSeq: Number(requestedSeq),
    requestedGeneration: historyGeneration(requestedGeneration),
    resolvedSeq: null, resolvedGeneration: null, data: null,
    error: error?.message || String(error || 'Unable to load historical snapshot'),
  }
}

export function reconcileHistoricalSelection(nodeId, snapshot) {
  if (nodeId == null) return null
  return snapshot?.nodes && snapshot.nodes[nodeId] != null ? nodeId : null
}

const accessByRun = new Map()

function announce(runId) {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent('ll:run-access', { detail: { runId, ...getRunAccess(runId) } }))
}

export function setRunAccess(runId, { readOnly = false, seq = null, mode = null } = {}) {
  if (!runId) return
  const resolvedMode = mode || (readOnly ? 'history' : 'live')
  accessByRun.set(String(runId), {
    readOnly: !!readOnly,
    seq: readOnly && seq != null ? Number(seq) : null,
    mode: resolvedMode,
  })
  announce(String(runId))
}

export function clearRunAccess(runId) {
  if (!runId) return
  accessByRun.delete(String(runId))
  announce(String(runId))
}

export function getRunAccess(runId) {
  return accessByRun.get(String(runId)) || { readOnly: false, seq: null, mode: 'live' }
}

export function runIdFromApiPath(path) {
  const m = String(path || '').match(/\/api\/runs\/([^/?#]+)(?:[/?#]|$)/)
  if (!m) return null
  try { return decodeURIComponent(m[1]) } catch { return m[1] }
}

export function assertRunMutationAllowed(path) {
  const runId = runIdFromApiPath(path)
  if (!runId) return
  const access = getRunAccess(runId)
  if (!access.readOnly) return
  const review = access.mode === 'review'
  const error = new Error(review
    ? 'This review link is read-only'
    : `Historical snapshot seq ${access.seq} is read-only — return to live to act`)
  error.code = review ? 'REVIEW_READ_ONLY' : 'HISTORICAL_READ_ONLY'
  error.runId = runId
  error.seq = access.seq
  throw error
}
