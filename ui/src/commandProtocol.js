// The durable run-command protocol: bind the displayed run generation, submit once under an
// idempotency key, observe the server-owned record to a terminal status, retry a durable id, and await
// a background job. Split out of api.js (doc 25 UI-02 — bodies verbatim); api.js re-exports
// everything, so importers are unchanged.
//
// `commandFetch` is deliberately not built on requestDeadline.js: it bounds the complete response
// lifecycle of a durable submission and has to surface a timeout as a typed error the retry path can
// classify. It reaches the wire through apiClient.js and it never touches sessionStorage — what a
// recovery envelope may keep is commandStorage.js's answer, not this module's.

import { assertRunMutationAllowed } from './runMode.js'
import {
  _authHeaders, _throw, apiUrl, assertNotReviewMutation, get, reviewReadPath, runApiPath,
} from './apiClient.js'
import {
  COMMAND_FAILED, COMMAND_REQUEST_TIMEOUT_MS, COMMAND_STATUSES, COMMAND_SUCCEEDED,
  createIdempotencyKey, getObservedRunGeneration, isTransientCommandReadError, observeRunGeneration,
  runGenerationError, scopeGenerationErrorIdentity, validRunGeneration,
} from './commandModel.js'

const commandSleep = (ms, signal) => new Promise((resolve, reject) => {
  const abort = () => { clearTimeout(timer); reject(signal.reason) }
  const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, ms)
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
})

function commandProtocolError(path, message, record = null) {
  const error = new Error(`${path}: ${message}`)
  error.code = 'COMMAND_PROTOCOL_ERROR'
  if (record && typeof record === 'object') error.commandRecord = record
  return error
}

export async function getRunGeneration(runId) {
  const payload = await get(runApiPath(runId, '/state'))
  if (payload?.generation == null) {
    throw runGenerationError(
      'run_generation_unavailable',
      'The current run generation is not available yet.',
      'Refresh the run and wait for its initial event before submitting another action.',
    )
  }
  if (!validRunGeneration(payload.generation)) {
    throw runGenerationError(
      'invalid_run_generation',
      'The server returned an invalid run generation.',
      'Refresh the run before submitting another action.',
    )
  }
  observeRunGeneration(runId, payload.generation)
  return payload.generation
}

function validatedCommandRecord(record, path, expectedId = null) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    throw commandProtocolError(path, 'invalid command response')
  }
  if (!String(record.id || '').trim()) {
    throw commandProtocolError(path, 'command response has no id', record)
  }
  if (expectedId != null && String(record.id || '') !== String(expectedId)) {
    throw commandProtocolError(path, 'response command id does not match the request', record)
  }
  if (!COMMAND_STATUSES.has(record.status)) {
    throw commandProtocolError(path, `unexpected command status ${record.status || 'missing'}`, record)
  }
  return record
}

async function commandResponseJson(response, path, { submission = false } = {}) {
  try { return await response.json() }
  catch (cause) {
    const error = commandProtocolError(path, 'response is not valid JSON')
    error.status = response?.status
    error.cause = cause
    if (submission) error.submissionMayHaveSucceeded = true
    throw error
  }
}

const commandTimeoutError = (path, timeout, cause = null) => {
  const error = new Error(`${path}: request timed out after ${timeout}ms`)
  error.code = 'COMMAND_REQUEST_TIMEOUT'
  error.transient = true
  if (cause) error.cause = cause
  return error
}

// The deadline owns the complete response lifecycle, not just receipt of HTTP headers. Keeping the
// same controller/timer alive while the consumer reads and parses the body prevents a stalled JSON
// stream from leaving a command surface pending forever. Promise.race also bounds test doubles and
// runtimes whose body parser does not promptly reject on AbortSignal.
async function commandFetch(path, options = {}, timeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
  consume = response => response) {
  const timeout = Math.max(0, Number(timeoutMs) || 0)
  const controller = typeof AbortController === 'undefined' ? null : new AbortController()
  const externalSignal = options.signal
  const forwardAbort = () => controller?.abort()
  const unlink = () => externalSignal?.removeEventListener?.('abort', forwardAbort)
  externalSignal?.addEventListener?.('abort', forwardAbort, { once: true })
  if (externalSignal?.aborted) forwardAbort()
  const signal = controller?.signal || externalSignal
  let timedOut = false, timer = null
  const work = Promise.resolve().then(async () => {
    const response = await fetch(apiUrl(path), signal ? { ...options, signal } : options)
    return consume(response)
  })
  if (!timeout) return work.finally(unlink)
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true
      controller?.abort()
      reject(commandTimeoutError(path, timeout))
    }, timeout)
  })
  try { return await Promise.race([work, deadline]) }
  catch (cause) {
    if (timedOut) throw cause?.code === 'COMMAND_REQUEST_TIMEOUT'
      ? cause : commandTimeoutError(path, timeout, cause)
    throw cause
  } finally {
    clearTimeout(timer)
    unlink()
  }
}

export const commandJson = (path, options, timeoutMs, { errorPath = path, submission = false } = {}) =>
  commandFetch(path, options, timeoutMs, async response => {
    if (!response.ok) await _throw(response, errorPath)
    return commandResponseJson(response, errorPath, { submission })
  })

export const commandRead = (path, {
  errorPath = path, signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) => commandJson(path, {
  headers: _authHeaders({}), cache: 'no-store', signal,
}, requestTimeoutMs, { errorPath })

const notifyCommandRecord = (callback, record) => {
  if (!callback) return
  try { callback(record) } catch { /* persistence/presentation must not break command execution */ }
}

// `allowRunMutationModes` is the transport tier of the fork-from-a-snapshot carve-out and is the
// SECOND fence that gesture has to be admitted through: `RunView.jsx` decides whether the click is
// allowed, and this decides whether the REQUEST is, from the run-access envelope
// `runMode.js::setRunAccess` publishes. They are deliberately separate — this one holds for a caller
// RunView never saw (a slash command, a recovered intent, another tab's surface) — so the exception
// has to be named at both, and it is named by exactly one caller: `api.js::CONTROL.forkFrom`, with
// `['history']` and nothing else. Empty for every other command, which is the historical behaviour
// byte for byte. Do NOT reach for it to make some other action work in a read-only view: the reason
// this one is admissible is a property of its PAYLOAD (`forkFromSeqModel.js`'s header), not a
// property of the view.
export async function submitRunCommand(runId, type, data = {}, {
  idempotencyKey = createIdempotencyKey(), expectedGeneration,
  requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS, allowRunMutationModes = [],
} = {}) {
  const path = runApiPath(runId, '/commands')
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path, { allowModes: allowRunMutationModes })
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError(
      'invalid_run_generation',
      'A verified run generation is required before submitting a command.',
      'Refresh the run before submitting another action.',
    )
  }
  try {
    const record = await commandJson(path, {
      method: 'POST',
      headers: _authHeaders({ 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }),
      body: JSON.stringify({ type, data: data || {}, expected_generation: expectedGeneration }),
    }, requestTimeoutMs, { submission: true })
    return validatedCommandRecord(record, path)
  }
  catch (error) {
    if (error?.code === 'COMMAND_PROTOCOL_ERROR' || error?.code === 'COMMAND_REQUEST_TIMEOUT') {
      error.submissionMayHaveSucceeded = true
    }
    throw error
  }
}

export async function getRunCommand(runId, commandId, { requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS } = {}) {
  const path = runApiPath(runId, `/commands/${encodeURIComponent(commandId)}`)
  return validatedCommandRecord(await commandRead(path, { requestTimeoutMs }), path, commandId)
}

async function awaitRunCommand(runId, record, {
  waitMs = 8000, pollMs = 250, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS, onRecord = null,
} = {}) {
  notifyCommandRecord(onRecord, record)
  if (COMMAND_SUCCEEDED.has(record.status) || COMMAND_FAILED.has(record.status)) return record
  let last = record
  const deadline = Date.now() + Math.max(0, Number(waitMs) || 0)
  const baseDelay = Math.max(0, Number(pollMs) || 0)
  let nextDelay = baseDelay, transientFailures = 0
  while (Date.now() < deadline) {
    await commandSleep(Math.min(nextDelay, Math.max(0, deadline - Date.now())))
    try {
      const refreshed = await getRunCommand(runId, record.id, { requestTimeoutMs })
      last = refreshed
      notifyCommandRecord(onRecord, last)
      transientFailures = 0; nextDelay = baseDelay
      if (COMMAND_SUCCEEDED.has(last.status) || COMMAND_FAILED.has(last.status)) return last
    } catch (error) {
      if (isTransientCommandReadError(error)) {
        transientFailures += 1
        nextDelay = Math.max(Number(error.retryAfterMs) || 0,
          Math.min(2000, Math.max(25, baseDelay || 25) * (2 ** Math.min(5, transientFailures - 1))))
        continue
      }
      // Let a control surface stop polling and retain the durable command id for an honest recovery
      // message. This is client metadata only; the server error remains untouched.
      error.commandRecord = last
      throw error
    }
  }
  return last
}

export async function retryRunCommand(runId, commandId, {
  waitMs = 8000, pollMs = 250, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS, onRecord = null,
} = {}) {
  const encodedId = encodeURIComponent(commandId)
  const path = runApiPath(runId, `/commands/${encodedId}/retry`)
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  let record
  try {
    record = await commandJson(path, {
      method: 'POST', headers: _authHeaders({}),
    }, requestTimeoutMs, { submission: true })
  } catch (error) {
    // A different active command is not evidence that retrying this failed id succeeded. Propagate
    // the conflict with its separate existingCommandId; observing the old failed record here would
    // mask the conflict and invite repeated contradictory retries.
    if (error?.status === 409 && error?.code === 'command_in_progress') throw error
    // 409 is often a cross-tab race: another owner tab already re-armed this exact id. Observe the
    // record before declaring failure. Transport/timeout/protocol ambiguity follows the same rule.
    if (error?.status !== 409 && !isTransientCommandReadError(error)
        && !error?.submissionMayHaveSucceeded) {
      error.commandRecord = { id: String(commandId), status: 'failed', error: null }
      throw error
    }
    // The retry POST may have reached the server even when its response was lost. Observe the SAME
    // record before offering another click: active/succeeded means recovery was accepted; the old
    // retryable failure means it was not and can still be retried safely.
    try {
      record = await getRunCommand(runId, commandId, { requestTimeoutMs })
    } catch (readError) {
      readError.commandRecord = { id: String(commandId), status: 'accepted' }
      throw readError
    }
  }
  record = validatedCommandRecord(record, path, commandId)
  return awaitRunCommand(runId, record, { waitMs, pollMs, requestTimeoutMs, onRecord })
}

export async function runCommand(runId, type, data = {}, {
  waitMs = 8000, pollMs = 250, idempotencyKey = createIdempotencyKey(), submitRetries = 1,
  retryMs = 150, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS, onRecord = null,
  expectedGeneration = undefined, allowRunMutationModes = [],
} = {}) {
  // New intent: bind once to the current event-log generation. Transport retries and id-less
  // recovery pass this exact token back; they never silently substitute a generation observed later.
  const generation = expectedGeneration === undefined
    ? (getObservedRunGeneration(runId) || await getRunGeneration(runId))
    : expectedGeneration
  if (!validRunGeneration(generation)) {
    throw runGenerationError(
      'invalid_run_generation',
      'A verified run generation is required before submitting a command.',
      'Refresh the run before submitting another action.',
    )
  }
  let record
  const retries = Math.max(0, Math.trunc(Number(submitRetries) || 0))
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      record = await submitRunCommand(runId, type, data, {
        idempotencyKey, expectedGeneration: generation, requestTimeoutMs, allowRunMutationModes,
      })
      notifyCommandRecord(onRecord, record)
      break
    } catch (error) {
      // A fresh browser key may encounter the unresolved identical command created before reload.
      // Attach to the id named by the server; never create another intent.
      if (error?.status === 409 && error?.code === 'retry_existing_command' && error?.existingCommandId) {
        try {
          record = await getRunCommand(runId, error.existingCommandId, { requestTimeoutMs })
          notifyCommandRecord(onRecord, record)
          break
        } catch (readError) {
          readError.commandRecord = { id: error.existingCommandId, status: 'accepted' }
          readError.idempotencyKey = idempotencyKey
          readError.commandUnknown = true
          throw readError
        }
      }
      // A 5xx may arrive after durable acceptance. Replay only transport/5xx failures, using the
      // SAME idempotency key; a 4xx is an authoritative payload/auth/state rejection.
      const retryable = isTransientCommandReadError(error) || error?.submissionMayHaveSucceeded
      if (!retryable || error?.name === 'AbortError' || attempt >= retries) {
        if (retryable) {
          error.idempotencyKey = idempotencyKey
          error.commandUnknown = true
        }
        throw error
      }
      await commandSleep(Math.max(Number(retryMs) || 0, Number(error.retryAfterMs) || 0))
    }
  }
  try {
    return await awaitRunCommand(runId, record, { waitMs, pollMs, requestTimeoutMs, onRecord })
  } catch (error) {
    error.idempotencyKey ||= idempotencyKey
    throw error
  }
}

// Generic background-job poll: the server hands back {status:'running', job_id}
// for slow work so it can't 504 behind a proxy. Returns the final result dict; tolerates transient
// poll errors. `resp` that's already a result (fast inline path) is returned unchanged.
const _job = (jobId, { requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS, signal } = {}) => {
  const path = `/api/jobs/${encodeURIComponent(jobId)}`
  return commandRead(reviewReadPath(path), { errorPath: path, requestTimeoutMs, signal })
}
export async function jobAwait(resp, {
  intervalMs = 1500, timeoutMs = 600000, signal,
  maxTransientErrors = Number.POSITIVE_INFINITY,
  requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  if (!resp || resp.status !== 'running' || !resp.job_id) return resp
  const acceptedJobId = String(resp.job_id)
  const deadline = Date.now() + timeoutMs
  let transientErrors = 0
  try {
    while (Date.now() < deadline) {
      if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError')
      let j
      try {
        const remainingMs = Math.max(1, deadline - Date.now())
        j = await _job(acceptedJobId, {
          requestTimeoutMs: Math.min(requestTimeoutMs, remainingMs), signal,
        })
        transientErrors = 0
      } catch (error) {
        if (!isTransientCommandReadError(error)) throw error
        if (++transientErrors >= maxTransientErrors) {
          return { ok: false, code: 'job_contact_lost', ambiguous: true,
            job_id: acceptedJobId, jobId: acceptedJobId, error: 'job contact lost' }
        }
        await commandSleep(intervalMs, signal)
        continue
      }
      if (!j || typeof j !== 'object' || Array.isArray(j)
          || !['running', 'done', 'unknown'].includes(j.status)) {
        return { ok: false, code: 'job_protocol_error', ambiguous: true,
          job_id: acceptedJobId, jobId: acceptedJobId, error: 'invalid job status' }
      }
      // A terminal payload is data produced by the worker, not authority to redirect the client to
      // another paid identity. Preserve the accepted id for recovery, but make any supplied mismatch
      // explicit so endpoint-specific validators cannot mistake a rewritten receipt for completion.
      const returnedIds = ['job_id', 'jobId']
        .filter(key => Object.hasOwn(j, key)).map(key => j[key])
      if (returnedIds.some(value => typeof value !== 'string' || value !== acceptedJobId)) {
        return { ok: false, code: 'job_identity_mismatch', ambiguous: true,
          job_id: acceptedJobId, jobId: acceptedJobId, error: 'job identity mismatch' }
      }
      if (j.status === 'done') return { ...j, job_id: acceptedJobId, jobId: acceptedJobId }
      if (j.status === 'unknown') return { ok: false, code: 'job_unknown', ambiguous: true,
        job_id: acceptedJobId, jobId: acceptedJobId, error: 'job receipt expired' }
      await commandSleep(intervalMs, signal)
    }
  } catch (error) {
    // a failed observation never erases the already accepted paid identity. This
    // includes local abort, auth/protocol failures, and a response body that failed after the server
    // atomically consumed its terminal job receipt.
    throw scopeGenerationErrorIdentity(error, error?.actionId || '', acceptedJobId, true)
  }
  return { ok: false, code: 'job_timeout', ambiguous: true,
    job_id: acceptedJobId, jobId: acceptedJobId, error: 'job timed out' }
}
