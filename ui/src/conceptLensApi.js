// The paid concept-lens protocol: submit, abandon, and the lost-tab recovery pair that resolves a
// paid identity another tab created. A MEMBER of the api.js barrel (doc 25 UI-02) — it never imports
// api.js back, and api.js re-exports every name below, so no consumer changed.
//
// It is the "ninth concern" doc 25 UI-02 measured but the finding never named: what its resolution
// called "the report-refresh intent store, 46 lines not the 153 implied" was 153 lines of THIS,
// sitting in the range the finding cited. It is one concern by the only test that matters here —
// every function is one paid request against one run's `/concepts/lens*` routes, fenced by the
// generation the operator SAW and keyed by an idempotency identity the caller saved before posting.
//
// Two rules this file exists to keep in one place. First, `paidConceptLensPost` is the ONLY way a
// concept-lens POST leaves the browser: it refuses without a verified run generation and a saved
// idempotency key, and on an ambiguous transport failure it marks the error
// `submissionMayHaveSucceeded` — a paid request whose outcome the browser cannot see must never be
// retried as if nothing happened. Second, recovery is owner-plane even where it reads: see
// `getConceptLensRecovery`'s own comment, which is the reason it does not go through `get()`.
import {
  _authHeaders, assertNotReviewMutation, runApiPath,
} from './apiClient.js'
import {
  COMMAND_REQUEST_TIMEOUT_MS, UUID_V4_RE, runGenerationError, safeIdentityText, validRunGeneration,
} from './commandModel.js'
import { commandJson, commandRead, jobAwait } from './commandProtocol.js'
import { assertRunMutationAllowed } from './runMode.js'

async function paidConceptLensPost(runId, suffix, body, {
  idempotencyKey, signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  if (!validRunGeneration(body?.expected_generation)) {
    throw runGenerationError('invalid_run_generation',
      'A verified run generation is required before paid concept-lens work.',
      'Reload Concepts before continuing this request.')
  }
  if (!safeIdentityText(idempotencyKey)) {
    throw new Error('A valid saved concept-lens idempotency key is required.')
  }
  const path = runApiPath(runId, suffix)
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  try {
    return await commandJson(path, {
      method: 'POST', signal,
      headers: _authHeaders({
        'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey,
      }),
      body: JSON.stringify(body),
    }, requestTimeoutMs, { submission: true })
  } catch (error) {
    if (error?.status == null || error?.code === 'COMMAND_REQUEST_TIMEOUT'
        || error?.code === 'COMMAND_PROTOCOL_ERROR') error.submissionMayHaveSucceeded = true
    throw error
  }
}

export const submitConceptLens = (runId, prompt, expectedGeneration, options) =>
  paidConceptLensPost(runId, '/concepts/lens', {
    prompt, expected_generation: expectedGeneration,
  }, options)

export const abandonConceptLens = (runId, expectedGeneration, requestId, options) =>
  paidConceptLensPost(runId, '/concepts/lens/abandon', {
    expected_generation: expectedGeneration, request_id: requestId,
  }, options)

// Lost-tab recovery is deliberately owner-plane even though discovery is a GET.  Do not route it
// through get(): reviewReadPath() would translate the URL into a reviewer capability, while this
// projection is the authority used to decide whether another paid identity may be created.
export async function getConceptLensRecovery(runId, expectedGeneration, {
  signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError('invalid_run_generation',
      'A verified run generation is required before paid concept-lens recovery.',
      'Reload Concepts before inspecting paid work.')
  }
  const basePath = runApiPath(runId, '/concepts/lens/recovery')
  assertNotReviewMutation(basePath)
  const path = `${basePath}?expected_generation=${encodeURIComponent(expectedGeneration)}`
  return commandRead(path, { errorPath: basePath, signal, requestTimeoutMs })
}

export function awaitConceptLensRecoveryJob(jobId, options = {}) {
  const path = `/api/jobs/${encodeURIComponent(String(jobId || ''))}`
  assertNotReviewMutation(path)
  if (!/^[0-9a-f]{16}$/.test(jobId || '')) {
    throw new Error('An exact recovered concept-lens job id is required.')
  }
  return jobAwait({ status: 'running', job_id: jobId }, options)
}

export async function abandonRecoveredConceptLens(
  runId, expectedGeneration, requestId, expectedStartedSeq, {
    resolutionIdempotencyKey, signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
  } = {},
) {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError('invalid_run_generation',
      'A verified run generation is required before resolving recovered paid work.',
      'Reload Concepts and inspect the current recovery receipt.')
  }
  if (!safeIdentityText(requestId) || !/^[0-9a-f]{64}$/.test(requestId)) {
    throw new Error('An exact recovered concept-lens request id is required.')
  }
  if (!Number.isSafeInteger(expectedStartedSeq) || expectedStartedSeq < 0) {
    throw new Error('An exact recovered concept-lens start sequence is required.')
  }
  if (!UUID_V4_RE.test(resolutionIdempotencyKey || '')) {
    throw new Error('A valid recovery resolution idempotency key is required.')
  }
  const path = runApiPath(runId, '/concepts/lens/recovery/abandon')
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  try {
    return await commandJson(path, {
      method: 'POST', signal,
      headers: _authHeaders({
        'Content-Type': 'application/json',
        'Resolution-Idempotency-Key': resolutionIdempotencyKey,
      }),
      body: JSON.stringify({
        expected_generation: expectedGeneration,
        request_id: requestId,
        expected_started_seq: expectedStartedSeq,
      }),
    }, requestTimeoutMs, { submission: true })
  } catch (error) {
    if (error?.status == null || error?.code === 'COMMAND_REQUEST_TIMEOUT'
        || error?.code === 'COMMAND_PROTOCOL_ERROR') error.submissionMayHaveSucceeded = true
    throw error
  }
}
