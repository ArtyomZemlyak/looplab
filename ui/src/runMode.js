// Historical run viewing is a capability boundary, not a presentation flag.  This module keeps the
// async snapshot identity explicit and provides a final client-side guard for every /api/runs/:id
// mutation.  Server authorization remains authoritative; this guard prevents the current UI from
// accidentally targeting the live run while a historical snapshot is on screen.

export const liveHistory = () => ({
  status: 'live', requestedSeq: null, resolvedSeq: null, data: null, error: null,
})

export const requestHistory = (seq) => ({
  status: 'loading', requestedSeq: Number(seq), resolvedSeq: null, data: null, error: null,
})

export function resolveHistory(resource, requestedSeq, payload) {
  if (resource.requestedSeq !== Number(requestedSeq)) return resource
  return {
    status: 'ready',
    requestedSeq: Number(requestedSeq),
    resolvedSeq: Number(payload?.seq ?? requestedSeq),
    data: payload?.state ?? null,
    error: null,
  }
}

export function rejectHistory(resource, requestedSeq, error) {
  if (resource.requestedSeq !== Number(requestedSeq)) return resource
  return {
    status: 'error', requestedSeq: Number(requestedSeq), resolvedSeq: null, data: null,
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

export function setRunAccess(runId, { readOnly = false, seq = null } = {}) {
  if (!runId) return
  accessByRun.set(String(runId), { readOnly: !!readOnly, seq: readOnly ? Number(seq) : null })
  announce(String(runId))
}

export function clearRunAccess(runId) {
  if (!runId) return
  accessByRun.delete(String(runId))
  announce(String(runId))
}

export function getRunAccess(runId) {
  return accessByRun.get(String(runId)) || { readOnly: false, seq: null }
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
  const error = new Error(`Historical snapshot seq ${access.seq} is read-only — return to live to act`)
  error.code = 'HISTORICAL_READ_ONLY'
  error.runId = runId
  error.seq = access.seq
  throw error
}
