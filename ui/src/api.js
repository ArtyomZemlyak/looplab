// The UI's server API: the fetch client (get/post/send wrappers + auth/prefix plumbing), the generic
// background-job await, every /api/* endpoint function, and the CONTROL action map. Split out of
// util.js (mega-refactor P5.2 — bodies verbatim); util.js re-exports everything, so importers are
// unchanged.

import { assertRunMutationAllowed } from './runMode.js'
import { splitRouteHash } from './runRouteState.js'

const OWNER_TOKEN_KEY = 'll.owner-token'
let volatileOwnerToken = ''

// One constructor for every owner-style per-run endpoint. Run IDs are filesystem names rather than
// URL slugs and may legitimately contain URL syntax such as `#` or a literal `%2F`; interpolating
// them directly can therefore drop the fragment or turn one path segment into several. Keep the
// suffix explicit at call sites while making the identity boundary impossible to forget.
export const runApiPath = (runId, suffix = '') =>
  `/api/runs/${encodeURIComponent(String(runId))}${suffix}`

// Node identity is currently numeric, but keeping the second dynamic segment encoded makes that
// contract robust to imported/legacy identifiers and prevents future callers from weakening the
// already-safe run boundary while composing a node endpoint.
export const runNodeApiPath = (runId, nodeId, suffix = '') =>
  runApiPath(runId, `/nodes/${encodeURIComponent(String(nodeId))}${suffix}`)

export function isReviewLocation(loc = (typeof location !== 'undefined' ? location : null)) {
  return !!loc && /\/review\/?$/.test(loc.pathname || '')
}

export function reviewTokenFromLocation(loc = (typeof location !== 'undefined' ? location : null)) {
  if (!isReviewLocation(loc)) return ''
  // Diagnostic state follows the bearer inside the fragment (`#/rv_…?node=4`).  Parse only the
  // route portion: the credential never moves into the HTTP path/query and forged suffix state can
  // neither extend the token nor make review mode fall back to an owner credential.
  const m = splitRouteHash(loc.hash || '').path.match(/^\/(rv_[A-Za-z0-9_-]+)$/)
  return m ? m[1] : ''
}

function ownerToken() {
  if (typeof sessionStorage === 'undefined') return volatileOwnerToken
  try { return sessionStorage.getItem(OWNER_TOKEN_KEY) || volatileOwnerToken } catch { return volatileOwnerToken }
}

export function setOwnerToken(token) {
  volatileOwnerToken = token ? String(token) : ''
  if (typeof sessionStorage === 'undefined') return
  try {
    if (volatileOwnerToken) sessionStorage.setItem(OWNER_TOKEN_KEY, volatileOwnerToken)
    else sessionStorage.removeItem(OWNER_TOKEN_KEY)
  } catch { /* module memory keeps this tab usable when session storage is disabled */ }
}

export const COMMAND_SUCCEEDED = new Set(['succeeded', 'noop'])
export const COMMAND_FAILED = new Set(['failed', 'rejected', 'timed_out'])
const COMMAND_PENDING = new Set(['accepted', 'executing'])
const COMMAND_STATUSES = new Set([...COMMAND_SUCCEEDED, ...COMMAND_FAILED, ...COMMAND_PENDING])
const TRANSIENT_HTTP = new Set([408, 425, 429])
const COMMAND_REQUEST_TIMEOUT_MS = 8000
const TRANSPORT_STORAGE_PREFIX = 'll.command-transport.'
const TRANSPORT_ACTIONS = new Set(['stop', 'finalize', 'resume'])
const ASSISTANT_TRANSPORT_STORAGE_PREFIX = 'll.assistant-command-transport.'
const ASSISTANT_TRANSPORT_ACTIONS = new Set(['stop', 'finalize', 'resume', 'pause', 'abort', 'ratify', 'approve'])
const RUN_COMMAND_LOCK_PREFIX = 'll.command-lock.'
const LAUNCH_TRANSPORT_PREFIX = 'll.launch-transport.'
const RUN_COMMAND_LOCK_EVENT = 'll:command-lock'
const LAUNCH_TRANSPORT_EVENT = 'll:launch-transport'
const COMMAND_ID_RE = /^cmd_[0-9a-f]{32}$/
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
const UUID_V4_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i
const validRunGeneration = value => typeof value === 'string' && RUN_GENERATION_RE.test(value)
const observedRunGenerations = new Map()
const MAX_OBSERVED_RUN_GENERATIONS = 512   // exceeds any realistic in-view working set; bounds the map
const STORED_COMMAND_STATUSES = new Set(['submitting', ...COMMAND_STATUSES])
const OBSERVATION_KINDS = new Set([null, 'transport', 'access', 'protocol', 'missing', 'request'])
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
const STORED_ERROR_KEYS = new Set(['code', 'retryable', 'existing_command_id'])
const STORED_ERROR_CODES = new Set([
  'command_failed', 'command_request_failed', 'command_request_timeout',
  'owner_access_required', 'command_protocol_error', 'command_record_missing',
  'command_storage_unavailable', 'command_timeout', 'postcondition_timeout',
  'invalid_command', 'command_target_not_found', 'command_intent_missing',
  'command_not_retryable', 'command_in_progress', 'retry_existing_command',
  'finalize_payload_conflict', 'finalize_in_progress', 'engine_finishing',
  'engine_start_uncertain', 'spawn_claim_confirmation_required', 'engine_failed',
  'spawn_failed', 'command_worker_failed', 'approval_not_requested',
  'ratification_not_requested', 'invalid_transition',
  'invalid_run_generation', 'run_generation_changed', 'run_generation_unavailable',
])
const STORED_ERROR_COPY = Object.freeze({
  command_failed: ['Command failed', 'Refresh run state before acting again.'],
  command_request_failed: ['The command request failed', 'Refresh run state before acting again.'],
  command_request_timeout: ['The command request timed out', 'Check the same durable command before acting again.'],
  owner_access_required: ['Owner access is required', 'Restore owner access, then check the same command.'],
  command_protocol_error: ['The server returned an invalid command response', 'Check the same durable command; do not submit a new intent.'],
  command_record_missing: ['The durable command record is missing', 'Refresh run state before acting again.'],
  command_storage_unavailable: ['Durable tab storage is unavailable', 'Enable session storage or free browser storage, then try again.'],
  command_timeout: ['The command timed out', 'Retry this same command only when the interface offers it.'],
  postcondition_timeout: ['The command did not reach its expected state in time', 'Check or retry this same command.'],
  invalid_command: ['The command was rejected as invalid', 'Correct the action and submit a new intent.'],
  command_target_not_found: ['The command target no longer exists', 'Refresh run state before acting again.'],
  command_intent_missing: ['The durable command intent is missing or changed', 'Inspect the run before acting again.'],
  command_not_retryable: ['This command cannot be retried', 'Refresh run state before choosing another action.'],
  command_in_progress: ['Another run command is already in progress', 'Wait for the active command to finish.'],
  retry_existing_command: ['An identical command already exists', 'Observe the existing command.'],
  finalize_payload_conflict: ['A different finalization is already pending', 'Observe the existing finalization.'],
  finalize_in_progress: ['Finalization is still in progress', 'Wait for finalization to finish.'],
  engine_finishing: ['The engine is finishing terminal write-out', 'Wait until the engine stops, then check again.'],
  engine_start_uncertain: ['Engine startup could not be confirmed', 'Check this same command; do not launch another driver.'],
  spawn_claim_confirmation_required: ['Engine startup needs confirmation', 'Resolve the startup state before acting again.'],
  engine_failed: ['The run engine reported a failure', 'Correct the run error, then retry this same command if offered.'],
  spawn_failed: ['The run engine could not be started', 'Correct the startup problem, then retry this same command if offered.'],
  command_worker_failed: ['The command worker failed', 'Correct the cause, then retry this same command if offered.'],
  approval_not_requested: ['The run is not awaiting approval', 'Approve only while approval is requested.'],
  ratification_not_requested: ['The run is not awaiting ratification', 'Ratify only while specification approval is requested.'],
  invalid_transition: ['The run cannot perform that transition now', 'Refresh run state and choose an available action.'],
  invalid_run_generation: ['The run generation could not be verified', 'Refresh the run before submitting another action.'],
  run_generation_changed: ['This run was reset or replaced before the command arrived', 'Refresh the run, review its current state, then submit a new command.'],
  run_generation_unavailable: ['The current run generation is temporarily unavailable', 'Refresh the run and wait for its initial event before submitting another action.'],
})
const STORED_RECORD_KEYS = new Set(['id', 'status', 'event_type', 'error'])
const RUN_ENVELOPE_KEYS = new Set([
  'runId', 'action', 'expectedGeneration', 'idempotencyKey', 'commandId', 'record', 'statusUnavailable',
  'observationKind', 'retrying', 'checking', 'updatedAt', 'committed',
])
const ASSISTANT_ENVELOPE_KEYS = new Set([...RUN_ENVELOPE_KEYS, 'arg', 'nodeGeneration'])
const LOCK_KEYS = new Set([
  'runId', 'source', 'action', 'expectedGeneration', 'idempotencyKey', 'commandId', 'status', 'statusUnavailable', 'updatedAt',
])
const LAUNCH_TRANSPORT_KEYS = new Set(['identity', 'runId', 'idempotencyKey', 'updatedAt'])

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

const hasOnlyKeys = (value, allowed) => Object.keys(value).every(key => allowed.has(key))
const safeIdentityText = value => typeof value === 'string' && value.length > 0 && value.length <= 200
  && !/[\u0000-\u001f\u007f]/.test(value)
const sanitizeStoredError = (error, { strict = false } = {}) => {
  if (!error || typeof error !== 'object' || Array.isArray(error)) {
    return strict ? null : { code: 'command_failed', retryable: false }
  }
  if (strict && !hasOnlyKeys(error, STORED_ERROR_KEYS)) return null
  const candidate = String(error.code || '')
  const code = STORED_ERROR_CODES.has(candidate) ? candidate : 'command_failed'
  const stored = {
    code,
    retryable: error.retryable === true,
  }
  if (error.existing_command_id != null) {
    if (!COMMAND_ID_RE.test(String(error.existing_command_id))) return strict ? null : stored
    stored.existing_command_id = String(error.existing_command_id)
  }
  return stored
}

const storedRecord = (record, action, source, { strict = false } = {}) => {
  if (!record || typeof record !== 'object' || Array.isArray(record)) return null
  if (strict && !hasOnlyKeys(record, STORED_RECORD_KEYS)) return null
  const status = String(record.status || '')
  if (!STORED_COMMAND_STATUSES.has(status)) return null
  if (status === 'submitting') {
    if (strict && (record.id != null || record.event_type != null || record.error != null)) return null
    return { status }
  }
  const expectedEvent = commandEventForAction(action, source)
  const id = record.id == null ? '' : String(record.id)
  const eventType = record.event_type == null ? '' : String(record.event_type)
  const serverRecord = COMMAND_ID_RE.test(id) && !!expectedEvent && eventType === expectedEvent
  const localFailure = COMMAND_FAILED.has(status) && !id && !eventType
  if (!serverRecord && !localFailure) return null
  const result = { ...(id ? { id } : {}), status, ...(eventType ? { event_type: eventType } : {}) }
  if (COMMAND_FAILED.has(status)) {
    const error = sanitizeStoredError(record.error, { strict })
    if (!error) return null
    result.error = error
  } else if (strict && record.error != null) return null
  return result
}

export const commandRecordMatchesAction = (record, action, source = 'assistant') =>
  storedRecord(record, action, source) != null

const protocolTransport = (runId, source, payload = null) => {
  const allowedActions = source === 'dock' ? TRANSPORT_ACTIONS : ASSISTANT_TRANSPORT_ACTIONS
  const rawAction = typeof payload?.action === 'string' ? payload.action : ''
  const action = allowedActions.has(rawAction) ? rawAction : 'unknown'
  const rawKey = payload?.idempotencyKey
  const idempotencyKey = safeIdentityText(rawKey) ? rawKey : `invalid-${source}-envelope`
  const topId = COMMAND_ID_RE.test(String(payload?.commandId || '')) ? String(payload.commandId) : ''
  const recordId = COMMAND_ID_RE.test(String(payload?.record?.id || '')) ? String(payload.record.id) : ''
  const commandId = topId && recordId && topId !== recordId ? '' : (topId || recordId)
  const expectedGeneration = validRunGeneration(payload?.expectedGeneration)
    ? String(payload.expectedGeneration) : ''
  return {
    runId: String(runId), action, arg: null, nodeGeneration: null,
    expectedGeneration, idempotencyKey, commandId,
    record: commandId ? { id: commandId, status: 'accepted' } : { status: 'submitting' },
    statusUnavailable: true, observationKind: 'protocol', retrying: false, checking: false,
    protocolInvalid: true, canResubmit: false,
  }
}

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

const transportStorage = (storage) => {
  if (storage !== undefined) return storage
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage }
  catch { return null }
}
const transportStorageKey = runId => TRANSPORT_STORAGE_PREFIX + encodeURIComponent(String(runId || ''))
const assistantTransportStorageKey = runId => ASSISTANT_TRANSPORT_STORAGE_PREFIX + encodeURIComponent(String(runId || ''))
const runCommandLockKey = runId => RUN_COMMAND_LOCK_PREFIX + encodeURIComponent(String(runId || ''))
const launchTransportKey = identity => LAUNCH_TRANSPORT_PREFIX + encodeURIComponent(String(identity || ''))
const safeLaunchText = (value, max) => typeof value === 'string' && value.length > 0
  && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value)

const parsedLaunchTransport = (raw, identity) => {
  let payload
  try { payload = JSON.parse(raw) } catch { return { invalid: true } }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || !hasOnlyKeys(payload, LAUNCH_TRANSPORT_KEYS)
      || payload.identity !== String(identity) || !safeLaunchText(payload.runId, 255)
      || !safeLaunchText(payload.idempotencyKey, 200) || !Number.isFinite(payload.updatedAt)) {
    return { invalid: true }
  }
  return payload
}

const notifyLaunchTransports = () => {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function'
      || typeof CustomEvent === 'undefined') return
  try { window.dispatchEvent(new CustomEvent(LAUNCH_TRANSPORT_EVENT)) } catch { /* reload can recover */ }
}

// New-run transport stores identity only: never task/settings/chat/token/provider data. A paid Start
// is blocked when this tab-scoped recovery key cannot be committed before the request leaves.
export function saveLaunchTransport(identity, state, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !safeLaunchText(identity, 300) || !safeLaunchText(state?.runId, 255)
      || !safeLaunchText(state?.idempotencyKey, 200)) return false
  const payload = {
    identity: String(identity), runId: String(state.runId),
    idempotencyKey: String(state.idempotencyKey), updatedAt: Date.now(),
  }
  try {
    target.setItem(launchTransportKey(identity), JSON.stringify(payload))
    const saved = target.getItem(launchTransportKey(identity)) === JSON.stringify(payload)
    if (saved && storage === undefined) notifyLaunchTransports()
    return saved
  } catch { return false }
}

export function loadLaunchTransport(identity, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !safeLaunchText(identity, 300)) return null
  try {
    const raw = target.getItem(launchTransportKey(identity))
    if (raw == null) return null
    return parsedLaunchTransport(raw, identity)
  } catch { return { invalid: true } }
}

// Global recovery UI needs only safe correlation metadata.  Idempotency keys never leave the API
// module or enter DOM events/state; malformed entries remain visible as attention-required records.
export function listLaunchTransports(storage = undefined) {
  const target = transportStorage(storage)
  if (!target) return []
  const records = []
  try {
    for (let index = 0; index < target.length; index += 1) {
      const key = target.key(index)
      if (typeof key !== 'string' || !key.startsWith(LAUNCH_TRANSPORT_PREFIX)) continue
      const encoded = key.slice(LAUNCH_TRANSPORT_PREFIX.length)
      let identity
      try { identity = decodeURIComponent(encoded) } catch { identity = encoded }
      const parsed = parsedLaunchTransport(target.getItem(key), identity)
      records.push(parsed.invalid
        ? { identity, runId: '', updatedAt: 0, invalid: true }
        : { identity: parsed.identity, runId: parsed.runId, updatedAt: parsed.updatedAt, invalid: false })
    }
  } catch { return [] }
  return records.sort((left, right) => right.updatedAt - left.updatedAt)
}

export function subscribeLaunchTransports(callback) {
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return () => {}
  const listener = () => callback(listLaunchTransports())
  window.addEventListener(LAUNCH_TRANSPORT_EVENT, listener)
  return () => window.removeEventListener(LAUNCH_TRANSPORT_EVENT, listener)
}

export function clearLaunchTransport(identity, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !safeLaunchText(identity, 300)) return false
  try {
    const key = launchTransportKey(identity)
    target.removeItem(key)
    const cleared = target.getItem(key) == null
    if (cleared && storage === undefined) notifyLaunchTransports()
    return cleared
  } catch { return false }
}

const notifyRunCommandLock = (runId) => {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function'
      || typeof CustomEvent === 'undefined') return
  // The event is only an invalidation signal. Consumers re-read sessionStorage, so command ids,
  // idempotency keys, credentials, and payloads never enter the DOM event channel.
  try { window.dispatchEvent(new CustomEvent(RUN_COMMAND_LOCK_EVENT, { detail: { runId: String(runId) } })) }
  catch { /* storage still provides recovery when DOM events are unavailable */ }
}

const commandStatePending = state => !!state && (
  state.statusUnavailable || state.retrying || state.checking
  || !state.record || state.record.status === 'submitting'
  || COMMAND_PENDING.has(state.record.status)
)

const compatibleCommandLock = (current, next) => !current || (
  current.source === next.source
  && current.idempotencyKey === next.idempotencyKey
  && current.action === next.action
  && current.expectedGeneration === next.expectedGeneration
  && (!current.commandId || !next.commandId || current.commandId === next.commandId)
)

// A tiny shared per-run lock makes Dock and Assistant one control surface. It deliberately stores no
// command payload: the owning surface keeps the safe, deterministic recovery data in its own record.
export function saveRunCommandLock(runId, state, storage = undefined) {
  const target = transportStorage(storage)
  const source = state?.source
  const action = String(state?.action || '')
  const expectedGeneration = state?.expectedGeneration
  const idempotencyKey = String(state?.idempotencyKey || '')
  const commandId = String(state?.commandId || state?.record?.id || '')
  const status = String(state?.record?.status || 'submitting')
  if (!target || !runId || (source !== 'dock' && source !== 'assistant')
      || !safeIdentityText(action) || !safeIdentityText(idempotencyKey)
      || !validRunGeneration(expectedGeneration)
      || (commandId && !COMMAND_ID_RE.test(commandId)) || !STORED_COMMAND_STATUSES.has(status)) return false
  const payload = {
    runId: String(runId), source, action, expectedGeneration, idempotencyKey, commandId,
    status, statusUnavailable: !!state.statusUnavailable,
    updatedAt: Date.now(),
  }
  try {
    const current = loadRunCommandLock(runId, target)
    if (!compatibleCommandLock(current, payload)) return false
    if (current?.commandId && !payload.commandId) payload.commandId = current.commandId
    target.setItem(runCommandLockKey(runId), JSON.stringify(payload))
    if (storage === undefined) notifyRunCommandLock(runId)
    return true
  } catch { return false }
}

export function loadRunCommandLock(runId, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !runId) return null
  try {
    const payload = JSON.parse(target.getItem(runCommandLockKey(runId)) || 'null')
    if (!payload || typeof payload !== 'object' || Array.isArray(payload) || !hasOnlyKeys(payload, LOCK_KEYS)
        || payload.runId !== String(runId)
        || (payload.source !== 'dock' && payload.source !== 'assistant')
        || !validRunGeneration(payload.expectedGeneration)
        || !safeIdentityText(payload.action) || !safeIdentityText(payload.idempotencyKey)
        || (payload.commandId && !COMMAND_ID_RE.test(payload.commandId))
        || !STORED_COMMAND_STATUSES.has(payload.status)
        || typeof payload.statusUnavailable !== 'boolean'
        || !Number.isFinite(payload.updatedAt)) return null
    return payload
  } catch { return null }
}

export function clearRunCommandLock(runId, expected = {}, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !runId) return false
  try {
    const current = loadRunCommandLock(runId, target)
    if (current && ((expected.source && current.source !== expected.source)
        || (expected.idempotencyKey && current.idempotencyKey !== expected.idempotencyKey)
        || (expected.action && current.action !== expected.action)
        || (expected.expectedGeneration
          && current.expectedGeneration !== expected.expectedGeneration)
        // Once the lock learned a durable id, an id-less/stale cleanup must not remove it. Requiring
        // exact identity here prevents an older render from unlocking a newer accepted command.
        || (current.commandId && current.commandId !== String(expected.commandId || '')))) return false
    target.removeItem(runCommandLockKey(runId))
    if (storage === undefined) notifyRunCommandLock(runId)
    return true
  } catch { return false }
}

export function subscribeRunCommandLock(runId, callback) {
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return () => {}
  const listener = event => {
    if (String(event.detail?.runId) === String(runId)) callback(loadRunCommandLock(runId))
  }
  window.addEventListener(RUN_COMMAND_LOCK_EVENT, listener)
  return () => window.removeEventListener(RUN_COMMAND_LOCK_EVENT, listener)
}

// A command key is written before POST. Consequently a reload can re-submit the SAME intent even if
// every response was lost before the browser learned the server's command id. Records contain no
// owner/review credential and stay tab-scoped in sessionStorage.
export function saveRunTransport(runId, state, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !runId || !TRANSPORT_ACTIONS.has(state?.action) || !state?.idempotencyKey) return false
  const record = storedRecord(state.record, state.action, 'dock')
  if (!record) return false
  const explicitId = String(state.commandId || '')
  if ((explicitId && !COMMAND_ID_RE.test(explicitId))
      || (explicitId && record.id && explicitId !== record.id)) return false
  const commandId = explicitId || record.id || ''
  const expectedGeneration = state.expectedGeneration
  if (!validRunGeneration(expectedGeneration)) return false
  const payload = {
    runId: String(runId), action: state.action, expectedGeneration,
    idempotencyKey: String(state.idempotencyKey),
    commandId, record,
    statusUnavailable: !!state.statusUnavailable,
    observationKind: OBSERVATION_KINDS.has(state.observationKind || null) ? state.observationKind || null : 'protocol',
    retrying: !!state.retrying,
    checking: !!state.checking,
    updatedAt: Date.now(),
    committed: true,
  }
  const pending = commandStatePending(payload)
  const lockState = { ...payload, source: 'dock' }
  const currentLock = loadRunCommandLock(runId, target)
  const prospectiveLock = {
    source: 'dock', action: payload.action, idempotencyKey: payload.idempotencyKey,
    expectedGeneration, commandId, status: record.status,
  }
  if (pending && !compatibleCommandLock(currentLock, prospectiveLock)) return false
  if (pending && currentLock?.commandId && !commandId) return false
  try {
    const key = transportStorageKey(runId)
    const previous = target.getItem(key)
    if (pending) {
      // Two-phase storage commit. If the lock write or rollback fails, `committed:false` survives as
      // an explicit quarantine marker and reload will never auto-submit the staged envelope.
      target.setItem(key, JSON.stringify({ ...payload, committed: false }))
      if (!saveRunCommandLock(runId, lockState, storage)) {
        try { if (previous == null) target.removeItem(key); else target.setItem(key, previous) } catch { /* quarantine remains */ }
        return false
      }
    }
    target.setItem(key, JSON.stringify(payload))
    if (!pending) clearRunCommandLock(runId, {
      source: 'dock', idempotencyKey: payload.idempotencyKey, action: payload.action,
      expectedGeneration, commandId,
    }, storage)
    return true
  } catch { return false }
}

export function loadRunTransport(runId, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !runId) return null
  try {
    const raw = target.getItem(transportStorageKey(runId))
    if (raw == null) return null
    let payload
    try { payload = JSON.parse(raw) } catch { return protocolTransport(runId, 'dock') }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)
        || !hasOnlyKeys(payload, RUN_ENVELOPE_KEYS)
        || payload.runId !== String(runId) || !TRANSPORT_ACTIONS.has(payload.action)
        || !validRunGeneration(payload.expectedGeneration)
        || !safeIdentityText(payload.idempotencyKey)
        || typeof payload.commandId !== 'string'
        || (payload.commandId && !COMMAND_ID_RE.test(payload.commandId))
        || typeof payload.statusUnavailable !== 'boolean'
        || !OBSERVATION_KINDS.has(payload.observationKind)
        || typeof payload.retrying !== 'boolean' || typeof payload.checking !== 'boolean'
        || payload.committed !== true
        || !Number.isFinite(payload.updatedAt)) return protocolTransport(runId, 'dock', payload)
    const record = storedRecord(payload.record, payload.action, 'dock', { strict: true })
    if (!record || (payload.commandId && record.id && payload.commandId !== record.id)
        || (!!payload.commandId !== !!record.id)) return protocolTransport(runId, 'dock', payload)
    const { committed: _committed, ...restored } = payload
    return { ...restored, commandId: payload.commandId, record, lastError: '' }
  } catch { return protocolTransport(runId, 'dock') }
}

export function clearRunTransport(runId, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !runId) return false
  try {
    const saved = loadRunTransport(runId, target)
    target.removeItem(transportStorageKey(runId))
    clearRunCommandLock(runId, {
      source: 'dock', idempotencyKey: saved?.idempotencyKey,
      action: saved?.action, expectedGeneration: saved?.expectedGeneration,
      commandId: saved?.commandId,
    }, storage)
    return true
  } catch { return false }
}

// Assistant slash commands use the same durable envelope as Dock but a separate key. Only the
// allow-listed action, optional numeric node id, and that node's exact lifecycle generation are
// persisted; arbitrary command data is not.
export function saveAssistantRunTransport(runId, state, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !runId || !ASSISTANT_TRANSPORT_ACTIONS.has(state?.action) || !state?.idempotencyKey) return false
  const record = storedRecord(state.record, state.action, 'assistant')
  if (!record) return false
  const explicitId = String(state.commandId || '')
  if ((explicitId && !COMMAND_ID_RE.test(explicitId))
      || (explicitId && record.id && explicitId !== record.id)) return false
  const commandId = explicitId || record.id || ''
  const expectedGeneration = state.expectedGeneration
  if (!validRunGeneration(expectedGeneration)) return false
  const numericArg = state.arg == null ? null : Number(state.arg)
  const nodeGeneration = state.nodeGeneration == null ? null : Number(state.nodeGeneration)
  if (state.action === 'approve') {
    if (numericArg != null && (!Number.isSafeInteger(numericArg) || numericArg < 0)) return false
    if (nodeGeneration != null
        && (!Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0)) return false
    // Before the server returns a durable command id, recovery must retain the exact node lifecycle
    // inspected by the user. Re-fetching a later attempt would turn recovery into a new action.
    if (!commandId && (numericArg == null || nodeGeneration == null)) return false
  } else if (state.nodeGeneration != null) return false
  const payload = {
    runId: String(runId), action: state.action, arg: state.action === 'approve' ? numericArg : null,
    nodeGeneration: state.action === 'approve' ? nodeGeneration : null,
    expectedGeneration,
    idempotencyKey: String(state.idempotencyKey), commandId, record,
    statusUnavailable: !!state.statusUnavailable,
    observationKind: OBSERVATION_KINDS.has(state.observationKind || null) ? state.observationKind || null : 'protocol',
    retrying: !!state.retrying, checking: !!state.checking,
    updatedAt: Date.now(),
    committed: true,
  }
  const pending = commandStatePending(payload)
  const lockState = { ...payload, source: 'assistant' }
  const currentLock = loadRunCommandLock(runId, target)
  const prospectiveLock = {
    source: 'assistant', action: payload.action, idempotencyKey: payload.idempotencyKey,
    expectedGeneration, commandId, status: record.status,
  }
  if (pending && !compatibleCommandLock(currentLock, prospectiveLock)) return false
  if (pending && currentLock?.commandId && !commandId) return false
  try {
    const key = assistantTransportStorageKey(runId)
    const previous = target.getItem(key)
    if (pending) {
      target.setItem(key, JSON.stringify({ ...payload, committed: false }))
      if (!saveRunCommandLock(runId, lockState, storage)) {
        try { if (previous == null) target.removeItem(key); else target.setItem(key, previous) } catch { /* quarantine remains */ }
        return false
      }
    }
    target.setItem(key, JSON.stringify(payload))
    if (!pending) clearRunCommandLock(runId, {
      source: 'assistant', idempotencyKey: payload.idempotencyKey, action: payload.action,
      expectedGeneration, commandId,
    }, storage)
    return true
  } catch { return false }
}

export function loadAssistantRunTransport(runId, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || !runId) return null
  try {
    const raw = target.getItem(assistantTransportStorageKey(runId))
    if (raw == null) return null
    let payload
    try { payload = JSON.parse(raw) } catch { return protocolTransport(runId, 'assistant') }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)
        || !hasOnlyKeys(payload, ASSISTANT_ENVELOPE_KEYS)
        || payload.runId !== String(runId) || !ASSISTANT_TRANSPORT_ACTIONS.has(payload.action)
        || !validRunGeneration(payload.expectedGeneration)
        || !safeIdentityText(payload.idempotencyKey)
        || typeof payload.commandId !== 'string'
        || (payload.commandId && !COMMAND_ID_RE.test(payload.commandId))
        || typeof payload.statusUnavailable !== 'boolean'
        || !OBSERVATION_KINDS.has(payload.observationKind)
        || typeof payload.retrying !== 'boolean' || typeof payload.checking !== 'boolean'
        || payload.committed !== true
        || !Number.isFinite(payload.updatedAt)) return protocolTransport(runId, 'assistant', payload)
    const record = storedRecord(payload.record, payload.action, 'assistant', { strict: true })
    if (!record || (payload.commandId && record.id && payload.commandId !== record.id)
        || (!!payload.commandId !== !!record.id)) return protocolTransport(runId, 'assistant', payload)
    const arg = payload.action === 'approve' && payload.arg != null ? Number(payload.arg) : null
    const nodeGeneration = payload.action === 'approve' && payload.nodeGeneration != null
      ? Number(payload.nodeGeneration) : null
    if (payload.action === 'approve' && ((arg != null && (!Number.isSafeInteger(arg) || arg < 0))
        || (nodeGeneration != null
          && (!Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0))
        || (!payload.commandId && (arg == null || nodeGeneration == null)))) {
      return protocolTransport(runId, 'assistant', payload)
    }
    if (payload.action !== 'approve'
        && (payload.arg !== null || (payload.nodeGeneration != null))) {
      return protocolTransport(runId, 'assistant', payload)
    }
    const { committed: _committed, ...restored } = payload
    return { ...restored, arg, nodeGeneration, commandId: payload.commandId,
      record, lastError: '' }
  } catch { return protocolTransport(runId, 'assistant') }
}

export function clearAssistantRunTransport(runId, storage = undefined, expected = {}) {
  const target = transportStorage(storage)
  if (!target || !runId) return false
  try {
    const saved = loadAssistantRunTransport(runId, target)
    if (saved && expected.idempotencyKey && saved.idempotencyKey !== expected.idempotencyKey) return false
    target.removeItem(assistantTransportStorageKey(runId))
    clearRunCommandLock(runId, {
      source: 'assistant', idempotencyKey: saved?.idempotencyKey,
      action: saved?.action, expectedGeneration: saved?.expectedGeneration,
      commandId: saved?.commandId,
    }, storage)
    return true
  } catch { return false }
}

export function commandErrorMessage(record) {
  const error = record?.error
  if (error && typeof error === 'object') {
    const canonical = STORED_ERROR_COPY[error.code] || STORED_ERROR_COPY.command_failed
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

function runGenerationError(code, message, remediation) {
  const error = new Error(message)
  error.code = code
  error.remediation = remediation
  return error
}

export async function getRunGeneration(runId) {
  const payload = await get(`/api/runs/${encodeURIComponent(runId)}/state`)
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

const notifyCommandRecord = (callback, record) => {
  if (!callback) return
  try { callback(record) } catch { /* persistence/presentation must not break command execution */ }
}

export async function submitRunCommand(runId, type, data = {}, {
  idempotencyKey = createIdempotencyKey(), expectedGeneration,
  requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  const path = `/api/runs/${encodeURIComponent(runId)}/commands`
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError(
      'invalid_run_generation',
      'A verified run generation is required before submitting a command.',
      'Refresh the run before submitting another action.',
    )
  }
  try {
    return await commandFetch(path, {
      method: 'POST',
      headers: _authHeaders({ 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }),
      body: JSON.stringify({ type, data: data || {}, expected_generation: expectedGeneration }),
    }, requestTimeoutMs, async response => {
      if (!response.ok) await _throw(response, path)
      return validatedCommandRecord(await commandResponseJson(response, path, { submission: true }), path)
    })
  }
  catch (error) {
    if (error?.code === 'COMMAND_PROTOCOL_ERROR' || error?.code === 'COMMAND_REQUEST_TIMEOUT') {
      error.submissionMayHaveSucceeded = true
    }
    throw error
  }
}

export async function getRunCommand(runId, commandId, { requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS } = {}) {
  const path = `/api/runs/${encodeURIComponent(runId)}/commands/${encodeURIComponent(commandId)}`
  return commandFetch(path, { headers: _authHeaders({}), cache: 'no-store' }, requestTimeoutMs,
    async response => {
      if (!response.ok) await _throw(response, path)
      return validatedCommandRecord(await commandResponseJson(response, path), path, commandId)
    })
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
  const path = `/api/runs/${encodeURIComponent(runId)}/commands/${encodedId}/retry`
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  let record
  try {
    record = await commandFetch(path, { method: 'POST', headers: _authHeaders({}) }, requestTimeoutMs,
      async response => {
        if (!response.ok) await _throw(response, path)
        return commandResponseJson(response, path, { submission: true })
      })
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
  expectedGeneration = undefined,
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
        idempotencyKey, expectedGeneration: generation, requestTimeoutMs,
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

const reportStorageError = cause => Object.assign(
  new Error('Report refresh storage is unavailable.'),
  { code: 'REPORT_REFRESH_STORAGE_UNAVAILABLE', cause },
)

// Keep one logical refresh identity across component unmounts and ambiguous responses. A retry POST
// can then rejoin the server's first job. Supplying `completedKey` clears only that exact intent;
// paid work fails closed when tab storage is unavailable.
export function reportRefreshIntent(runId, generation, completedKey = '', storage = undefined) {
  const backing = transportStorage(storage)
  if (!validRunGeneration(generation)) return null
  if (!backing) throw reportStorageError()
  const key = 'll.report-refresh.' + encodeURIComponent(String(runId || ''))
  const prefix = generation + ':'
  try {
    if (completedKey) {
      if (backing.getItem(key) === prefix + completedKey) {
        // Tombstone before best-effort removal. If removeItem fails, the next acquisition cannot
        // mistake a completed paid identity for an active one.
        backing.setItem(key, '')
        if (backing.getItem(key) !== '') throw new Error('storage write failed')
        try { backing.removeItem(key) } catch { /* the tombstone is already authoritative */ }
      }
      return true
    }
    const saved = backing.getItem(key)
    const candidate = saved?.startsWith(prefix) ? saved.slice(prefix.length) : null
    if (candidate !== null && !UUID_V4_RE.test(candidate)) throw new Error('invalid stored report identity')
    const idempotencyKey = candidate ?? createIdempotencyKey()
    backing.setItem(key, prefix + idempotencyKey)
    if (backing.getItem(key) !== prefix + idempotencyKey) throw new Error('storage write failed')
    return { generation, idempotencyKey }
  } catch (cause) { throw reportStorageError(cause) }
}

export const CONTROL = {
  // Three operator controls (see docs/guide/concepts.md → "Stopping a run"):
  //   stop     — freeze the run, NO finalization (event: pause). Resumable; finalize later if wanted.
  //   finalize — stop AND wrap up (report / cross-run lessons+case / cost roll-up). event: run_abort.
  //   resume   — continue from ANY stopped state (pause / finalize / natural finish). event: resume.
  stop: (rid) => runCommand(rid, 'pause', {}),
  finalize: (rid) => runCommand(rid, 'run_abort', { reason: 'finalized' }),
  resume: (rid) => runCommand(rid, 'resume', {}),
  // One durable server-owned pause -> replacement-owner handoff. The browser does not orchestrate
  // two commands, so navigation or reload cannot strand a run between them.
  restart: (rid) => runCommand(rid, 'restart', {}),
  // back-compat aliases (older callers / NL control): pause≡stop, abort≡finalize, reopen≡resume.
  pause: (rid) => runCommand(rid, 'pause', {}),
  abort: (rid) => runCommand(rid, 'run_abort', { reason: 'finalized' }),
  nodeAbort: (rid, id, generation) => runCommand(
    rid, 'node_abort', { node_id: id, generation, reason: 'ui' }),
  // Re-run an existing node IN PLACE from a stage (no new node): eval=re-score (keep code),
  // implement=re-run the Developer (keep the idea), propose=full redo. The command service drives it.
  resetNode: (rid, id, stage, generation) => runCommand(
    rid, 'node_reset', { node_id: id, generation, from_stage: stage }),
  approve: (rid, id, generation) => runCommand(
    rid, 'approval_granted', { node_id: id, generation }),
  ratify: (rid) => runCommand(rid, 'spec_approved', {}),
  hint: (rid, text) => runCommand(rid, 'hint', { text }),
  budget: (rid, sec) => runCommand(rid, 'budget_extend', { max_eval_seconds: sec }),
  forceConfirm: (rid, id, generation) => runCommand(
    rid, 'force_confirm', { node_id: id, generation }),
  forceAblate: (rid, id, generation) => runCommand(
    rid, 'force_ablate', { node_id: id, generation }),
  fork: (rid, id, generation) => runCommand(
    rid, 'fork', { from_node_id: id, generation }),
  annotate: (rid, id, text) => runCommand(rid, 'annotation', { node_id: id, text }),
  // Structured comments are append-only run commands. The caller supplies the exact displayed run
  // generation separately from the node's attempt generation; a late click can therefore update
  // neither a replacement run nor a reset incarnation of the same numeric node id.
  createComment: (rid, { nodeId, nodeGeneration, text }, options = {}) => runCommand(
    rid, 'comment_created', { node_id: nodeId, node_generation: nodeGeneration, text }, options),
  editComment: (rid, { commentId, nodeId, nodeGeneration, expectedVersion, text }, options = {}) => runCommand(
    rid, 'comment_edited', {
      comment_id: commentId, node_id: nodeId, node_generation: nodeGeneration,
      expected_version: expectedVersion, text,
    }, options),
  setCommentResolved: (rid, {
    commentId, nodeId, nodeGeneration, expectedVersion, resolved,
  }, options = {}) => runCommand(rid, 'comment_resolution_changed', {
    comment_id: commentId, node_id: nodeId, node_generation: nodeGeneration,
    expected_version: expectedVersion, resolved,
  }, options),
  promote: (rid, id, generation) => runCommand(
    rid, 'promote', { node_id: id, generation, alias: 'champion' }),
  // Operator-authored experiment: hand-add a node to the search tree. `idea` = {operator, params,
  // rationale, theme?}; optional parent_id (branch from a node) and code (ship ready-made code).
  inject: (rid, { idea, parent_id = null, parent_generation = null, code = null }) =>
    runCommand(rid, 'inject_node', {
      idea, parent_id, code,
      parent_generations: parent_id != null && parent_generation != null
        ? { [parent_id]: parent_generation } : undefined,
    }),
  reopen: (rid) => runCommand(rid, 'run_reopened', {}),
  // U3: merge two nodes — inject a multi-parent `merge` node; the engine recombines the parents'
  // solutions via its real merge/ensemble operator (not a blank manual node).
  merge: (rid, ids, parentGenerations = undefined, options = {}) => runCommand(rid, 'inject_node', {
      idea: { operator: 'merge', rationale: `merge ${ids.map(i => '#' + i).join(' + ')}` },
      parent_ids: ids, parent_generations: parentGenerations,
    }, options),
  // A7: pin/override the Strategist's choice live (HITL parity). `strategy` = a Strategy dict
  // {policy?, policy_params?, developer?, operators?, fidelity?, rationale?}.
  setStrategy: (rid, strategy) => runCommand(rid, 'set_strategy', { strategy }),
  // P2: ask the engine to run the Deep-Research stage now (read all results + the web, write a memo).
  deepResearch: (rid) => runCommand(rid, 'deep_research', {}),
  // P1: register an open hypothesis on the board (a question the search should resolve), or drop one.
  addHypothesis: (rid, statement) => runCommand(rid, 'hypothesis_added', { statement, source: 'human' }),
  abandonHypothesis: (rid, id) => runCommand(rid, 'hypothesis_updated', { id, status: 'abandoned' }),
  deleteHypothesis: (rid, id) => runCommand(rid, 'hypothesis_updated', { id, status: 'deleted' }),
  // Workstream A: force a high-quality regeneration of the agent-authored run report now. Dedicated
  // endpoint (not /control) — appends a `report_generated` event. Runs as a background job, so we
  // jobAwait the response (a slow/large regen can't 504 behind a proxy; a fast one returns inline).
  // Contract preserved: resolves to {ok, seq, generation, content} (or {ok:false} offline), never a
  // job_id. The same key rejoins ambiguous retries to one paid server job.
  refreshReport: async (rid, { expectedGeneration, idempotencyKey, signal,
    requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS } = {}) => {
    if (!validRunGeneration(expectedGeneration)) {
      throw runGenerationError(
        'invalid_run_generation',
        'A verified run generation is required before refreshing the report.',
        'Reload the run before generating its report.',
      )
    }
    if (!safeIdentityText(idempotencyKey)) {
      throw new Error('A valid report refresh idempotency key is required.')
    }
    const path = runApiPath(rid, '/report_refresh')
    assertNotReviewMutation(path)
    assertRunMutationAllowed(path)
    const response = await commandFetch(path, {
      method: 'POST',
      headers: _authHeaders({
        'Content-Type': 'application/json', 'Idempotency-Key': String(idempotencyKey),
      }),
      body: JSON.stringify({ expected_generation: expectedGeneration }),
      signal,
    }, requestTimeoutMs, async reply => {
      if (!reply.ok) await _throw(reply, path)
      return commandResponseJson(reply, path, { submission: true })
    })
    const result = await jobAwait(response, { maxTransientErrors: 3, signal })
    if (result?.ambiguous !== true
        && (!validRunGeneration(result?.generation) || result.generation !== expectedGeneration)) {
      const error = new Error('Invalid report generation receipt.')
      error.code = 'REPORT_REFRESH_PROTOCOL_ERROR'
      error.ambiguous = true
      error.submissionMayHaveSucceeded = true
      throw error
    }
    return result
  },
  // Generic authoritative command by {type, data}; slash commands and action routers share this path.
  raw: (rid, type, data = {}) => runCommand(rid, type, data),
}

// Apply one assistant/boss action through the same authoritative lifecycle. Report regeneration keeps
// its dedicated background-job endpoint; all event commands delegate engine policy to the server.
export async function appendAction(runId, action, options = {}) {
  if (action.type === '__refresh_report__') return CONTROL.refreshReport(runId, options)
  return CONTROL.raw(runId, action.type, action.data || {})
}

// round-7 "Replay": reset a run IN PLACE — the server archives its event log + spans and re-spawns a
// fresh run on the same run-id. Only offered on a FINISHED run (no live engine), so it's race-free.
export const resetRun = (rid) => post(`/api/runs/${encodeURIComponent(rid)}/reset`, {})

// Clear ONE node's trace: erase its spans from spans.jsonl so a reset+rebuild's fresh bands don't
// stack on top of the old attempt's. Server refuses (409) while the engine is live (sole writer).
export const clearNodeTrace = (rid, id) =>
  post(runNodeApiPath(rid, id, '/clear_trace'), {})

export const llmHealth = () => get('/api/llm/health')

// Owner and reviewer are distinct principals. A review fragment wins even if this tab has stale
// owner state, so the read-only surface can never accidentally send both credentials.
const _authHeaders = (base) => {
  // The review pathname is an authority boundary even when its fragment is missing or malformed.
  // Never fall back to a session-scoped owner credential from a tab that navigated to /review.
  if (isReviewLocation()) {
    const review = reviewTokenFromLocation()
    return review ? { ...base, 'X-LoopLab-Review': review } : { ...base }
  }
  const owner = ownerToken()
  return owner ? { ...base, 'X-LoopLab-Token': owner } : { ...base }
}
// Surface the server's error DETAIL (FastAPI puts the human-readable reason in `detail`) instead of a
// bare status code — so e.g. a 422 from a per-run config save reads "invalid settings — n_seeds: …"
// in the toast rather than just "422". Falls back to status when there's no JSON body.
async function _throw(r, path) {
  let detail = '', payload = null
  try { payload = await r.json(); detail = (payload && (payload.detail ?? payload.error)) ?? '' } catch { /* no body */ }
  const structured = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : null
  // FastAPI validation errors (422) put `detail` as an ARRAY of {loc, msg, type}. String(array) would
  // render "[object Object],[object Object]" in the toast — flatten each entry to "field: msg" instead.
  const arrayDetail = Array.isArray(detail)
    ? detail.map(d => {
        if (!d || typeof d !== 'object') return String(d)
        const field = Array.isArray(d.loc) ? d.loc.filter(x => x !== 'body' && x !== 'query').join('.') : ''
        return (field ? `${field}: ` : '') + String(d.msg || d.type || JSON.stringify(d))
      }).filter(Boolean).join('; ')
    : null
  const message = structured
    ? String(structured.message || structured.detail || structured.error || structured.code || `${path}: ${r.status}`)
    : arrayDetail ? arrayDetail
    : detail ? String(detail) : `${path}: ${r.status}`
  const err = new Error(message)
  err.status = r.status   // callers branch on the code (e.g. 409 = run live / name taken), not a regex on the message
  err.detail = structured || detail || null
  if (structured?.code) err.code = String(structured.code)
  if (structured?.remediation) err.remediation = String(structured.remediation)
  const detailText = `${message} ${typeof detail === 'string' ? detail : ''}`
  const existingCommandId = structured?.existing_command_id || structured?.existingCommandId
    || detailText.match(/\bcmd_[0-9a-f]{32}\b/i)?.[0]
  const commandId = structured?.command_id || structured?.commandId
  // A conflicting command belongs to another action. Keeping it separate prevents callers from
  // fabricating a failed record for the requested action with the active command's durable id.
  if (existingCommandId) err.existingCommandId = String(existingCommandId)
  if (commandId) err.commandId = String(commandId)
  const retryAfter = r.headers?.get?.('Retry-After')
  if (retryAfter) {
    const seconds = Number(retryAfter)
    const millis = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(retryAfter) - Date.now()
    if (Number.isFinite(millis) && millis > 0) err.retryAfterMs = Math.min(60_000, millis)
  }
  throw err
}

// Path-mounting-proxy support. The UI may be served under a prefix (JupyterHub
// `/user/<name>/proxy/8765/`, a reverse-proxy subpath, …) rather than at the domain root, so an
// absolute `/api/…` would hit the proxy host's root and miss the backend. We route every request
// through apiUrl(), which prepends the prefix the page itself was served from. Routing is hash-based
// (`#/run/…`), so location.pathname is exactly that prefix; the proxy strips it before forwarding,
// so the backend still sees `/api/…`. At the root (local `looplab ui`) the prefix is '' — unchanged.
export function apiPrefix() {
  if (typeof location === 'undefined') return ''
  return location.pathname.replace(/\/index\.html$/, '').replace(/\/review\/?$/, '').replace(/\/+$/, '')
}
export const apiUrl = (path) => apiPrefix() + path

// Review reads use a namespace whose run identity comes from the bearer. Existing read-only
// components can keep asking for `/api/runs/<id>/...`; only GET paths are translated.
export function reviewReadPath(path) {
  if (!isReviewLocation()) return path
  const m = String(path || '').match(/^\/api\/runs\/[^/?#]+(\/[^?#]*)?(\?[^#]*)?$/)
  if (!m) return path
  return `/api/review${m[1] || ''}${m[2] || ''}`
}

const EVENT_STREAM_MAX_FRAME_CHARS = 2 * 1024 * 1024

// Incremental WHATWG event-stream parser. Fetch chunks can split CRLF, UTF-8 code points and any
// field at arbitrary boundaries, so parsing per network chunk (or only `\n\n`) is not sufficient.
// Keeping this pure also makes reconnect/id semantics testable without React or a browser.
export function createEventStreamParser(onEvent, initialLastEventId = '') {
  let buffer = ''
  let eventType = ''
  let dataLines = []
  let dataChars = 0
  let lastEventId = String(initialLastEventId || '')
  let retry = null

  const dispatch = () => {
    if (dataLines.length) {
      onEvent?.({
        type: eventType || 'message',
        data: dataLines.join('\n'),
        lastEventId,
        retry,
      })
    }
    eventType = ''
    dataLines = []
    dataChars = 0
  }
  const line = rawLine => {
    const valueLine = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!valueLine) { dispatch(); return }
    if (valueLine.startsWith(':')) return
    const separator = valueLine.indexOf(':')
    const field = separator < 0 ? valueLine : valueLine.slice(0, separator)
    let value = separator < 0 ? '' : valueLine.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') eventType = value
    else if (field === 'data') {
      dataChars += value.length
      if (dataChars > EVENT_STREAM_MAX_FRAME_CHARS) throw new Error('Event-stream frame is too large')
      dataLines.push(value)
    } else if (field === 'id' && !value.includes('\0')) {
      lastEventId = value
    } else if (field === 'retry' && /^\d+$/.test(value)) {
      retry = Math.min(Number(value), 60_000)
    }
  }

  return {
    push(text) {
      buffer += String(text || '')
      if (buffer.length > EVENT_STREAM_MAX_FRAME_CHARS) throw new Error('Event-stream buffer is too large')
      let newline
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const next = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        line(next)
      }
    },
    finish() {
      // EOF without a blank line is an incomplete event and is intentionally discarded, matching
      // EventSource. A reconnect can replay it from the last complete event id.
      buffer = ''
      eventType = ''
      dataLines = []
      dataChars = 0
      return { lastEventId, retry }
    },
    state: () => ({ lastEventId, retry }),
  }
}

// Authenticated GET-SSE transport for owner live state. Native EventSource cannot attach the owner
// or review credential, whereas this path uses the exact auth, review-translation and proxy-prefix
// plumbing as every ordinary API read. The caller owns reconnect timing and abort lifecycle.
export async function fetchEventStream(path, {
  signal, lastEventId = '', onEvent,
} = {}) {
  const requestPath = reviewReadPath(path)
  const headers = { Accept: 'text/event-stream', 'Cache-Control': 'no-cache' }
  if (lastEventId !== '') headers['Last-Event-ID'] = String(lastEventId).slice(0, 256)
  const response = await fetch(apiUrl(requestPath), {
    method: 'GET',
    headers: _authHeaders(headers),
    signal,
    cache: 'no-store',
  })
  if (!response.ok) await _throw(response, path)
  if (!response.body || typeof response.body.getReader !== 'function') {
    throw new Error('The server returned no readable event stream.')
  }
  const parser = createEventStreamParser(onEvent, lastEventId)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    parser.push(decoder.decode(value, { stream: true }))
  }
  parser.push(decoder.decode())
  return parser.finish()
}

function assertNotReviewMutation(path) {
  if (!isReviewLocation()) return
  const error = new Error('This review link is read-only')
  error.code = 'REVIEW_READ_ONLY'
  error.path = path
  throw error
}

export async function get(path, options = {}) {
  // Carry the UI token on reads too: most GETs don't need it, but the artifact routes (raw file
  // content) are token-gated server-side. _authHeaders is a no-op when no token is set (local), so
  // ordinary local use is unchanged.
  const requestPath = reviewReadPath(path)
  // Every review bearer addresses the same small URL namespace.  Force a cache bypass so a cached
  // 401/410 from a revoked capability can never poison a subsequently created link in this tab.
  const { headers = {}, ...fetchOptions } = options || {}
  const r = await fetch(apiUrl(requestPath), {
    ...fetchOptions,
    headers: _authHeaders(headers),
    ...(isReviewLocation() ? { cache: 'no-store' } : {}),
  })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
export async function post(path, body) {
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  const r = await fetch(apiUrl(path), { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
export async function putText(path, text) {
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  const r = await fetch(apiUrl(path), { method: 'PUT', headers: _authHeaders({ 'Content-Type': 'text/plain' }), body: text })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
async function send(path, method, body) {
  if (method !== 'GET') assertNotReviewMutation(path)
  if (method !== 'GET') assertRunMutationAllowed(path)
  // Only attach a JSON body for methods that carry one (PATCH/PUT/POST). A DELETE with a request
  // body + Content-Type is unusual and some reverse proxies (e.g. jupyter-server-proxy) mishandle it
  // — which surfaced as a 500 on "delete chat"/"delete run". DELETE goes bodyless.
  const hasBody = method !== 'DELETE' && method !== 'GET'
  const opts = { method, headers: _authHeaders(hasBody ? { 'Content-Type': 'application/json' } : {}) }
  if (hasBody) opts.body = JSON.stringify(body || {})
  const r = await fetch(apiUrl(path), opts)
  if (!r.ok) await _throw(r, path)
  return r.json()
}

export const authStatus = () => get('/api/auth/status')
export async function verifyOwnerToken(token) {
  const r = await fetch(apiUrl('/api/auth/verify'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LoopLab-Token': String(token || '') },
    body: '{}',
  })
  if (!r.ok) await _throw(r, '/api/auth/verify')
  setOwnerToken(token)
  return r.json()
}
export const clearOwnerToken = () => setOwnerToken('')
export const reviewManifest = () => get('/api/review')

// ---- ClearML-style project API ----
export const listProjects = () => get('/api/projects')
export const createProject = (name, parent_id = null) => post('/api/projects', { name, parent_id })
export const patchProject = (id, body) => send(`/api/projects/${encodeURIComponent(id)}`, 'PATCH', body)
export const deleteProject = (id) => send(`/api/projects/${encodeURIComponent(id)}`, 'DELETE')
export const assignRun = (runId, project_id) => post(`/api/runs/${encodeURIComponent(runId)}/project`, { project_id })
export const renameRun = (runId, label) => send(`/api/runs/${encodeURIComponent(runId)}`, 'PATCH', { label })
export const deleteRun = (runId) => send(`/api/runs/${encodeURIComponent(runId)}`, 'DELETE')
export const createRunReview = (runId, { ttl_seconds, include_evidence = false } = {}) =>
  post(`/api/runs/${encodeURIComponent(runId)}/reviews`, { ttl_seconds, include_evidence })
export const listRunReviews = (runId) => get(`/api/runs/${encodeURIComponent(runId)}/reviews`)
export const revokeRunReview = (runId, linkId) =>
  send(`/api/runs/${encodeURIComponent(runId)}/reviews/${encodeURIComponent(linkId)}`, 'DELETE')

// Bounded collaboration projections. In review mode reviewReadPath() translates both owner paths to
// `/api/review/comments...`; the capability still supplies the run identity and every mutation is
// rejected before fetch by assertNotReviewMutation().
export const runComments = (runId, {
  nodeId = null, nodeGeneration = null, includeResolved = true, limit = 100, cursor = null,
} = {}) => {
  const query = new URLSearchParams()
  if (nodeId != null) query.set('node_id', String(nodeId))
  if (nodeGeneration != null) query.set('node_generation', String(nodeGeneration))
  query.set('include_resolved', includeResolved ? 'true' : 'false')
  query.set('limit', String(Math.max(1, Math.min(100, Math.trunc(Number(limit) || 100)))))
  if (cursor != null) query.set('cursor', String(cursor))
  return get(`/api/runs/${encodeURIComponent(runId)}/comments?${query}`,
    { cache: 'no-store' })
}
export const commentHistory = (runId, commentId, { limit = 100, cursor = null } = {}) => {
  const query = new URLSearchParams()
  query.set('limit', String(Math.max(1, Math.min(100, Math.trunc(Number(limit) || 100)))))
  if (cursor != null) query.set('cursor', String(cursor))
  return get(`/api/runs/${encodeURIComponent(runId)}/comments/${encodeURIComponent(commentId)}/history?${query}`,
    { cache: 'no-store' })
}

// super-tasks: a user-managed, flat grouping of runs by the global task they attack (parallel axis
// to projects). create / rename / delete the bucket, then assign any run (existing or new) to it.
export const listSupertasks = () => get('/api/supertasks')
export const createSupertask = (name, task_id = null) => post('/api/supertasks', { name, task_id })
export const renameSupertask = (id, name) => send(`/api/supertasks/${encodeURIComponent(id)}`, 'PATCH', { name })
export const deleteSupertask = (id) => send(`/api/supertasks/${encodeURIComponent(id)}`, 'DELETE')
export const assignSupertask = (runId, supertask_id) => post(`/api/runs/${encodeURIComponent(runId)}/supertask`, { supertask_id })

export const gpuStat = () => get('/api/gpu')

// ---- settings + run launch ----
export const getSettings = () => get('/api/settings')
export const saveSettings = (settings) => send('/api/settings', 'PUT', { settings })
// Store (or clear, value='') a secret credential. Goes to the dedicated owner-only secret store,
// NOT ui_settings.json — the value is never echoed back (the GET reports it only as masked "***").
export const saveSecret = (key, value) => send('/api/settings/secret', 'PUT', { key, value })
// Per-run settings: edit a specific run's config.snapshot.json so the next RESUME picks up the
// change (only changed fields are sent). Blocked server-side while the run's engine is live.
export const saveRunConfig = (rid, settings) => send(`/api/runs/${encodeURIComponent(rid)}/config`, 'PUT', { settings })

// Experimental Research Atlas: owner-only, read-only projections over the shared memory portfolio.
// Bypass browser caches so Refresh observes newly finalized runs/governance without a stale intermediary.
const crossRunRead = (path, options = {}) => get(path, { ...options, cache: 'no-store' })
const boundedCrossRunInt = (value, fallback, maximum, minimum = 0) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, Math.trunc(parsed))) : fallback
}
const crossRunLimitArgs = (limitOrOptions, fallback, maximum, options) =>
  limitOrOptions && typeof limitOrOptions === 'object'
    ? { limit: fallback, options: limitOrOptions }
    : { limit: boundedCrossRunInt(limitOrOptions, fallback, maximum, 1), options }
// Bounds exist on both sides of the wire. Client render caps prevent DOM amplification; these query
// caps also prevent a routine Atlas preview navigation from requesting an unbounded shared ledger.
export const getCrossRunAtlas = (limitOrOptions = 24, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 24, 50, options)
  return crossRunRead(`/api/cross-run/atlas?limit=${args.limit}`, args.options)
}
export const getCrossRunClaims = (limitOrOptions = 80, offset = 0, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 80, 200, options)
  const offsetIsOptions = offset && typeof offset === 'object'
  if (offsetIsOptions && args.options == null) args.options = offset
  return crossRunRead(
    `/api/cross-run/claims?limit=${args.limit}&offset=${boundedCrossRunInt(offsetIsOptions ? 0 : offset, 0, 1_000_000)}`,
    args.options)
}
export const getCrossRunCurationLog = (limitOrOptions = 20, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 20, 50, options)
  return crossRunRead(`/api/cross-run/curation-log?limit=${args.limit}`, args.options)
}
export const getCrossRunClaimCurationLog = (limitOrOptions = 20, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 20, 50, options)
  return crossRunRead(`/api/cross-run/claim-curation-log?limit=${args.limit}`, args.options)
}
// New-run creation is propose -> edit -> validate -> start.  The preflight is non-billable and
// side-effect free; its opaque token binds the exact payload the server checked.  An inconclusive
// launch is observed by idempotency key instead of blindly POSTing a second engine start.
export const preflightRunStart = (body) => post('/api/start/preflight', body)
export const startRun = (body) => post('/api/start', body)
export const getStartStatus = (runId, idempotencyKey) => get(
  `/api/start/${encodeURIComponent(runId)}/status`, {
    cache: 'no-store', headers: { 'Idempotency-Key': String(idempotencyKey || '') },
  })

// cross-run aggregate reports over a scope (project | task | supertask). GET returns the stored report
// + staleness ({exists, content, generated_at, run_ids, stale, added, current_run_count}); generate
// (re)synthesizes on demand via an agent with access to every run in the scope.
const _scopeUrl = (type, id) => `/api/scope-report/${encodeURIComponent(type)}/${encodeURIComponent(id)}`
export const getScopeReport = (type, id) => get(_scopeUrl(type, id))
// Generic background-job poll: the server hands back {status:'running', job_id}
// for slow work so it can't 504 behind a proxy. Returns the final result dict; tolerates transient
// poll errors. `resp` that's already a result (fast inline path) is returned unchanged.
const _job = (jobId, { requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS, signal } = {}) => {
  const path = `/api/jobs/${encodeURIComponent(jobId)}`
  const requestPath = reviewReadPath(path)
  return commandFetch(requestPath, {
    headers: _authHeaders({}), cache: 'no-store', signal,
  }, requestTimeoutMs, async response => {
    if (!response.ok) await _throw(response, path)
    return commandResponseJson(response, path)
  })
}
export async function jobAwait(resp, {
  intervalMs = 1500, timeoutMs = 600000, signal,
  maxTransientErrors = Number.POSITIVE_INFINITY,
  requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  if (!resp || resp.status !== 'running' || !resp.job_id) return resp
  const deadline = Date.now() + timeoutMs
  let transientErrors = 0
  while (Date.now() < deadline) {
    if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError')
    let j
    try {
      const remainingMs = Math.max(1, deadline - Date.now())
      j = await _job(resp.job_id, {
        requestTimeoutMs: Math.min(requestTimeoutMs, remainingMs), signal,
      })
      transientErrors = 0
    } catch (error) {
      error.submissionMayHaveSucceeded = true
      if (!isTransientCommandReadError(error)) throw error
      if (++transientErrors >= maxTransientErrors) {
        return { ok: false, code: 'job_contact_lost', ambiguous: true,
          error: 'job contact lost' }
      }
      await commandSleep(intervalMs, signal)
      continue
    }
    if (!j || typeof j !== 'object' || Array.isArray(j)
        || !['running', 'done', 'unknown'].includes(j.status)) {
      return { ok: false, code: 'job_protocol_error', ambiguous: true,
        error: 'invalid job status' }
    }
    if (j.status === 'done') return j
    if (j.status === 'unknown') return { ok: false, code: 'job_unknown', ambiguous: true,
      error: 'job receipt expired' }
    await commandSleep(intervalMs, signal)
  }
  return { ok: false, code: 'job_timeout', ambiguous: true,
    error: 'job timed out' }
}
// Cross-run synthesis can read many runs + drive an agent, so it runs as a background job; await it to
// completion and surface a hard failure as a throw (the panel's catch shows it), preserving the old
// "returns the final record" contract for callers.
export async function genScopeReport(type, id) {
  const r = await jobAwait(await post(`${_scopeUrl(type, id)}/generate`, {}))
  if (r && r.ok === false) {
    const error = new Error(r.error || r.message || r.code || 'scope report generation failed')
    error.code = r.code
    throw error
  }
  return r
}

// ---- assistant (general chat agent — the evolution of Genesis) ----
export const assistantSessions = () => get('/api/assistant/sessions')
export const assistantCreate = (title = '', mode = 'plan') => post('/api/assistant/sessions', { title, mode })
export const assistantGet = (sid) => get(`/api/assistant/sessions/${encodeURIComponent(sid)}`)
export const assistantDelete = (sid) => send(`/api/assistant/sessions/${encodeURIComponent(sid)}`, 'DELETE')
export const assistantFork = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/fork`, {})
// Streaming turn: POST and read the SSE stream, invoking callbacks for token/step/todos/done/error.
// Real token streaming of the final answer (Claude-Desktop feel). Returns the final result dict.
export async function assistantMessageStream(sid, instruction, mode, cbs = {}, signal, display = null) {
  const r = await fetch(apiUrl(`/api/assistant/sessions/${encodeURIComponent(sid)}/message_stream`),
    { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
      // Recovery must send the persisted clean display even when it happens to equal `instruction`:
      // the explicit three-field body is the exact durable-turn contract, not a newly composed send.
      body: JSON.stringify(display != null ? { instruction, display, mode } : { instruction, mode }), signal })
  if (!r.ok) { await _throw(r, 'message_stream'); return null }
  // A 2xx with no readable body (empty stream, or an environment/proxy that doesn't expose one) is NOT
  // a server error — returning null lets the caller fall back to the non-streaming path instead of
  // surfacing a misleading "message_stream: 200" toast from _throw's status fallback.
  if (!r.body) return null
  const reader = r.body.getReader(); const dec = new TextDecoder()
  let buf = ''; let result = null
  for (;;) {
    let chunk
    try { chunk = await reader.read() } catch { break }   // aborted (unmount) — stop cleanly
    const { done, value } = chunk
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2)
      let ev = 'message'; let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      let parsed; try { parsed = JSON.parse(data) } catch { parsed = data }
      if (ev === 'token') cbs.onToken && cbs.onToken(parsed)
      else if (ev === 'text') cbs.onText && cbs.onText(parsed)
      else if (ev === 'step') cbs.onStep && cbs.onStep(parsed)
      else if (ev === 'todos') cbs.onTodos && cbs.onTodos(parsed)
      else if (ev === 'error') { cbs.onError && cbs.onError(parsed); result = { ok: false, error: parsed } }
      else if (ev === 'done') { result = parsed; cbs.onDone && cbs.onDone(parsed) }
    }
  }
  return result
}
// Full (uncapped) I/O for one trace observation — fetched lazily when the user expands a
// generation/tool in the trace tree (the tree itself is served light, without prompts/outputs).
export const spanDetail = (runId, spanId) =>
  get(`/api/runs/${encodeURIComponent(runId)}/spans/${encodeURIComponent(spanId)}`)

// Linear, de-duplicated conversation view of a node's trace (request once per sub-loop, then each
// generation's delta interleaved with tool calls) — the readable alternative to the raw span tree.
export const nodeConversation = (runId, nid) =>
  get(runNodeApiPath(runId, nid, '/conversation'))

// Stop an in-flight assistant turn server-side (survives a page reload, unlike aborting the local
// stream). Also used to poll whether a turn is still running (reattach after switch/reload).
export const assistantCancel = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/cancel`, {})
export const assistantProgress = (sid) => get(`/api/assistant/progress?session=${encodeURIComponent(sid)}`)

export const assistantCommands = () => get('/api/assistant/commands')
export const assistantRevert = (path) => post('/api/assistant/revert', { path })
export const assistantShare = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/share`, {})
// Pending human-in-the-loop confirm requests for a session, and resolving one.
export const assistantPermissions = (sid = null) => get(sid == null
  ? '/api/assistant/permissions'
  : `/api/assistant/permissions?session=${encodeURIComponent(sid)}`)
export const assistantResolve = (reqId, decision) =>
  post(`/api/assistant/permissions/${encodeURIComponent(reqId)}`, { decision })
export const attentionFeed = (limit = 200, cursor = null) => get(
  `/api/attention?limit=${encodeURIComponent(limit)}`
  + (cursor == null ? '' : `&cursor=${encodeURIComponent(cursor)}`),
)
