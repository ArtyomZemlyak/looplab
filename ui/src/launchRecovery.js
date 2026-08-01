import { deadlineRequest } from './requestDeadline.js'

const STARTED_STATUSES = new Set(['executing', 'succeeded'])
const PENDING_STATUSES = new Set(['preparing', 'accepted', 'pending'])
const RETRYABLE_STATUSES = new Set(['not_started', 'failed', 'rejected'])
const MAX_STATUS_POLL_WINDOW_MS = 15_000
const MAX_STATUS_POLL_DELAY_MS = 2_000
const MAX_STATUS_POLLS = 24

const boundedMs = (value, fallback, maximum, minimum = 1) => {
  const parsed = Number(value)
  return Number.isFinite(parsed)
    ? Math.max(minimum, Math.min(maximum, Math.trunc(parsed)))
    : fallback
}

// A status response authorizes navigation or a fresh paid intent only when it names the exact run
// being observed and carries an internally consistent terminal state. Everything else retains the
// durable key and stays observational.
export function launchStatusOutcome(result, expectedRunId) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    return { kind: 'unknown', reason: 'protocol' }
  }
  const runId = typeof result.run_id === 'string' ? result.run_id : ''
  if (!runId || runId !== String(expectedRunId || '')) {
    return { kind: 'unknown', reason: 'identity' }
  }
  if (typeof result.started !== 'boolean'
      || typeof result.can_retry !== 'boolean'
      || typeof result.paid_effect_unknown !== 'boolean') {
    return { kind: 'unknown', runId, reason: 'protocol' }
  }
  const status = String(result.status || result.state || '').toLowerCase()
  if (result.paid_effect_unknown === true) {
    return { kind: 'unknown-paid', runId, status }
  }
  if (STARTED_STATUSES.has(status) && result.started !== false && result.can_retry !== true) {
    return { kind: 'started', runId, status }
  }
  if (result.can_retry === true && RETRYABLE_STATUSES.has(status) && result.started !== true) {
    return { kind: 'retryable', runId, status }
  }
  if (result.started === true || result.can_retry === true) {
    return { kind: 'unknown', runId, status, reason: 'protocol' }
  }
  if (PENDING_STATUSES.has(status)) return { kind: 'pending', runId, status }
  return { kind: 'unknown', runId, status, reason: 'unverified' }
}

const pause = ms => new Promise(resolve => setTimeout(resolve, ms))

// Polling is GET-only and has one hard wall-clock window. `read` also receives the remaining budget
// so its own per-request deadline can never extend this observation into an indefinite busy state.
export async function pollLaunchStatus(read, {
  expectedRunId, waitMs = 6_000, pollMs = 500,
} = {}) {
  const windowMs = boundedMs(waitMs, 6_000, MAX_STATUS_POLL_WINDOW_MS)
  const delayMs = boundedMs(pollMs, 500, MAX_STATUS_POLL_DELAY_MS, 50)
  const deadline = Date.now() + windowMs
  let result = null
  let outcome = { kind: 'pending', runId: String(expectedRunId || '') }

  for (let attempt = 0; attempt < MAX_STATUS_POLLS; attempt += 1) {
    const remaining = Math.max(1, deadline - Date.now())
    result = await deadlineRequest(() => read(remaining), remaining).promise
    outcome = launchStatusOutcome(result, expectedRunId)
    if (outcome.kind !== 'pending') return { result, outcome, timedOut: false }

    const afterRead = deadline - Date.now()
    if (afterRead <= 0) return { result, outcome, timedOut: true }
    await pause(Math.min(delayMs, afterRead))
    if (Date.now() >= deadline) return { result, outcome, timedOut: true }
  }
  return { result, outcome, timedOut: true }
}
