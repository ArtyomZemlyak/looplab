// The pure command model: the status/action vocabulary, the durable identity regexes and their
// validators, the observed run-generation map, the client-owned error-code copy, and the record
// presentation contract every control surface shares. Split out of api.js (doc 25 UI-02 — bodies
// verbatim); api.js re-exports everything, so importers are unchanged.
//
// It imports NOTHING, on purpose. commandStorage.js (what may reach sessionStorage) and
// commandProtocol.js (what may leave over HTTP) both need this vocabulary and neither sits below the
// other, so this is the shared leaf they hoist down into instead of reaching back through the barrel.
// `scopeGenerationErrorIdentity` lives here for that reason and only that reason: it is pure, and
// both `jobAwait` in commandProtocol.js and the scope-report saga annotate an error with it.

export const COMMAND_SUCCEEDED = new Set(['succeeded', 'noop'])
export const COMMAND_FAILED = new Set(['failed', 'rejected', 'timed_out'])
export const COMMAND_PENDING = new Set(['accepted', 'executing'])
export const COMMAND_STATUSES = new Set([...COMMAND_SUCCEEDED, ...COMMAND_FAILED, ...COMMAND_PENDING])
export const TRANSIENT_HTTP = new Set([408, 425, 429])
export const COMMAND_REQUEST_TIMEOUT_MS = 8000

export const COMMAND_ID_RE = /^cmd_[0-9a-f]{32}$/
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
export const UUID_V4_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i
export const validRunGeneration = value => typeof value === 'string' && RUN_GENERATION_RE.test(value)
const observedRunGenerations = new Map()
const MAX_OBSERVED_RUN_GENERATIONS = 512   // exceeds any realistic in-view working set; bounds the map

const TRANSPORT_EVENT_BY_ACTION = Object.freeze({ stop: 'pause', finalize: 'run_abort', resume: 'resume' })
const ASSISTANT_EVENT_BY_ACTION = Object.freeze({
  stop: 'pause', pause: 'pause', finalize: 'run_abort', abort: 'run_abort', resume: 'resume',
  ratify: 'spec_approved', approve: 'approval_granted',
})
const CANONICAL_ACTION_BY_EVENT = Object.freeze({
  pause: 'stop', run_abort: 'finalize', resume: 'resume', spec_approved: 'ratify',
  approval_granted: 'approve',
})
// Durable command recovery is deliberately metadata-only. Server-provided messages/remediation can
// contain task data (or even serialized JSON with credentials), so storage keeps only a known stable
// code plus booleans/ids. Presentation is reconstructed from this client-owned copy after reload.
export const STORED_ERROR_KEYS = new Set(['code', 'retryable', 'existing_command_id'])
export const STORED_ERROR_CODES = new Set([
  'command_failed', 'command_request_failed', 'command_request_timeout',
  'owner_access_required', 'command_protocol_error', 'command_record_missing',
  'command_storage_unavailable', 'command_timeout', 'postcondition_timeout',
  'invalid_command', 'command_target_not_found', 'command_intent_missing',
  'command_not_retryable', 'command_in_progress', 'retry_existing_command',
  'command_intent_spent',
  'finalize_payload_conflict', 'finalize_in_progress', 'engine_finishing',
  'engine_start_uncertain', 'spawn_claim_confirmation_required', 'engine_failed',
  'spawn_failed', 'command_worker_failed', 'approval_not_requested',
  'ratification_not_requested', 'invalid_transition',
  'invalid_run_generation', 'run_generation_changed', 'run_generation_unavailable',
])
// Restored command records intentionally contain only a stable code, never server-authored text.
// Generate their copy from that code instead of eagerly shipping a large near-duplicate dictionary.
const storedErrorCopy = code => {
  const title = code === 'engine_failed' ? 'The run engine reported a failure'
    : code.replaceAll('_', ' ')
  let remediation = 'Refresh state before acting again.'
  if (code.includes('storage')) remediation = 'Enable session storage or free browser space, then try again.'
  else if (code.includes('access')) remediation = 'Restore owner access, then check this command.'
  else if (/timeout|protocol|uncertain|retry_existing/.test(code)) {
    remediation = 'Check this command; do not submit a new intent.'
  }
  return [title, remediation]
}

export const commandEventForAction = (action, source = 'assistant') =>
  (source === 'dock' ? TRANSPORT_EVENT_BY_ACTION : ASSISTANT_EVENT_BY_ACTION)[action] || null
export const commandActionForEvent = eventType => CANONICAL_ACTION_BY_EVENT[eventType] || null
export const normalizeRunGeneration = generation => validRunGeneration(generation) ? generation : null

// The token last rendered by useRunState. Mutation controls bind to this displayed snapshot rather
// than silently fetching a replacement generation after an in-place reset the user has not seen yet.
export function observeRunGeneration(runId, generation) {
  const key = String(runId || '')
  if (!key) return null
  const normalized = normalizeRunGeneration(generation)
  if (normalized) {
    // Bound the map so a long owner session navigating many runs can't grow it without limit: on
    // overflow drop the oldest entry (Map preserves insertion order). Re-set to move a live run to
    // the newest slot so the run currently in view is never the one evicted.
    observedRunGenerations.delete(key)
    observedRunGenerations.set(key, normalized)
    if (observedRunGenerations.size > MAX_OBSERVED_RUN_GENERATIONS) {
      observedRunGenerations.delete(observedRunGenerations.keys().next().value)
    }
    return normalized
  }
  observedRunGenerations.delete(key)
  return null
}

export function getObservedRunGeneration(runId) {
  return observedRunGenerations.get(String(runId || '')) || null
}

export const hasOnlyKeys = (value, allowed) => Object.keys(value).every(key => allowed.has(key))
export const safeIdentityText = value => typeof value === 'string' && value.length > 0 && value.length <= 200
  && !/[\u0000-\u001f\u007f]/.test(value)

// Status reads are observation, not command replay. A missing/forbidden command is therefore an
// authoritative terminal condition for this client; only failures that can plausibly disappear on
// the next request (network/timeouts, overload/rate-limit, and 5xx) keep the durable id observable.
export function isTransientCommandReadError(error) {
  if (error?.code === 'COMMAND_PROTOCOL_ERROR') return false
  if (error?.code === 'COMMAND_REQUEST_TIMEOUT' || error?.transient === true) return true
  if (error?.name === 'AbortError') return false
  if (error?.status == null) return true
  const status = Number(error.status)
  return Number.isFinite(status) && (status >= 500 || TRANSIENT_HTTP.has(status))
}

export function commandCanRetry(record) {
  return !!record?.id
    && (record.status === 'failed' || record.status === 'timed_out')
    && record?.error?.retryable === true
}

export function createIdempotencyKey(source = globalThis.crypto) {
  if (source?.randomUUID) return source.randomUUID()
  const bytes = new Uint8Array(16)
  if (source?.getRandomValues) source.getRandomValues(bytes)
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function commandErrorMessage(record) {
  const error = record?.error
  if (error && typeof error === 'object') {
    const canonical = storedErrorCopy(STORED_ERROR_CODES.has(error.code) ? error.code : 'command_failed')
    // Live responses may include a server-redacted explanation. A restored record contains no free
    // text, so it deterministically falls back to client-owned copy instead of persisted server data.
    const message = String(error.message || error.detail || canonical[0]).slice(0, 500)
    const remediation = String(error.remediation || (!error.message && !error.detail ? canonical[1] : '')).trim().slice(0, 500)
    return remediation && !message.toLowerCase().includes(remediation.toLowerCase())
      ? `${message} — ${remediation}` : message
  }
  return String(error || record?.detail || 'Command failed').slice(0, 500)
}

export function commandFailureRecord(error, previous = error?.commandRecord || null) {
  const localCode = error?.code === 'COMMAND_PROTOCOL_ERROR' ? 'command_protocol_error'
    : error?.code === 'COMMAND_REQUEST_TIMEOUT' ? 'command_request_timeout'
      : error?.code
  return {
    ...(previous || {}),
    ...(error?.commandId && !previous?.id ? { id: error.commandId } : {}),
    status: 'failed',
    error: {
      code: error?.status === 401 || error?.status === 403 ? 'owner_access_required'
        : error?.status === 404 ? 'command_record_missing' : localCode || 'command_request_failed',
      message: error?.message || String(error),
      retryable: false,
      remediation: error?.remediation || (error?.status === 401 || error?.status === 403
        ? 'restore owner access, then check this command again'
        : error?.code === 'COMMAND_PROTOCOL_ERROR'
          ? 'check the same durable command again; do not submit a new intent'
          : error?.status === 404
            ? 'refresh the run; this server no longer has the durable command record'
            : 'refresh run state before submitting another action'),
      ...(error?.existingCommandId ? { existing_command_id: String(error.existingCommandId) } : {}),
    },
  }
}

// Pure presentation contract shared by every control surface. Only succeeded/noop are completion;
// executing is deliberately pending, and terminal server failures stay structured/actionable.
export function commandFeedback(record, labels = {}) {
  const status = record?.status
  if (status === 'succeeded') return { kind: 'success', terminal: true, status,
    message: labels.success || 'Command completed' }
  if (status === 'noop') return { kind: 'success', terminal: true, status,
    message: labels.noop || `${labels.success || 'Command completed'} (already satisfied)` }
  if (COMMAND_PENDING.has(status)) return { kind: 'pending', terminal: false, status,
    message: labels.executing || `${labels.requested || 'Command'} requested — waiting for completion` }
  if (COMMAND_FAILED.has(status)) return { kind: 'error', terminal: true, status,
    message: `${labels.failure || 'Command failed'}: ${commandErrorMessage(record)}` }
  return { kind: 'error', terminal: true, status: status || 'missing',
    message: `${labels.failure || 'Command failed'}: unexpected command status ${status || 'missing'}` }
}

// The other half of the same presentation contract (doc 25 UI-07). `commandFeedback` explains a
// RECORD; this explains an ATTEMPT — including the attempt that never produced a record because the
// transport threw. A dozen panels wrote the same try/await/feedback/onToast/catch-onToast block, and
// a copy that drifts is invisible: an operator sees no toast at all and reads the silence as "it
// worked". The two things a call site must still be able to do are why this RETURNS the feedback
// rather than owning the whole interaction:
//
// * gate a success-only side effect (clearing an input draft) on `kind === 'success'`, and
// * roll an optimistic update back on anything else.
//
// The transport arm is therefore an `error` feedback, never a success — otherwise a network failure
// would clear the operator's draft. `labels.transport` is for the surfaces that deliberately WITHHOLD
// the thrown message ("… could not be submitted. Try again."); everything else keeps the
// `${failure}: ${message}` shape the panels already used.
export async function submitCommand(promise, labels = {}, onToast) {
  let feedback
  try {
    feedback = commandFeedback(await promise, labels)
  } catch (error) {
    feedback = { kind: 'error', terminal: true, status: 'transport', transport: true,
      message: labels.transport
        || `${labels.failure || 'Command failed'}: ${error?.message || error}` }
  }
  onToast?.(feedback.message)
  return feedback
}

export function runGenerationError(code, message, remediation) {
  const error = new Error(message)
  error.code = code
  error.remediation = remediation
  return error
}

// once paid work may have crossed the POST boundary, every error carries only bounded
// client-owned identity metadata. Presentation never needs the server/provider body to recover the
// exact action, and callers can never mistake an identity-less failure for permission to re-bill.
export const scopeGenerationErrorIdentity = (cause, actionId, jobId = null, ambiguous = true) => {
  let error = cause && typeof cause === 'object' ? cause : new Error('scope report request failed')
  try {
    if (actionId) { error.actionId = actionId; error.action_id = actionId }
    if (jobId) {
      error.jobId = jobId
      error.job_id = jobId
    }
    if (ambiguous) {
      error.ambiguous = true
      error.submissionMayHaveSucceeded = true
    }
    return error
  } catch {
    const wrapped = new Error(error?.message || 'scope report request failed', { cause: error })
    if (actionId) { wrapped.actionId = actionId; wrapped.action_id = actionId }
    if (jobId) { wrapped.jobId = jobId; wrapped.job_id = jobId }
    if (ambiguous) { wrapped.ambiguous = true; wrapped.submissionMayHaveSucceeded = true }
    return wrapped
  }
}
