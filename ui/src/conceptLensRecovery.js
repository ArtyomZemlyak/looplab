import {
  abandonConceptLens, abandonRecoveredConceptLens, awaitConceptLensRecoveryJob,
  createIdempotencyKey, getConceptLensRecovery, jobAwait, submitConceptLens,
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
const JOB_ID = /^[0-9a-f]{16}$/
const RECOVERY_STATES = new Set(['none', 'running', 'orphaned', 'terminal', 'conflict'])
const ABANDON_REASONS = new Set(['operator_abandoned', 'operator_recovered_abandon'])
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
      || !STATES.has(payload.state) || !(payload.jobId === null || JOB_ID.test(payload.jobId))
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
const recoveryProtocolError = message => Object.assign(new Error(message), {
  code: 'CONCEPT_LENS_RECOVERY_PROTOCOL_ERROR', ambiguous: true,
})
const validAbandonmentReceipt = result => result?.code !== 'concept_lens_abandoned'
  || (result.ok === false && ABANDON_REASONS.has(result.reason)
    && result.abandoned === true
    && result.resolved === true && result.provider_outcome === 'unknown'
    && result.billing_status === 'unknown'
    && safeText(result.warning, 500))
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
    if (result.code === 'job_identity_mismatch'
        || (Object.hasOwn(result, 'generation') && result.generation !== intent.generation)
        || (Object.hasOwn(result, 'request_id')
          && (typeof result.request_id !== 'string' || !REQUEST_ID.test(result.request_id)
            || (intent.requestId && result.request_id !== intent.requestId)
            || (!intent.requestId && !Object.hasOwn(result, 'generation'))))
        || (Object.hasOwn(result, 'job_id')
          && (!JOB_ID.test(result.job_id || '') || result.job_id !== intent.jobId))) {
      throw protocolError('Ambiguous paid concept-lens receipt does not match this request.')
    }
    return result
  }
  if (!validAbandonmentReceipt(result)) {
    throw protocolError('Invalid paid concept-lens abandonment receipt.')
  }
  if (result.status === 'running') {
    if (!JOB_ID.test(result.job_id || '') || result.generation !== intent.generation
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

const exactKeys = (value, names) => {
  const expected = names.split(' ')
  return Object.keys(value).length === expected.length
    && expected.every(name => Object.hasOwn(value, name))
}
const safeSequence = (value, minimum = 0) => Number.isSafeInteger(value) && value >= minimum
const terminalAfterClaim = (value, startedSeq) => safeSequence(value?.seq)
  && value.seq > startedSeq

// This is a security boundary, not a permissive API decoder. The projection is what lets a tab
// decide that creating another paid identity is safe, so unknown/missing fields and identity drift
// fail closed. The server intentionally excludes prompt, paid key and prompt digest.
export function validateConceptLensRecovery(value, expectedGeneration) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || value.schema !== 1 || value.generation !== expectedGeneration
      || !validGeneration(value.generation) || !RECOVERY_STATES.has(value.state)) {
    throw recoveryProtocolError('Invalid paid concept-lens recovery projection.')
  }
  if (value.state === 'none') {
    if (!exactKeys(value, 'schema generation state')) {
      throw recoveryProtocolError('Invalid empty paid concept-lens recovery projection.')
    }
    return value
  }
  if (value.state === 'conflict') {
    if (!exactKeys(value, 'schema generation state code message')
        || !safeText(value.code) || !safeText(value.message, 500)
        || !value.code.startsWith('concept_lens_recovery_')) {
      throw recoveryProtocolError('Invalid conflicting paid concept-lens recovery projection.')
    }
    return value
  }
  const identityKeys = 'schema generation state request_id started_seq input_seq'
  if (!REQUEST_ID.test(value.request_id || '') || !safeSequence(value.started_seq)
      || !safeSequence(value.input_seq, -1) || value.input_seq >= value.started_seq) {
    throw recoveryProtocolError('Invalid paid concept-lens recovery identity.')
  }
  if (value.state === 'orphaned') {
    if (!exactKeys(value, identityKeys)) {
      throw recoveryProtocolError('Invalid orphaned paid concept-lens recovery projection.')
    }
    return value
  }
  if (value.state === 'running') {
    if (!exactKeys(value, `${identityKeys} job_id status`)
        || !JOB_ID.test(value.job_id || '') || !['running', 'done'].includes(value.status)) {
      throw recoveryProtocolError('Invalid running paid concept-lens recovery projection.')
    }
    return value
  }
  if (!exactKeys(value, `${identityKeys} terminal`)) {
    throw recoveryProtocolError('Invalid terminal paid concept-lens recovery projection.')
  }
  const terminal = validateReceipt(value.terminal, {
    generation: value.generation, requestId: value.request_id, jobId: null,
  })
  if (terminal.ambiguous === true || terminal.status === 'running') {
    throw recoveryProtocolError('Paid concept-lens recovery did not contain a terminal receipt.')
  }
  if (!terminalAfterClaim(terminal, value.started_seq)) {
    throw recoveryProtocolError('Paid concept-lens terminal does not follow its durable claim.')
  }
  return { ...value, terminal }
}

export const createConceptLensResolutionKey = () => createIdempotencyKey()

export async function discoverConceptLensRecovery(runId, generation, options = {}) {
  return validateConceptLensRecovery(
    await getConceptLensRecovery(runId, generation, options), generation)
}

// A discovered job is polled by job id only. In particular this path has no prompt and no paid
// Idempotency-Key, so an expired job receipt can only trigger a fresh discovery read, never provider
// resubmission. The caller owns that refresh because it also owns navigation/generation fencing.
export async function pollDiscoveredConceptLens(runId, recovery, {
  signal, pollIntervalMs = 1000, pollTimeoutMs = 45000, requestTimeoutMs = 8000,
} = {}) {
  const inspected = validateConceptLensRecovery(recovery, recovery?.generation)
  if (inspected.state !== 'running') {
    throw recoveryProtocolError('Only a discovered running paid concept-lens job can be polled.')
  }
  const result = validateReceipt(await awaitConceptLensRecoveryJob(inspected.job_id, {
    signal, intervalMs: Math.max(0, pollIntervalMs), timeoutMs: Math.max(1, pollTimeoutMs),
    maxTransientErrors: 3, requestTimeoutMs,
  }), {
    generation: inspected.generation, requestId: inspected.request_id, jobId: inspected.job_id,
  })
  if (result.ambiguous !== true && result.status !== 'running'
      && !terminalAfterClaim(result, inspected.started_seq)) {
    throw recoveryProtocolError('Paid concept-lens job terminal does not follow its durable claim.')
  }
  return result
}

export async function resolveOrphanedConceptLens(
  runId, recovery, resolutionIdempotencyKey, options = {},
) {
  const inspected = validateConceptLensRecovery(recovery, recovery?.generation)
  if (inspected.state !== 'orphaned' || !UUID.test(resolutionIdempotencyKey || '')) {
    throw recoveryProtocolError('An exact orphan receipt and separate resolution key are required.')
  }
  const response = validateReceipt(await abandonRecoveredConceptLens(
    runId, inspected.generation, inspected.request_id, inspected.started_seq, {
      ...options, resolutionIdempotencyKey,
    }), {
    generation: inspected.generation, requestId: inspected.request_id, jobId: null,
  })
  if (response.ambiguous !== true && response.status === 'running') {
    throw recoveryProtocolError('Orphan resolution returned a running receipt.')
  }
  if (response.ambiguous !== true && !terminalAfterClaim(response, inspected.started_seq)) {
    throw recoveryProtocolError('Orphan resolution terminal does not follow its durable claim.')
  }
  return response
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
  if (!validAbandonmentReceipt(response)) {
    throw protocolError('Invalid paid concept-lens abandonment receipt.')
  }
  return response
}
