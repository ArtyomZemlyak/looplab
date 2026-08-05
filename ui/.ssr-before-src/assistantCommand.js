import { getRunCommand, isTransientCommandReadError, runCommand } from './api.js'

const INTENTS = {
  stop: () => ({ type: 'pause', data: {} }),
  pause: () => ({ type: 'pause', data: {} }),
  finalize: () => ({ type: 'run_abort', data: { reason: 'finalized' } }),
  abort: () => ({ type: 'run_abort', data: { reason: 'finalized' } }),
  resume: () => ({ type: 'resume', data: {} }),
  ratify: () => ({ type: 'spec_approved', data: {} }),
  approve: (arg, nodeGeneration) => ({
    type: 'approval_granted',
    data: { node_id: Number(arg), generation: Number(nodeGeneration) },
  }),
}
const activeSubmissions = new Map()

export function assistantDirectIntent(action, arg = null, nodeGeneration = null) {
  const create = INTENTS[action]
  if (!create) throw new Error(`Unsupported direct command: ${action}`)
  if (action === 'approve'
      && (arg == null || !Number.isSafeInteger(Number(arg)) || Number(arg) < 0)) {
    throw new Error('Approve requires a valid node id')
  }
  if (action === 'approve' && (nodeGeneration == null
      || !Number.isSafeInteger(Number(nodeGeneration)) || Number(nodeGeneration) < 0)) {
    throw new Error('Approve requires the exact node generation')
  }
  return create(arg, nodeGeneration)
}

// Rebuild the in-memory controller from the strict persisted envelope. Keep the node lifecycle
// token alongside the run generation: an id-less /approve recovery must submit the exact attempt the
// operator saw, never rebind the same durable key to whichever attempt happens to be current now.
export function restoreAssistantDirectEntry(saved, spec, runId, extra = {}) {
  return {
    ...extra,
    name: saved.action,
    spec,
    arg: saved.arg,
    nodeGeneration: saved.nodeGeneration,
    runId,
    expectedGeneration: saved.expectedGeneration,
    idempotencyKey: saved.idempotencyKey,
  }
}

// The Assistant owns polling so its visible state changes as soon as the POST returns accepted; it
// does not sit in a bounded API wait while rendering a misleading "submitting" state.
export function submitAssistantDirect(runId, action, arg, idempotencyKey, {
  expectedGeneration, nodeGeneration = null, execute = runCommand, onRecord = null,
} = {}) {
  const intent = assistantDirectIntent(action, arg, nodeGeneration)
  const key = `${String(runId)}\u0000${String(idempotencyKey)}\u0000${String(expectedGeneration)}\u0000${String(nodeGeneration)}`
  const active = activeSubmissions.get(key)
  if (active) return active
  const request = Promise.resolve().then(() => execute(runId, intent.type, intent.data, {
    idempotencyKey, expectedGeneration, waitMs: 0, submitRetries: 0, onRecord,
  }))
  activeSubmissions.set(key, request)
  request.finally(() => { if (activeSubmissions.get(key) === request) activeSubmissions.delete(key) })
    .catch(() => {})
  return request
}

export function pollAssistantDirectOnce(runId, record, { observe = getRunCommand } = {}) {
  if (!record?.id || (record.status !== 'accepted' && record.status !== 'executing')) return Promise.resolve(record)
  return observe(runId, record.id)
}

export function assistantDirectObservationKind(error) {
  if (error?.status === 401 || error?.status === 403) return 'access'
  if (error?.code === 'COMMAND_PROTOCOL_ERROR') return 'protocol'
  if (error?.status === 404) return 'missing'
  return isTransientCommandReadError(error) ? 'transport' : 'request'
}

export function assistantDirectStatus(entry) {
  if (!entry) return ''
  if (entry.checking) return 'Checking the same command…'
  if (entry.statusUnavailable) {
    if (entry.observationKind === 'access') return 'Owner access required to check the pending command'
    if (entry.observationKind === 'protocol') return 'Invalid command response — the same intent is preserved'
    if (entry.observationKind === 'missing') return 'Command record is missing — refresh run state before acting'
    return 'Command status unavailable — the same intent is preserved'
  }
  if (entry.record?.status === 'submitting') return `Submitting /${entry.name || entry.action}…`
  return `Waiting for /${entry.name || entry.action}…`
}

export function presentAssistantCommandResult(currentRunId, resultRunId, present) {
  if (String(currentRunId ?? '') !== String(resultRunId ?? '')) return false
  present()
  return true
}

export const assistantRunChanged = (previousRunId, nextRunId) =>
  String(previousRunId ?? '') !== String(nextRunId ?? '')

// A pre-POST persistence failure can leave a staged (committed:false) envelope and its own id-less
// lock when the final storage write fails. That lock must not hide the actionable failure UI. Exact
// identity + no command id ensures we never ignore a real accepted command or another surface's lock.
export function assistantStorageFailureOwnsLock(failure, lock) {
  return failure?.record?.error?.code === 'command_storage_unavailable'
    && lock?.source === 'assistant' && !lock.commandId
    && String(failure?.runId ?? '') === String(lock?.runId ?? '')
    && failure?.name === lock?.action
    && failure?.idempotencyKey === lock?.idempotencyKey
    && failure?.expectedGeneration === lock?.expectedGeneration
}
