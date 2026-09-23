// Historical run viewing is a capability boundary, not a presentation flag.  This module keeps the
// async snapshot identity explicit and provides a final client-side guard for every /api/runs/:id
// mutation.  Server authorization remains authoritative; this guard prevents the current UI from
// accidentally targeting the live run while a historical snapshot is on screen.

import { loadRunStartOverIntent } from './runStartOverRecovery.js'

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
  const key = String(runId)
  const published = accessByRun.get(key)
  if (published) return published
  // RunView may be unmounted while the user returns to the run list. Recover the destructive lock
  // directly from this tab's durable envelope so rename/move/delete cannot bypass it during Back,
  // a route switch, or a full reload on the list screen.
  const recovery = loadRunStartOverIntent(key)
  if (recovery.kind === 'active' || recovery.kind === 'corrupt') {
    return { readOnly: true, seq: null, mode: 'start-over', recoveryKind: recovery.kind }
  }
  return { readOnly: false, seq: null, mode: 'live' }
}

export function listStartOverRunAccesses() {
  return [...accessByRun.entries()]
    .filter(([, access]) => access?.mode === 'start-over')
    .map(([runId, access]) => ({ runId, kind: access.recoveryKind || 'active' }))
}

export function runIdFromApiPath(path) {
  const m = String(path || '').match(/\/api\/runs\/([^/?#]+)(?:[/?#]|$)/)
  if (!m) return null
  try { return decodeURIComponent(m[1]) } catch { return m[1] }
}

// THE READ-ONLY MODES, one row each (review 2026-09-22, UI-04). RunView publishes seven access modes
// (`runAccessMode`), and its three consumers each named a subset: this guard spelled four and worded
// the rest as history, and RunView's `readOnlyReason` for the Inspector and the Report collapsed
// everything but review and start-over into 'history' too. So a run that was merely LOADING, or whose
// current generation was UNCONFIRMED, refused an action with "Historical snapshot seq null is
// read-only — return to live to act" and labelled its panels "Snapshot seq null": wrong about the
// cause, and wrong about the remedy — there was no snapshot to return from. `label` is the short cause
// the panels print before their own consequence; `refusal` is the guard's whole sentence.
const READ_ONLY_MODES = Object.freeze({
  review: {
    code: 'REVIEW_READ_ONLY', label: () => 'Read-only review',
    refusal: () => 'This review link is read-only',
  },
  'stale-link': {
    code: 'STALE_LINK_READ_ONLY', label: () => 'Earlier run generation',
    refusal: () => 'This diagnostic link targets an earlier run generation — open the current generation before acting',
  },
  'start-over': {
    code: 'START_OVER_RECOVERY_LOCK', label: () => 'Start over unresolved',
    refusal: () => 'Start over is unresolved — retry or finish that exact request before changing the run',
  },
  history: {
    code: 'HISTORICAL_READ_ONLY', label: seq => `Snapshot seq ${seq}`,
    refusal: seq => `Historical snapshot seq ${seq} is read-only — return to live to act`,
  },
  loading: {
    code: 'RUN_LOADING_READ_ONLY', label: () => 'Run still loading',
    refusal: () => 'The run is still loading — act once its current state has arrived',
  },
  unavailable: {
    code: 'RUN_UNAVAILABLE_READ_ONLY', label: () => 'Run state unconfirmed',
    refusal: () => "The run's current state is not confirmed — reload the run before acting",
  },
})
// A read-only access in a mode no row names fails CLOSED under its own code — never in history's words.
const UNKNOWN_READ_ONLY = {
  code: 'RUN_READ_ONLY', label: () => 'Read-only', refusal: () => 'This run is read-only here',
}

// Every code `assertRunMutationAllowed` can throw. Each is a refusal made in THIS tab before any
// request left it, so a caller may read it as "nothing was sent" (`ConceptView` clears a paid lens
// intent on exactly these — it used to keep its own list of three, which missed start-over).
export const LOCAL_READ_ONLY_CODES = Object.freeze([
  ...Object.values(READ_ONLY_MODES).map(row => row.code), UNKNOWN_READ_ONLY.code,
])

// The ONE derivation of RunView's access mode, in precedence order: the mode it publishes to
// `setRunAccess` and the `readOnlyReason` its panels print are both this value.
export function runAccessMode({ reviewMode = false, startOverBlocked = false, routeFenceBlocked = false,
  historyActive = false, runStatus = 'ready', runAuthorityBlocked = false } = {}) {
  return reviewMode ? 'review' : startOverBlocked ? 'start-over'
    : routeFenceBlocked ? 'stale-link' : historyActive ? 'history'
      : runStatus === 'loading' ? 'loading' : runAuthorityBlocked ? 'unavailable' : 'live'
}

// The short cause a read-only panel prints for `mode` ("Snapshot seq 12", "Run still loading").
export function readOnlyLabel(mode, seq = null) {
  return (READ_ONLY_MODES[mode] || UNKNOWN_READ_ONLY).label(seq)
}

export function assertRunMutationAllowed(path, { allowModes = [] } = {}) {
  const runId = runIdFromApiPath(path)
  if (!runId) return
  const access = getRunAccess(runId)
  if (!access.readOnly) return
  if (allowModes.includes(access.mode)) return
  const row = READ_ONLY_MODES[access.mode] || UNKNOWN_READ_ONLY
  const error = new Error(row.refusal(access.seq))
  error.code = row.code
  error.runId = runId
  error.seq = access.seq
  throw error
}
