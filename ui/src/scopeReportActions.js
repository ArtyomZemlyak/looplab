// The paid scope-report action protocol: one POST per explicit attempt under a caller-owned UUID, and
// a GET-only reconciliation that never mints or automatically replays a paid identity. Split out of
// api.js (doc 25 UI-02 — bodies verbatim); api.js re-exports everything, so importers are unchanged.
//
// Every failure that may have crossed the POST boundary keeps the exact action id (and job id) so a
// caller can reconcile instead of re-billing; `scopeGenerationErrorIdentity` is shared with
// `jobAwait` and therefore lives in commandModel.js rather than here.

import { _authHeaders, reviewReadPath } from './apiClient.js'
import {
  COMMAND_REQUEST_TIMEOUT_MS, UUID_V4_RE, safeIdentityText, scopeGenerationErrorIdentity,
} from './commandModel.js'
import { commandJson, commandRead, jobAwait } from './commandProtocol.js'

// cross-run aggregate reports over a scope (project | task | supertask). GET returns the stored report
// + staleness ({exists, content, generated_at, run_ids, stale, added, current_run_count}); generate
// (re)synthesizes on demand via an agent with access to every run in the scope.
const _scopeUrl = (type, id) => `/api/scope-report/${encodeURIComponent(type)}/${encodeURIComponent(id)}`
export const getScopeReport = (type, id, options = {}) => {
  const path = _scopeUrl(type, id)
  return commandRead(reviewReadPath(path), {
    errorPath: path, signal: options.signal,
    requestTimeoutMs: options.requestTimeoutMs ?? COMMAND_REQUEST_TIMEOUT_MS,
  })
}
// durable action reads use a separate route namespace. Appending `/actions/...` to
// an opaque scope path would make valid ids ending in that shape impossible to read as reports.
const _scopeActionUrl = (type, id, actionId) =>
  `/api/scope-report-actions/${encodeURIComponent(actionId)}`
  + `?scope_type=${encodeURIComponent(type)}&scope_id=${encodeURIComponent(id)}`

const scopeGenerationRecord = value => !!value && typeof value === 'object' && !Array.isArray(value)
// The server owns one canonical spelling for durable identities. Normalize at every public API
// boundary so a valid uppercase UUID cannot become a permanently mismatched paid receipt.
const scopeActionId = value => typeof value === 'string' && UUID_V4_RE.test(value)
  ? value.toLowerCase() : null
const scopeJobId = value => typeof value === 'string' && safeIdentityText(value) ? value : null


const scopeIdentityMismatch = (actionId, jobId = null) => {
  const error = new Error('scope report action identity could not be verified')
  error.code = 'scope_report_action_identity_mismatch'
  return scopeGenerationErrorIdentity(error, actionId, jobId, true)
}

const scopeTerminalResult = (value, actionId, jobId = null) => {
  if (!scopeGenerationRecord(value) || value.action_id !== actionId) {
    throw scopeIdentityMismatch(actionId, jobId)
  }
  const retainedJobId = scopeJobId(value.job_id) || jobId
  if (value.status === 'indeterminate' && value.ok === false
      && value.code === 'scope_report_action_indeterminate') {
    const error = new Error('scope report action outcome is indeterminate')
    error.code = value.code
    throw scopeGenerationErrorIdentity(error, actionId, retainedJobId, true)
  }
  if (value.status != null && !['done', 'abandoned'].includes(value.status)) {
    const error = new Error('scope report terminal status is invalid')
    error.code = 'scope_report_action_protocol_error'
    throw scopeGenerationErrorIdentity(error, actionId, retainedJobId, true)
  }
  if (value.ok === false) {
    const error = new Error(value.error || value.message || value.code || 'scope report generation failed')
    error.code = typeof value.code === 'string' ? value.code : 'scope_report_generation_failed'
    // `/api/jobs` necessarily labels every completed worker receipt `done`, including the worker's
    // fail-closed response when it could not durably publish an action terminal. That result is not a
    // definitive paid outcome: preserve the UUID and fall through to the durable action ledger.
    if (error.code === 'scope_report_action_indeterminate') {
      throw scopeGenerationErrorIdentity(error, actionId, retainedJobId, true)
    }
    // Exact action identity plus an explicit terminal failure is authoritative. A stray legacy
    // ambiguity flag cannot keep a settled paid lock alive forever or authorize another POST.
    throw scopeGenerationErrorIdentity(error, actionId, retainedJobId, false)
  }
  if (value.ok !== true) {
    const error = new Error('scope report terminal response is invalid')
    error.code = 'scope_report_action_protocol_error'
    throw scopeGenerationErrorIdentity(error, actionId, retainedJobId, true)
  }
  return retainedJobId && !value.job_id ? { ...value, job_id: retainedJobId } : value
}

export async function getScopeReportAction(type, id, actionId, {
  signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  actionId = scopeActionId(actionId)
  if (!actionId) throw new Error('A valid scope report action id is required.')
  const path = _scopeActionUrl(type, id, actionId)
  try {
    const value = await commandRead(path, { signal, requestTimeoutMs })
    if (!scopeGenerationRecord(value) || value.action_id !== actionId
        || !['running', 'done', 'unknown', 'indeterminate', 'abandoned'].includes(value.status)) {
      throw scopeIdentityMismatch(actionId, scopeJobId(value?.job_id))
    }
    return value
  } catch (error) {
    throw scopeGenerationErrorIdentity(error, actionId, scopeJobId(error?.job_id || error?.jobId), true)
  }
}

export async function abandonScopeReportAction(type, id, actionId, {
  signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  actionId = scopeActionId(actionId)
  if (!actionId) throw new Error('A valid scope report action id is required.')
  const path = `/api/scope-report-actions/${encodeURIComponent(actionId)}/abandon`
    + `?scope_type=${encodeURIComponent(type)}&scope_id=${encodeURIComponent(id)}`
  try {
    const value = await commandJson(path, {
      method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
      body: '{}', cache: 'no-store', signal,
    }, requestTimeoutMs)
    const explicitlyAbandoned = value?.status === 'abandoned'
      && value?.ok === false && value?.code === 'scope_report_action_abandoned'
    const concurrentlySettled = value?.status === 'done' && typeof value?.ok === 'boolean'
    // A server-proven unknown action has no claim/provider effect to tombstone. The explicit discard
    // is therefore a successful local cleanup acknowledgement, not a durable `abandoned` receipt.
    const safelyUnknown = value?.status === 'unknown' && value?.ok == null && value?.code == null
    if (!scopeGenerationRecord(value) || value.action_id !== actionId
        || (!explicitlyAbandoned && !concurrentlySettled && !safelyUnknown)) {
      throw scopeIdentityMismatch(actionId, scopeJobId(value?.job_id))
    }
    return value
  } catch (error) {
    throw scopeGenerationErrorIdentity(error, actionId, scopeJobId(error?.job_id), true)
  }
}

const scopeAmbiguousReceipt = (code, actionId, jobId = null) => {
  const error = new Error('scope report action is not yet authoritative')
  error.code = code
  return scopeGenerationErrorIdentity(error, actionId, jobId, true)
}

// Resume is GET-only and never mints or automatically replays a paid identity. A consumed volatile
// job receipt falls through to the durable action ledger. Unknown, transient, indeterminate and
// mismatched reads stay locked until an explicit user recovery action.
export async function reconcileScopeReportGeneration(type, id, {
  actionId, jobId = null, signal, onJob,
  intervalMs = 1500, timeoutMs = 600000,
  requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
  maxTransientErrors = 3,
} = {}) {
  actionId = scopeActionId(actionId)
  if (!actionId) throw new Error('A valid scope report action id is required.')
  let acceptedJobId = scopeJobId(jobId)
  let attemptedJobId = null
  if (acceptedJobId) {
    attemptedJobId = acceptedJobId
    try {
      await jobAwait({ status: 'running', job_id: acceptedJobId }, {
        intervalMs, timeoutMs, signal, requestTimeoutMs, maxTransientErrors,
      })
    } catch (error) {
      acceptedJobId = scopeJobId(error?.job_id || error?.jobId) || acceptedJobId
    }
    // JobRegistry is only a latency hint. Its generic worker fallback can say `done/job_failed`
    // without proving that this endpoint committed a paid-action terminal. Always read the durable
    // action ledger below before success or failure is allowed to clear the caller-owned UUID.
  }

  const action = await getScopeReportAction(type, id, actionId, { signal, requestTimeoutMs })
  const durableJobId = scopeJobId(action.job_id)
  if (durableJobId) {
    acceptedJobId = durableJobId
    try { onJob?.(durableJobId) } catch { /* persistence callbacks cannot alter paid work */ }
  }
  if (action.status === 'done' || action.status === 'abandoned') {
    return scopeTerminalResult(action, actionId, acceptedJobId)
  }
  if (action.status === 'unknown') {
    // A stale tab may retain a UUID that was rejected while another tab's action later completed.
    // Absence is therefore never permission to spend automatically: retry or abandon is explicit.
    throw scopeAmbiguousReceipt('scope_report_action_unknown', actionId, acceptedJobId)
  }
  if (action.status === 'indeterminate') {
    throw scopeAmbiguousReceipt('scope_report_action_indeterminate', actionId, acceptedJobId)
  }
  if (!durableJobId) {
    throw scopeAmbiguousReceipt('scope_report_action_protocol_error', actionId, acceptedJobId)
  }
  // A fresh durable job id can appear when the initial POST response was lost before the browser
  // learned it. Do one bounded job observation; never spin job->action->same-job in one UI attempt.
  if (durableJobId === attemptedJobId) {
    throw scopeAmbiguousReceipt('scope_report_action_running', actionId, durableJobId)
  }
  let result
  try {
    result = await jobAwait({ status: 'running', job_id: durableJobId }, {
      intervalMs, timeoutMs, signal, requestTimeoutMs, maxTransientErrors,
    })
  } catch (error) {
    throw scopeGenerationErrorIdentity(error, actionId, durableJobId, true)
  }
  if (result?.status === 'done' || result?.ambiguous !== true) {
    const settled = await getScopeReportAction(type, id, actionId, { signal, requestTimeoutMs })
    if (settled.status === 'done' || settled.status === 'abandoned') {
      return scopeTerminalResult(settled, actionId, scopeJobId(settled.job_id) || durableJobId)
    }
    if (settled.status === 'indeterminate') {
      throw scopeAmbiguousReceipt('scope_report_action_indeterminate', actionId, durableJobId)
    }
    throw scopeAmbiguousReceipt('scope_report_action_unresolved', actionId, durableJobId)
  }
  throw scopeAmbiguousReceipt('scope_report_action_unresolved', actionId, durableJobId)
}

// Cross-run synthesis is paid. Persist/caller-own the UUID before invoking this function. Each
// explicit attempt sends one POST; recovery never invents a new UUID. Accepted/possibly-accepted
// failures retain the exact identity for read-only reconciliation or explicit same-action retry.
export async function genScopeReport(type, id, {
  actionId, signal, onJob,
  intervalMs = 1500, timeoutMs = 600000,
  requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
  maxTransientErrors = 3,
} = {}) {
  actionId = scopeActionId(actionId)
  if (!actionId) throw new Error('A valid scope report action id is required.')
  const path = `${_scopeUrl(type, id)}/generate`
  let response
  try {
    response = await commandJson(path, {
      method: 'POST', signal,
      headers: _authHeaders({
        'Content-Type': 'application/json', 'Idempotency-Key': actionId,
      }),
      body: '{}',
    }, requestTimeoutMs, { submission: true })
  } catch (error) {
    const activeActionId = error?.code === 'scope_report_action_in_progress'
      ? scopeActionId(error?.detail?.action_id) : null
    if (activeActionId) {
      // A second tab can discover the scope's exact server-fenced action through this definitive
      // conflict. Adopt that identity for GET-only recovery; the rejected fresh UUID was never paid.
      throw scopeGenerationErrorIdentity(error, activeActionId, null, true)
    }
    const status = Number(error?.status)
    // A strict claim/fence rename can become visible before its durability confirmation throws.
    // The server then has no provider result to return, but the UUID may already be a durable or
    // fail-closed identity. Preserve it across this typed 409 instead of minting a second action.
    const ambiguous = error?.code === 'scope_report_storage_conflict'
      || error?.submissionMayHaveSucceeded === true
      || error?.status == null || status >= 500 || status === 408 || status === 425
    throw scopeGenerationErrorIdentity(error, actionId, null, ambiguous)
  }
  if (!scopeGenerationRecord(response) || response.action_id !== actionId) {
    throw scopeIdentityMismatch(actionId, scopeJobId(response?.job_id))
  }
  if (response.status !== 'running') return scopeTerminalResult(response, actionId)
  const jobId = scopeJobId(response.job_id)
  if (!jobId) throw scopeIdentityMismatch(actionId)
  try { onJob?.(jobId) } catch { /* persistence callbacks cannot alter paid work */ }
  return reconcileScopeReportGeneration(type, id, {
    actionId, jobId, signal, onJob, intervalMs, timeoutMs, requestTimeoutMs, maxTransientErrors,
  })
}
