import {
  abandonConceptLens, createIdempotencyKey, jobAwait, submitConceptLens,
} from './api.js'

const PREFIX = 'll.concept-lens.'
const KEYS = new Set([
  'version', 'runId', 'generation', 'prompt', 'idempotencyKey', 'state', 'jobId', 'requestId',
  'updatedAt',
])
const STATES = new Set(['ready', 'submitting', 'running', 'unknown'])
const UUID = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i
const GENERATION = /^[0-9a-f]{64}$/
const REQUEST_ID = /^[0-9a-f]{64}$/
const safeText = (value, max = 200) => typeof value === 'string' && value.length > 0
  && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value)
const validGeneration = value => typeof value === 'string' && GENERATION.test(value)
const validRequestId = value => value === null
  || (typeof value === 'string' && REQUEST_ID.test(value))
const validPrompt = value => typeof value === 'string' && value === value.trim() && !!value
  && value.length <= 800 && new TextEncoder().encode(value).length <= 2048
const plausibleTime = value => Number.isSafeInteger(value) && value >= 1_577_836_800_000
  && value <= Date.now() + 300_000
const storageKey = runId => PREFIX + encodeURIComponent(String(runId || ''))
const storageTarget = storage => {
  if (storage !== undefined) return storage
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage }
  catch { return null }
}
const storageError = (cause, code = 'CONCEPT_LENS_STORAGE_UNAVAILABLE') => Object.assign(
  new Error(code === 'CONCEPT_LENS_INTENT_CONFLICT'
    ? 'A different paid concept-lens request is already saved for this run.'
    : 'Paid concept-lens recovery storage is unavailable.'),
  { code, cause },
)

const validateIntent = (payload, runId) => {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || Object.keys(payload).some(key => !KEYS.has(key)) || Object.keys(payload).length !== KEYS.size
      || payload.version !== 1 || payload.runId !== String(runId)
      || !safeText(payload.runId, 255) || !validGeneration(payload.generation)
      || !validPrompt(payload.prompt) || !UUID.test(payload.idempotencyKey)
      || !STATES.has(payload.state) || !(payload.jobId === null || safeText(payload.jobId))
      || !validRequestId(payload.requestId) || (payload.state === 'running' && !payload.jobId)
      || (payload.state === 'running' && !payload.requestId)
      || (['ready', 'submitting'].includes(payload.state)
        && (payload.jobId !== null || payload.requestId !== null))
      || (payload.jobId !== null && payload.requestId === null)
      || !plausibleTime(payload.updatedAt)) throw storageError()
  return payload
}

export function peekConceptLensIntent(runId, storage = undefined) {
  const backing = storageTarget(storage)
  if (!backing || !safeText(String(runId || ''), 255)) throw storageError()
  try {
    const raw = backing.getItem(storageKey(runId))
    if (raw == null || raw === '') return null
    let payload
    try { payload = JSON.parse(raw) } catch (cause) { throw storageError(cause) }
    return validateIntent(payload, runId)
  } catch (cause) {
    if (cause?.code === 'CONCEPT_LENS_STORAGE_UNAVAILABLE') throw cause
    throw storageError(cause)
  }
}

const commit = (runId, payload, backing) => {
  const serialized = JSON.stringify(payload)
  try {
    backing.setItem(storageKey(runId), serialized)
    if (backing.getItem(storageKey(runId)) !== serialized) throw new Error('storage write failed')
    return payload
  } catch (cause) { throw storageError(cause) }
}

// Commit the exact run/generation/prompt before transport. Existing intent wins only when all three
// semantic inputs match, so reload and retry can never manufacture a second paid identity.
export function acquireConceptLensIntent(runId, generation, prompt, storage = undefined) {
  const backing = storageTarget(storage)
  if (!backing || !safeText(String(runId || ''), 255) || !validGeneration(generation)
      || !validPrompt(prompt)) throw storageError()
  const existing = peekConceptLensIntent(runId, backing)
  if (existing) {
    if (existing.generation === generation && existing.prompt === prompt) return existing
    throw storageError(null, 'CONCEPT_LENS_INTENT_CONFLICT')
  }
  return commit(runId, {
    version: 1, runId: String(runId), generation, prompt,
    idempotencyKey: createIdempotencyKey(), state: 'ready', jobId: null, requestId: null,
    updatedAt: Date.now(),
  }, backing)
}

export function updateConceptLensIntent(runId, idempotencyKey, update, storage = undefined) {
  const backing = storageTarget(storage)
  if (!backing) throw storageError()
  const current = peekConceptLensIntent(runId, backing)
  if (!current || current.idempotencyKey !== idempotencyKey) {
    throw storageError(null, 'CONCEPT_LENS_INTENT_CONFLICT')
  }
  const next = {
    ...current, state: update?.state ?? current.state,
    jobId: update?.jobId === undefined ? current.jobId : update.jobId,
    requestId: update?.requestId === undefined ? current.requestId : update.requestId,
    updatedAt: Date.now(),
  }
  return commit(runId, validateIntent(next, runId), backing)
}

export function clearConceptLensIntent(runId, idempotencyKey, storage = undefined) {
  const backing = storageTarget(storage)
  if (!backing) throw storageError()
  const current = peekConceptLensIntent(runId, backing)
  if (!current) return true
  if (current.idempotencyKey !== idempotencyKey) return false
  try {
    backing.setItem(storageKey(runId), '')
    if (backing.getItem(storageKey(runId)) !== '') throw new Error('storage write failed')
    try { backing.removeItem(storageKey(runId)) } catch { /* verified tombstone wins */ }
    return true
  } catch (cause) { throw storageError(cause) }
}

const protocolError = message => Object.assign(new Error(message), {
  code: 'CONCEPT_LENS_PROTOCOL_ERROR', ambiguous: true, submissionMayHaveSucceeded: true,
})
const normalizeJobResult = value => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw protocolError('Invalid paid concept-lens receipt.')
  }
  // Generic jobs use `status: done`, shadowing the frame status. Restore it only from the frame's
  // independently validated completeness bit; ConceptView still validates the entire projection.
  return value.status === 'done' && typeof value.complete === 'boolean'
    ? { ...value, status: value.complete ? 'complete' : 'partial' } : value
}
const validateReceipt = (value, intent) => {
  const result = normalizeJobResult(value)
  if (result.ambiguous === true) {
    // Locally generated timeout/contact-loss receipts intentionally omit generation/request identity.
    // If an ambiguous server/job receipt does carry identity, however, it must be well-formed and
    // remain bound to the receipt already saved before dispatch; ambiguity is never permission to
    // replace a known request or job with another one.
    if ((Object.hasOwn(result, 'generation') && result.generation !== intent.generation)
        || (Object.hasOwn(result, 'request_id')
          && (typeof result.request_id !== 'string' || !REQUEST_ID.test(result.request_id)
            || (intent.requestId && result.request_id !== intent.requestId)
            || (!intent.requestId && !Object.hasOwn(result, 'generation'))))
        || (Object.hasOwn(result, 'job_id')
          && (!safeText(result.job_id) || result.job_id !== intent.jobId))) {
      throw protocolError('Ambiguous paid concept-lens receipt does not match this request.')
    }
    return result
  }
  if (result.status === 'running') {
    if (!safeText(result.job_id) || result.generation !== intent.generation
        || typeof result.request_id !== 'string' || !REQUEST_ID.test(result.request_id)
        || (intent.requestId && result.request_id !== intent.requestId)) {
      throw protocolError('Invalid running paid concept-lens receipt.')
    }
    return result
  }
  if (result.generation !== intent.generation || typeof result.request_id !== 'string'
      || !REQUEST_ID.test(result.request_id)
      || (intent.requestId && result.request_id !== intent.requestId)) {
    throw protocolError('Paid concept-lens receipt does not match this request.')
  }
  return result
}

export async function requestConceptLens(runId, intent, {
  signal, onReceipt, pollIntervalMs = 1000, pollTimeoutMs = 45000,
  requestTimeoutMs = 8000,
} = {}) {
  validateIntent(intent, runId)
  const submit = () => submitConceptLens(runId, intent.prompt, intent.generation, {
    idempotencyKey: intent.idempotencyKey, signal, requestTimeoutMs,
  })
  const poll = response => jobAwait(response, {
    intervalMs: Math.max(0, pollIntervalMs), timeoutMs: Math.max(1, pollTimeoutMs),
    maxTransientErrors: 3, requestTimeoutMs, signal,
  })
  let response
  if (intent.jobId) {
    response = await poll({ status: 'running', job_id: intent.jobId })
    if (response?.code === 'job_unknown') response = await submit()
  } else response = await submit()
  response = validateReceipt(response, intent)
  if (response.status !== 'running') return response
  try {
    onReceipt?.({ state: 'running', jobId: response.job_id, requestId: response.request_id })
  } catch (cause) {
    const error = storageError(cause)
    error.submissionMayHaveSucceeded = true
    throw error
  }
  return validateReceipt(await poll(response), {
    ...intent, jobId: response.job_id, requestId: response.request_id,
  })
}

export async function abandonUnknownConceptLens(runId, intent, options = {}) {
  validateIntent(intent, runId)
  if (intent.state !== 'unknown' || !intent.requestId) {
    throw protocolError('Only a reconciled unknown paid concept-lens claim can be abandoned.')
  }
  const response = validateReceipt(await abandonConceptLens(
    runId, intent.generation, intent.requestId, {
      ...options, idempotencyKey: intent.idempotencyKey,
    }), intent)
  if (response.ambiguous === true) return response
  if (response.code === 'concept_lens_abandoned'
      && (response.ok !== false || response.reason !== 'operator_abandoned'
        || response.resolved !== true || response.provider_outcome !== 'unknown'
        || response.billing_status !== 'unknown'
        || typeof response.warning !== 'string' || !response.warning)) {
    throw protocolError('Invalid paid concept-lens abandonment receipt.')
  }
  return response
}
