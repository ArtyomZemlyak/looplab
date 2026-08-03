// The UI's server API: the fetch client (get/post/send wrappers + auth/prefix plumbing), the generic
// background-job await, every /api/* endpoint function, and the CONTROL action map. Split out of
// util.js (mega-refactor P5.2 — bodies verbatim); util.js re-exports everything, so importers are
// unchanged.

import { assertRunMutationAllowed } from './runMode.js'
import { splitRouteHash } from './runRouteState.js'
import { deadlineRequest } from './requestDeadline.js'

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
const LAUNCH_PREFLIGHT_TIMEOUT_MS = 12_000
const LAUNCH_SUBMISSION_TIMEOUT_MS = 12_000
const LAUNCH_STATUS_TIMEOUT_MS = 5_000
const MAX_LAUNCH_REQUEST_TIMEOUT_MS = 60_000
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

const launchTransportMatches = (record, identity, state) => !!record && !record.invalid
  && record.identity === String(identity)
  && record.runId === String(state?.runId || '')
  && record.idempotencyKey === String(state?.idempotencyKey || '')

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
    const key = launchTransportKey(identity)
    const currentRaw = target.getItem(key)
    if (currentRaw != null) {
      // Once a paid Start owns this proposal, a second click/render must never replace the exact
      // observation key. Re-saving the same identity is idempotent; every changed identity is blocked.
      return launchTransportMatches(parsedLaunchTransport(currentRaw, identity), identity, payload)
    }
    const encoded = JSON.stringify(payload)
    target.setItem(key, encoded)
    const saved = launchTransportMatches(
      parsedLaunchTransport(target.getItem(key), identity), identity, payload)
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
        ? { identity, storageKey: key, runId: '', updatedAt: 0, invalid: true }
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

export function clearLaunchTransport(identity, storage = undefined, expectedState = null) {
  const target = transportStorage(storage)
  if (!target || !safeLaunchText(identity, 300)) return false
  try {
    const key = launchTransportKey(identity)
    const currentRaw = target.getItem(key)
    if (expectedState && currentRaw != null
        && !launchTransportMatches(
          parsedLaunchTransport(currentRaw, identity), identity, expectedState)) return false
    target.removeItem(key)
    const cleared = target.getItem(key) == null
    if (cleared && storage === undefined) notifyLaunchTransports()
    return cleared
  } catch { return false }
}

// A malformed record may contain an identity that the normal transport API intentionally refuses
// to address. The global recovery surface can still release that one exact namespace key after an
// explicit warning, but only while its current value remains malformed; a concurrently repaired
// valid startup record is never removed.
export function clearDamagedLaunchTransport(storageKey, storage = undefined) {
  const target = transportStorage(storage)
  if (!target || typeof storageKey !== 'string'
      || !storageKey.startsWith(LAUNCH_TRANSPORT_PREFIX)) return false
  try {
    const currentRaw = target.getItem(storageKey)
    if (currentRaw == null) {
      if (storage === undefined) notifyLaunchTransports()
      return true
    }
    const encoded = storageKey.slice(LAUNCH_TRANSPORT_PREFIX.length)
    let identity
    try { identity = decodeURIComponent(encoded) } catch { identity = encoded }
    if (!parsedLaunchTransport(currentRaw, identity).invalid) return false
    target.removeItem(storageKey)
    const cleared = target.getItem(storageKey) == null
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

const commandTransportActions = source =>
  source === 'dock' ? TRANSPORT_ACTIONS : ASSISTANT_TRANSPORT_ACTIONS
const commandTransportKey = (source, runId) =>
  source === 'dock' ? transportStorageKey(runId) : assistantTransportStorageKey(runId)
const commandEnvelopeKeys = source =>
  source === 'dock' ? RUN_ENVELOPE_KEYS : ASSISTANT_ENVELOPE_KEYS

// Dock and Assistant share one durable command envelope implementation. A key is committed before
// POST, so reload can recover the SAME intent even when every response was lost; the Assistant's
// optional node identity is the only source-specific payload and remains strictly allow-listed.
function saveCommandTransport(source, runId, state, storage) {
  const target = transportStorage(storage)
  if (!target || !runId || !commandTransportActions(source).has(state?.action)
      || !state?.idempotencyKey) return false
  const record = storedRecord(state.record, state.action, source)
  if (!record) return false
  const explicitId = String(state.commandId || '')
  if ((explicitId && !COMMAND_ID_RE.test(explicitId))
      || (explicitId && record.id && explicitId !== record.id)) return false
  const commandId = explicitId || record.id || ''
  const expectedGeneration = state.expectedGeneration
  if (!validRunGeneration(expectedGeneration)) return false
  let arg = null
  let nodeGeneration = null
  if (source === 'assistant' && state.action === 'approve') {
    arg = state.arg == null ? null : Number(state.arg)
    nodeGeneration = state.nodeGeneration == null ? null : Number(state.nodeGeneration)
    if ((arg != null && (!Number.isSafeInteger(arg) || arg < 0))
        || (nodeGeneration != null
          && (!Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0))) return false
    // Before the server returns a durable command id, recovery must retain the exact node lifecycle
    // inspected by the user. Re-fetching a later attempt would turn recovery into a new action.
    if (!commandId && (arg == null || nodeGeneration == null)) return false
  } else if (source === 'assistant' && state.nodeGeneration != null) return false
  const payload = {
    runId: String(runId), action: state.action,
    ...(source === 'assistant' ? { arg, nodeGeneration } : {}),
    expectedGeneration, idempotencyKey: String(state.idempotencyKey), commandId, record,
    statusUnavailable: !!state.statusUnavailable,
    observationKind: OBSERVATION_KINDS.has(state.observationKind || null) ? state.observationKind || null : 'protocol',
    retrying: !!state.retrying, checking: !!state.checking,
    updatedAt: Date.now(),
    committed: true,
  }
  const pending = commandStatePending(payload)
  const lockState = { ...payload, source }
  const currentLock = loadRunCommandLock(runId, target)
  const prospectiveLock = {
    source, action: payload.action, idempotencyKey: payload.idempotencyKey,
    expectedGeneration, commandId, status: record.status,
  }
  if (pending && !compatibleCommandLock(currentLock, prospectiveLock)) return false
  if (pending && currentLock?.commandId && !commandId) return false
  try {
    const key = commandTransportKey(source, runId)
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
      source, idempotencyKey: payload.idempotencyKey, action: payload.action,
      expectedGeneration, commandId,
    }, storage)
    return true
  } catch { return false }
}

function loadCommandTransport(source, runId, storage) {
  const target = transportStorage(storage)
  if (!target || !runId) return null
  try {
    const raw = target.getItem(commandTransportKey(source, runId))
    if (raw == null) return null
    let payload
    try { payload = JSON.parse(raw) } catch { return protocolTransport(runId, source) }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)
        || !hasOnlyKeys(payload, commandEnvelopeKeys(source))
        || payload.runId !== String(runId) || !commandTransportActions(source).has(payload.action)
        || !validRunGeneration(payload.expectedGeneration)
        || !safeIdentityText(payload.idempotencyKey)
        || typeof payload.commandId !== 'string'
        || (payload.commandId && !COMMAND_ID_RE.test(payload.commandId))
        || typeof payload.statusUnavailable !== 'boolean'
        || !OBSERVATION_KINDS.has(payload.observationKind)
        || typeof payload.retrying !== 'boolean' || typeof payload.checking !== 'boolean'
        || payload.committed !== true
        || !Number.isFinite(payload.updatedAt)) return protocolTransport(runId, source, payload)
    const record = storedRecord(payload.record, payload.action, source, { strict: true })
    if (!record || (payload.commandId && record.id && payload.commandId !== record.id)
        || (!!payload.commandId !== !!record.id)) return protocolTransport(runId, source, payload)
    let assistantFields = {}
    if (source === 'assistant') {
      const arg = payload.action === 'approve' && payload.arg != null ? Number(payload.arg) : null
      const nodeGeneration = payload.action === 'approve' && payload.nodeGeneration != null
        ? Number(payload.nodeGeneration) : null
      if ((payload.action === 'approve' && ((arg != null
          && (!Number.isSafeInteger(arg) || arg < 0))
        || (nodeGeneration != null
          && (!Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0))
        || (!payload.commandId && (arg == null || nodeGeneration == null))))
        || (payload.action !== 'approve'
          && (payload.arg !== null || payload.nodeGeneration != null))) {
        return protocolTransport(runId, source, payload)
      }
      assistantFields = { arg, nodeGeneration }
    }
    const { committed: _committed, ...restored } = payload
    return { ...restored, ...assistantFields, commandId: payload.commandId,
      record, lastError: '' }
  } catch { return protocolTransport(runId, source) }
}

function clearCommandTransport(source, runId, storage, expected = {}) {
  const target = transportStorage(storage)
  if (!target || !runId) return false
  try {
    const saved = loadCommandTransport(source, runId, target)
    if (source === 'assistant' && saved && expected.idempotencyKey
        && saved.idempotencyKey !== expected.idempotencyKey) return false
    target.removeItem(commandTransportKey(source, runId))
    clearRunCommandLock(runId, {
      source, idempotencyKey: saved?.idempotencyKey,
      action: saved?.action, expectedGeneration: saved?.expectedGeneration,
      commandId: saved?.commandId,
    }, storage)
    return true
  } catch { return false }
}

export function saveRunTransport(runId, state, storage = undefined) {
  return saveCommandTransport('dock', runId, state, storage)
}

export function loadRunTransport(runId, storage = undefined) {
  return loadCommandTransport('dock', runId, storage)
}

export function clearRunTransport(runId, storage = undefined) {
  return clearCommandTransport('dock', runId, storage)
}

export function saveAssistantRunTransport(runId, state, storage = undefined) {
  return saveCommandTransport('assistant', runId, state, storage)
}

export function loadAssistantRunTransport(runId, storage = undefined) {
  return loadCommandTransport('assistant', runId, storage)
}

export function clearAssistantRunTransport(runId, storage = undefined, expected = {}) {
  return clearCommandTransport('assistant', runId, storage, expected)
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

const commandJson = (path, options, timeoutMs, { errorPath = path, submission = false } = {}) =>
  commandFetch(path, options, timeoutMs, async response => {
    if (!response.ok) await _throw(response, errorPath)
    return commandResponseJson(response, errorPath, { submission })
  })

const commandRead = (path, {
  errorPath = path, signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) => commandJson(path, {
  headers: _authHeaders({}), cache: 'no-store', signal,
}, requestTimeoutMs, { errorPath })

const notifyCommandRecord = (callback, record) => {
  if (!callback) return
  try { callback(record) } catch { /* persistence/presentation must not break command execution */ }
}

export async function submitRunCommand(runId, type, data = {}, {
  idempotencyKey = createIdempotencyKey(), expectedGeneration,
  requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  const path = runApiPath(runId, '/commands')
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

const reportRefreshStorageKey = runId =>
  'll.report-refresh.' + encodeURIComponent(String(runId || ''))

// Read-only inspection shares the exact validation path below and never creates, clears, or
// rewrites an identity.
export function peekReportRefreshIntent(runId, generation, storage = undefined) {
  return reportRefreshIntent(runId, generation, '', storage, true)
}

// Keep one logical refresh identity across component unmounts and ambiguous responses. A retry POST
// can then rejoin the server's first job. Supplying `completedKey` clears only that exact intent;
// paid work fails closed when tab storage is unavailable.
export function reportRefreshIntent(
  runId, generation, completedKey = '', storage = undefined, inspectOnly = false,
) {
  const backing = transportStorage(storage)
  if (!validRunGeneration(generation)) return null
  if (!backing) throw reportStorageError()
  const key = reportRefreshStorageKey(runId)
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
    if (inspectOnly) return candidate === null ? null : { generation, idempotencyKey: candidate }
    const idempotencyKey = candidate ?? createIdempotencyKey()
    backing.setItem(key, prefix + idempotencyKey)
    if (backing.getItem(key) !== prefix + idempotencyKey) throw new Error('storage write failed')
    return { generation, idempotencyKey }
  } catch (cause) { throw reportStorageError(cause) }
}

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
  // `max_eval_seconds` is the absolute cumulative ceiling (durable LWW), not an additive delta.
  setEvalCeiling: (rid, seconds) =>
    runCommand(rid, 'budget_extend', { max_eval_seconds: seconds }),
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
  // PART V Phase 2c: an operator replaces ONE node's concept tags (full set). Generation-fenced like a
  // comment (the node's attempt separate from the displayed run generation), so a late click cannot
  // re-tag a replacement run or a reset incarnation of the same numeric node id. Folds with
  // `operator-edited` provenance the classifier re-tag cadence must not clobber.
  retagConcepts: (rid, { nodeId, nodeGeneration, concepts }, options = {}) => runCommand(
    rid, 'concept_tag_edited', {
      node_id: nodeId, node_generation: nodeGeneration, concepts,
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
  // A7/L2: pin the Strategist live. The strict server contract accepts policy/fidelity plus canonical
  // eval_parallel, llm_parallel, the closed llm_lane_limits allocation, and the atomic Card-scoring
  // treatment (never legacy aliases).
  // {policy?, policy_params?, fidelity?, eval_parallel?, llm_parallel?, llm_lane_limits?, card_scoring?}.
  setStrategy: (rid, strategy) => runCommand(rid, 'set_strategy', { strategy }),
  // P2: ask the engine to run the Deep-Research stage now (read all results + the web, write a memo).
  deepResearch: (rid) => runCommand(rid, 'deep_research', {}),
  // P1: register an open hypothesis on the board (a question the search should resolve), or drop one.
  addHypothesis: (rid, statement) => runCommand(rid, 'hypothesis_added', { statement, source: 'human' }),
  abandonHypothesis: (rid, id) => runCommand(rid, 'hypothesis_updated', { id, status: 'abandoned' }),
  deleteHypothesis: (rid, id) => runCommand(rid, 'hypothesis_updated', { id, status: 'deleted' }),
  // Layer-6 Card board controls. Authority/provenance is server-stamped; clients submit only the
  // exact subject and editable value through the generation-fenced command protocol.
  reprioritizeCard: (rid, id, priority) => runCommand(
    rid, 'card_reprioritized', { id, priority }),
  editCard: (rid, id, statement) => runCommand(rid, 'card_edited', { id, statement }),
  pinCardResources: (rid, id, gpus, gpuMemMiB = null) => runCommand(
    rid, 'card_resource_pinned', {
      id, gpus, ...(gpuMemMiB == null ? {} : { gpu_mem_mib: gpuMemMiB }),
    }),
  dropCard: (rid, id, reason = 'operator dropped') => runCommand(
    rid, 'card_dropped', { id, reason }),
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
    const response = await commandJson(path, {
      method: 'POST',
      headers: _authHeaders({
        'Content-Type': 'application/json', 'Idempotency-Key': String(idempotencyKey),
      }),
      body: JSON.stringify({ expected_generation: expectedGeneration }),
      signal,
    }, requestTimeoutMs, { submission: true })
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

// Generation-fenced, operation-idempotent Replay: archive the finished generation and re-spawn the
// same run id. The durable operation id lets an unknown browser outcome rejoin the exact request.
export const resetRun = (rid, expectedGeneration, operationId, options = {}) =>
  post(runApiPath(rid, '/reset'), {
    expected_generation: expectedGeneration,
    operation_id: operationId,
  }, { ...options, allowRunMutationModes: ['start-over', 'stale-link', 'history'] })

// Clear ONE node's trace only for the exact run + node lifecycle the operator inspected. Never
// substitute a later observed generation here: the confirmation belongs to the rendered snapshot,
// and a stale tab must fail closed instead of deleting a replacement run/attempt's diagnostics.
export const clearNodeTrace = (
  rid, id, {
    expectedGeneration, expectedTraceRevision, nodeGeneration, operationId, signal,
  } = {},
) => {
  if (!validRunGeneration(expectedGeneration)) {
    const error = new Error('An exact run generation is required to clear trace data.')
    error.code = 'run_generation_unavailable'
    throw error
  }
  if (!Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0) {
    const error = new Error('An exact experiment attempt is required to clear trace data.')
    error.code = 'node_generation_unavailable'
    throw error
  }
  if (!validRunGeneration(expectedTraceRevision)) {
    const error = new Error('An exact trace snapshot is required to clear trace data.')
    error.code = 'trace_revision_unavailable'
    throw error
  }
  if (!/^tc_[0-9a-f]{32}$/.test(operationId || '')) {
    const error = new Error('A stable trace clear operation id is required.')
    error.code = 'trace_clear_operation_unavailable'
    throw error
  }
  return post(runNodeApiPath(rid, id, '/clear_trace'), {
    expected_generation: expectedGeneration,
    expected_trace_revision: expectedTraceRevision,
    node_generation: nodeGeneration,
    operation_id: operationId,
  }, { signal })
}

const validSettingsRevision = value => typeof value === 'string' && value.length > 0 && value.length <= 256
const validLlmHealthIdentity = value => typeof value === 'string'
  && /^probe-v1:[\da-f]{64}$/i.test(value)
const validLlmHealthFailureCode = value => typeof value === 'string'
  && /^[a-z][a-z0-9_:-]{0,127}$/i.test(value)

const llmHealthRevisionError = (code, message, detail = null) => {
  const error = new Error(message)
  error.code = code
  if (detail) error.detail = detail
  return error
}

export async function llmHealth(
  expectedSettingsRevision, expectedSecretRevision, operationId, options = {},
) {
  if (!validSettingsRevision(expectedSettingsRevision) || !validSettingsRevision(expectedSecretRevision)) {
    throw llmHealthRevisionError(
      'llm_health_revision_unavailable',
      'Saved settings revisions are required before testing the LLM configuration.',
    )
  }
  if (!UUID_V4_RE.test(operationId || '')) {
    throw llmHealthRevisionError(
      'llm_health_operation_unavailable',
      'A unique operation identity is required before testing the LLM configuration.',
    )
  }
  const { replayOnly = false, ...requestOptions } = options
  let payload
  try {
    payload = await post('/api/llm/health', {
      expected_settings_revision: expectedSettingsRevision,
      expected_secret_revision: expectedSecretRevision,
      operation_id: operationId,
      replay_only: replayOnly === true,
    }, requestOptions)
  } catch (error) {
    const detail = error?.detail && typeof error.detail === 'object'
      && !Array.isArray(error.detail) ? error.detail : null
    const exactErrorEnvelope = detail
      && detail.operation_id === operationId
      && detail.expected_settings_revision === expectedSettingsRevision
      && detail.expected_secret_revision === expectedSecretRevision
      && validLlmHealthFailureCode(detail.code)
      && typeof detail.provider_attempted === 'boolean'
      && typeof detail.outcome_unknown === 'boolean'
      && (detail.ambiguous == null || typeof detail.ambiguous === 'boolean')
      && !(detail.ambiguous === true && detail.outcome_unknown !== true)
    if (!exactErrorEnvelope) {
      throw llmHealthRevisionError(
        'llm_health_identity_protocol_error',
        'The LLM health error did not prove which operation or saved configuration it belonged to.',
        {
          operation_id: operationId,
          provider_attempted: detail?.provider_attempted === true,
          ambiguous: true,
          outcome_unknown: true,
        },
      )
    }
    throw error
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || payload.operation_id !== operationId
      || payload.settings_revision !== expectedSettingsRevision
      || payload.secret_revision !== expectedSecretRevision
      || typeof payload.ok !== 'boolean'
      || typeof payload.provider_attempted !== 'boolean'
      || typeof payload.outcome_unknown !== 'boolean'
      || (payload.ambiguous != null && typeof payload.ambiguous !== 'boolean')
      || !validLlmHealthIdentity(payload.effective_identity)
      || (payload.ok === true
        && (payload.provider_attempted !== true || payload.outcome_unknown !== false))
      || (payload.outcome_unknown === true && payload.provider_attempted !== true)
      || (payload.ambiguous === true && payload.outcome_unknown !== true)
      || (payload.ok === false
        && !validLlmHealthFailureCode(payload.code || payload.error_kind))) {
    throw llmHealthRevisionError(
      'llm_health_identity_protocol_error',
      'The LLM health response did not match the requested operation, saved settings revisions, or provider-attempt contract.',
      {
        operation_id: operationId,
        provider_attempted: payload?.provider_attempted === true,
        // A malformed success envelope cannot prove that a paid provider attempt did not happen.
        ambiguous: true,
        outcome_unknown: true,
      },
    )
  }
  return payload
}

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
export const deadlineGet = (path, timeout = 8000, options) =>
  deadlineRequest(signal => get(path, { cache: 'no-store', ...options, signal }), timeout)

// A public Assistant capability is a separate principal. Never pass it through `get()`: an owner
// opening their own snapshot would otherwise send both the share bearer and X-LoopLab-Token to an
// intentionally unauthenticated route. This helper has no caller-supplied header merge by design.
export const deadlineSharedAssistant = (shareToken, timeout = 8000) => deadlineRequest(async signal => {
  const path = '/api/assistant/shared'
  const r = await fetch(apiUrl(path), {
    method: 'GET', cache: 'no-store', credentials: 'omit', signal,
    headers: { 'X-LoopLab-Share': String(shareToken || '') },
  })
  if (!r.ok) await _throw(r, path)
  return r.json()
}, timeout)

const artifactGenerationQuery = expectedGeneration => {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError(
      'run_generation_unavailable',
      'A verified run generation is required before reading files.',
      'Wait for the current run identity, then reopen Files.',
    )
  }
  const query = new URLSearchParams()
  query.set('expected_generation', expectedGeneration)
  return query
}

const validateArtifactGeneration = (payload, expectedGeneration, path) => {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || !validRunGeneration(payload.run_generation)) {
    const error = new Error(`${path}: invalid artifact generation receipt`)
    error.code = 'artifact_generation_protocol_error'
    throw error
  }
  if (payload.run_generation !== expectedGeneration) {
    const error = runGenerationError(
      'run_generation_changed',
      'The run changed while files were loading.',
      'Reload the file inventory for the current run generation.',
    )
    error.status = 409
    error.detail = {
      expected_generation: expectedGeneration,
      current_generation: payload.run_generation,
    }
    throw error
  }
  return payload
}

const artifactProtocolError = (code, message, path) => {
  const error = new Error(`${path}: ${message}`)
  error.code = code
  return error
}

const validateArtifactContentIdentity = (payload, expected, requestPath) => {
  if (payload.root !== expected.root || payload.path !== expected.path) {
    throw artifactProtocolError(
      'artifact_path_protocol_error',
      'the response did not echo the requested file identity',
      requestPath,
    )
  }
  const responseHasNodeIdentity = payload.node_id != null || payload.attempt != null
  if (responseHasNodeIdentity !== expected.hasNodeIdentity
      || (expected.hasNodeIdentity
        && (payload.node_id !== expected.nodeId || payload.attempt !== expected.attempt))) {
    throw artifactProtocolError(
      'artifact_attempt_protocol_error',
      'the response did not echo the requested experiment attempt',
      requestPath,
    )
  }
  return payload
}

export async function getRunArtifactInventory(runId, expectedGeneration, options = {}) {
  const query = artifactGenerationQuery(expectedGeneration)
  const path = runApiPath(runId, `/artifacts?${query}`)
  const payload = await get(path, { ...options, cache: 'no-store' })
  const fenced = validateArtifactGeneration(payload, expectedGeneration, path)
  if (fenced.run_id !== String(runId)) {
    throw artifactProtocolError(
      'artifact_run_protocol_error',
      'the response did not echo the requested run identity',
      path,
    )
  }
  return fenced
}

export async function getRunArtifactContent(runId, {
  root, path: artifactPath, expectedGeneration, nodeId = null, attempt = null, ...options
} = {}) {
  if (typeof root !== 'string' || typeof artifactPath !== 'string') {
    throw artifactProtocolError(
      'artifact_path_protocol_error',
      'the file inventory returned an invalid root or path',
      runApiPath(runId, '/artifact'),
    )
  }
  const query = artifactGenerationQuery(expectedGeneration)
  query.set('root', root)
  query.set('path', artifactPath)
  const hasNodeIdentity = nodeId != null || attempt != null
  if (hasNodeIdentity) {
    if (!Number.isSafeInteger(nodeId) || nodeId < 0
        || !Number.isSafeInteger(attempt) || attempt < 0) {
      const error = new Error('The artifact inventory returned an invalid experiment attempt identity.')
      error.code = 'artifact_attempt_protocol_error'
      throw error
    }
    query.set('node_id', String(nodeId))
    query.set('attempt', String(attempt))
  }
  const requestPath = runApiPath(runId, `/artifact?${query}`)
  const payload = await get(requestPath, { ...options, cache: 'no-store' })
  const fenced = validateArtifactGeneration(payload, expectedGeneration, requestPath)
  return validateArtifactContentIdentity(fenced, {
    root,
    path: artifactPath,
    nodeId,
    attempt,
    hasNodeIdentity,
  }, requestPath)
}

export async function post(path, body, { signal, allowRunMutationModes = [] } = {}) {
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path, { allowModes: allowRunMutationModes })
  const r = await fetch(apiUrl(path), {
    method: 'POST', signal,
    headers: _authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
export async function putText(path, text, { signal } = {}) {
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  const r = await fetch(apiUrl(path), {
    method: 'PUT', signal,
    headers: _authHeaders({ 'Content-Type': 'text/plain' }), body: text,
  })
  if (!r.ok) await _throw(r, path)
  return r.json()
}

export const AUTHORING_MISSING_REVISION = 'missing'
const AUTHORING_KINDS = new Set(['prompts', 'skills', 'knowledge'])
const AUTHORING_MAX_BYTES = 256 * 1024
const AUTHORING_REVISION_RE = /^(?:missing|sha256:[0-9a-f]{64})$/
const AUTHORING_RESULT_REVISION_RE = /^(?:missing|oversized|sha256:[0-9a-f]{64})$/
const AUTHORING_TARGET_ROOT_ID_RE = /^root-sha256:[0-9a-f]{64}$/
const AUTHORING_RECEIPT_KEYS = new Set([
  'schema', 'operation_id', 'kind', 'name', 'target_root_id', 'expected_revision',
  'desired_revision', 'status', 'result_revision', 'code', 'created_at', 'updated_at',
  'ok', 'replayable',
])
export const validAuthoringName = value => typeof value === 'string'
  && value.length > 3 && [...value].length <= 255 && value.endsWith('.md')
  && !/[\\/]/.test(value) && !/\p{C}/u.test(value)
const validAuthoringOperationId = value => typeof value === 'string'
  && value.length === 36 && value === value.toLowerCase() && UUID_V4_RE.test(value)
const validAuthoringRevision = value => typeof value === 'string'
  && (value === AUTHORING_MISSING_REVISION
    || (value.length === 71 && AUTHORING_REVISION_RE.test(value)))
const validAuthoringDesiredRevision = value => value !== AUTHORING_MISSING_REVISION
  && validAuthoringRevision(value)
const validAuthoringResultRevision = value => typeof value === 'string'
  && (value === AUTHORING_MISSING_REVISION || value === 'oversized'
    || (value.length === 71 && AUTHORING_RESULT_REVISION_RE.test(value)))
export const validAuthoringTargetRootId = value => typeof value === 'string'
  && value.length === 76 && AUTHORING_TARGET_ROOT_ID_RE.test(value)
const authoringTextWellFormed = text => {
  if (typeof text !== 'string') return false
  if (typeof text.isWellFormed === 'function') return text.isWellFormed()
  for (let index = 0; index < text.length; index++) {
    const unit = text.charCodeAt(index)
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = text.charCodeAt(index + 1)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false
      index++
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false
  }
  return true
}

const authoringOperationPath = (kind, name, operationId) => {
  const normalizedKind = String(kind || '')
  const normalizedName = String(name || '')
  const normalizedOperationId = String(operationId || '')
  if (!AUTHORING_KINDS.has(normalizedKind) || !validAuthoringName(normalizedName)
      || !validAuthoringOperationId(normalizedOperationId)) {
    const error = new Error('Invalid authoring operation identity')
    error.code = 'AUTHORING_PROTOCOL_ERROR'
    throw error
  }
  return `/api/${encodeURIComponent(normalizedKind)}/${encodeURIComponent(normalizedName)}`
    + `/operations/${encodeURIComponent(normalizedOperationId)}`
}

function authoringProtocolError(message, receipt = null) {
  const error = new Error(message)
  error.code = 'AUTHORING_PROTOCOL_ERROR'
  if (receipt && typeof receipt === 'object') error.receipt = receipt
  return error
}

export async function authoringTextRevision(text, source = globalThis.crypto) {
  if (typeof text !== 'string' || typeof TextEncoder === 'undefined'
      || typeof source?.subtle?.digest !== 'function') {
    throw authoringProtocolError('Secure UTF-8 hashing is unavailable for this authoring payload.')
  }
  if (!authoringTextWellFormed(text)) {
    throw authoringProtocolError('Authoring text contains an unpaired Unicode surrogate.')
  }
  const bytes = new TextEncoder().encode(text)
  if (bytes.byteLength > AUTHORING_MAX_BYTES) {
    throw authoringProtocolError(`Authoring text exceeds ${AUTHORING_MAX_BYTES} UTF-8 bytes.`)
  }
  const digest = await source.subtle.digest('SHA-256', bytes)
  return 'sha256:' + [...new Uint8Array(digest)]
    .map(value => value.toString(16).padStart(2, '0')).join('')
}

export function validateAuthoringReceipt(receipt, expected = {}) {
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)
      || !hasOnlyKeys(receipt, AUTHORING_RECEIPT_KEYS)
      || Object.keys(receipt).length !== AUTHORING_RECEIPT_KEYS.size
      || receipt.schema !== 'looplab.authoring-operation/v1'
      || !validAuthoringOperationId(receipt.operation_id)
      || !AUTHORING_KINDS.has(receipt.kind)
      || !validAuthoringName(receipt.name)
      || !validAuthoringTargetRootId(receipt.target_root_id)
      || !validAuthoringRevision(receipt.expected_revision)
      || !validAuthoringDesiredRevision(receipt.desired_revision)
      || !['prepared', 'succeeded', 'conflict'].includes(receipt.status)
      || !Number.isSafeInteger(receipt.created_at) || receipt.created_at < 0
      || !Number.isSafeInteger(receipt.updated_at) || receipt.updated_at < receipt.created_at) {
    throw authoringProtocolError('The server returned an invalid authoring receipt.', receipt)
  }
  const validTerminal = receipt.status === 'prepared'
    ? receipt.result_revision === null && receipt.code === null
    : receipt.status === 'succeeded'
      ? receipt.result_revision === receipt.desired_revision && receipt.code === null
      : validAuthoringResultRevision(receipt.result_revision)
        && ['authoring_revision_conflict', 'authoring_intervening_write'].includes(receipt.code)
  if (!validTerminal || receipt.ok !== (receipt.status === 'succeeded')
      || receipt.replayable !== (receipt.status === 'prepared')) {
    throw authoringProtocolError('The server returned an inconsistent authoring receipt.', receipt)
  }
  const expectedOperationId = expected.operationId == null ? null : String(expected.operationId)
  const expectedKind = expected.kind == null ? null : String(expected.kind)
  const expectedName = expected.name == null ? null : String(expected.name)
  const expectedRevision = expected.expectedRevision == null
    ? null : String(expected.expectedRevision)
  const expectedTargetRootId = expected.expectedTargetRootId == null
    ? null : String(expected.expectedTargetRootId)
  const desiredRevision = expected.desiredRevision == null
    ? null : String(expected.desiredRevision)
  if ((expectedOperationId != null && receipt.operation_id !== expectedOperationId)
      || (expectedKind != null && receipt.kind !== expectedKind)
      || (expectedName != null && receipt.name !== expectedName)
      || (expectedTargetRootId != null && receipt.target_root_id !== expectedTargetRootId)
      || (expectedRevision != null && receipt.expected_revision !== expectedRevision)
      || (desiredRevision != null && receipt.desired_revision !== desiredRevision)) {
    throw authoringProtocolError(
      'The authoring receipt does not match the requested operation identity.', receipt)
  }
  return receipt
}

export async function putAuthoringOperation(
  kind, name, operationId, {
    text, expectedRevision, expectedTargetRootId, desiredRevision = null,
  }, { signal } = {},
) {
  const path = authoringOperationPath(kind, name, operationId)
  if (typeof text !== 'string' || !validAuthoringRevision(expectedRevision)
      || !validAuthoringTargetRootId(expectedTargetRootId)) {
    throw authoringProtocolError('Invalid authoring operation payload.')
  }
  const submittedRevision = await authoringTextRevision(text)
  if (desiredRevision != null && desiredRevision !== submittedRevision) {
    throw authoringProtocolError('The authoring payload does not match its durable desired revision.')
  }
  const receipt = await send(path, 'PUT', {
    text,
    expected_revision: expectedRevision,
    expected_target_root_id: expectedTargetRootId,
  }, { signal })
  return validateAuthoringReceipt(receipt, {
    operationId, kind, name, expectedRevision, expectedTargetRootId,
    desiredRevision: submittedRevision,
  })
}

export async function getAuthoringOperation(
  kind, name, operationId, {
    signal, expectedRevision, expectedTargetRootId, desiredRevision,
  } = {},
) {
  if (!validAuthoringTargetRootId(expectedTargetRootId)
      || !validAuthoringRevision(expectedRevision)
      || !validAuthoringDesiredRevision(desiredRevision)) {
    throw authoringProtocolError('An exact authoring operation identity is required for receipt lookup.')
  }
  const path = authoringOperationPath(kind, name, operationId)
    + `?expected_target_root_id=${encodeURIComponent(expectedTargetRootId)}`
    + `&expected_revision=${encodeURIComponent(expectedRevision)}`
    + `&desired_revision=${encodeURIComponent(desiredRevision)}`
  const receipt = await get(path, { signal, cache: 'no-store' })
  return validateAuthoringReceipt(receipt, {
    operationId, kind, name, expectedRevision, expectedTargetRootId, desiredRevision,
  })
}

async function send(path, method, body, { signal } = {}) {
  if (method !== 'GET') assertNotReviewMutation(path)
  if (method !== 'GET') assertRunMutationAllowed(path)
  // Only attach a JSON body for methods that carry one (PATCH/PUT/POST). A DELETE with a request
  // body + Content-Type is unusual and some reverse proxies (e.g. jupyter-server-proxy) mishandle it
  // — which surfaced as a 500 on "delete chat"/"delete run". DELETE goes bodyless.
  const hasBody = method !== 'DELETE' && method !== 'GET'
  const opts = {
    method, signal,
    headers: _authHeaders(hasBody ? { 'Content-Type': 'application/json' } : {}),
  }
  if (hasBody) opts.body = JSON.stringify(body || {})
  const r = await fetch(apiUrl(path), opts)
  if (!r.ok) await _throw(r, path)
  return r.json()
}

export const authStatus = (options = {}) => get('/api/auth/status', options)
export async function verifyOwnerToken(token, { signal } = {}) {
  const r = await fetch(apiUrl('/api/auth/verify'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LoopLab-Token': String(token || '') },
    body: '{}',
    signal,
  })
  if (!r.ok) await _throw(r, '/api/auth/verify')
  const data = await r.json()
  // A deadline may abort while a non-standard fetch implementation is still resolving. Never
  // persist a credential from that late response; the mounted auth gate has already rejected it.
  if (signal?.aborted) {
    const error = new Error('Owner token verification was aborted')
    error.name = 'AbortError'
    throw error
  }
  setOwnerToken(token)
  return data
}
export const clearOwnerToken = () => setOwnerToken('')
// ---- ClearML-style project API ----
export const listProjects = options => get('/api/projects', options)
export const createProject = (name, parent_id = null) => post('/api/projects', { name, parent_id })
export const patchProject = (id, body) => send(`/api/projects/${encodeURIComponent(id)}`, 'PATCH', body)
export const deleteProject = (id) => send(`/api/projects/${encodeURIComponent(id)}`, 'DELETE')
const runOrganizationBody = (field, value, expectedGeneration, expectedCurrent) => {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError(
      'run_generation_unavailable',
      'An exact observed run generation is required before changing run organization.',
      'Refresh the Runs list and repeat the change on the intended run.',
    )
  }
  if (expectedCurrent !== null && typeof expectedCurrent !== 'string') {
    throw runGenerationError(
      'run_organization_unavailable',
      'The exact current organization value is required before changing run organization.',
      'Refresh the Runs list and repeat the change from the current run card.',
    )
  }
  return {
    [field]: value,
    [`expected_${field}`]: expectedCurrent,
    expected_generation: expectedGeneration,
  }
}
export const assignRun = (runId, project_id, expectedGeneration, expectedProjectId) => post(
  runApiPath(runId, '/project'),
  runOrganizationBody('project_id', project_id, expectedGeneration, expectedProjectId))
export const renameRun = (runId, label, expectedGeneration, expectedLabel) => send(
  runApiPath(runId), 'PATCH',
  runOrganizationBody('label', label, expectedGeneration, expectedLabel))
export function submitRunDeletion(runId, expectedGeneration, expectedSeq, operationId, options = {}) {
  if (!validRunGeneration(expectedGeneration)) {
    throw Object.assign(new Error('An exact run generation is required to delete a run.'), {
      code: 'run_generation_unavailable',
    })
  }
  if (!Number.isSafeInteger(expectedSeq) || expectedSeq < -1) {
    throw Object.assign(new Error('An exact run sequence is required to delete a run.'), {
      code: 'run_sequence_unavailable',
    })
  }
  if (!UUID_V4_RE.test(operationId || '')) {
    throw Object.assign(new Error('A stable deletion operation id is required.'), {
      code: 'delete_operation_invalid',
    })
  }
  return post(runApiPath(runId, '/deletions'), {
    operation_id: String(operationId).toLowerCase(),
    expected_generation: expectedGeneration,
    expected_seq: expectedSeq,
  }, options)
}

export function getRunDeletion(runId, operationId, options = {}) {
  if (!UUID_V4_RE.test(operationId || '')) {
    throw Object.assign(new Error('A stable deletion operation id is required.'), {
      code: 'delete_operation_invalid',
    })
  }
  return get(runApiPath(runId, `/deletions/${encodeURIComponent(String(operationId).toLowerCase())}`), {
    ...options, cache: 'no-store',
  })
}
export const createRunReview = (runId, {
  ttl_seconds, include_evidence = false, expected_generation, request_id, token_secret,
} = {}, options) =>
  post(runApiPath(runId, '/reviews'),
    { ttl_seconds, include_evidence, expected_generation, request_id, token_secret }, options)
export const listRunReviews = (runId, options) =>
  get(runApiPath(runId, '/reviews'), options)
export const revokeRunReview = (runId, linkId, options) =>
  send(runApiPath(runId, `/reviews/${encodeURIComponent(linkId)}`),
    'DELETE', null, options)

// Bounded collaboration projections. In review mode reviewReadPath() translates both owner paths to
// `/api/review/comments...`; the capability still supplies the run identity and every mutation is
// rejected before fetch by assertNotReviewMutation().
export const runComments = (runId, {
  nodeId = null, nodeGeneration = null, includeResolved = true, limit = 100, cursor = null,
  signal,
} = {}) => {
  const query = new URLSearchParams()
  if (nodeId != null) query.set('node_id', String(nodeId))
  if (nodeGeneration != null) query.set('node_generation', String(nodeGeneration))
  query.set('include_resolved', includeResolved ? 'true' : 'false')
  query.set('limit', String(Math.max(1, Math.min(100, Math.trunc(Number(limit) || 100)))))
  if (cursor != null) query.set('cursor', String(cursor))
  return get(runApiPath(runId, `/comments?${query}`),
    { cache: 'no-store', signal })
}
export const commentHistory = (runId, commentId, { limit = 100, cursor = null } = {}) => {
  const query = new URLSearchParams()
  query.set('limit', String(Math.max(1, Math.min(100, Math.trunc(Number(limit) || 100)))))
  if (cursor != null) query.set('cursor', String(cursor))
  return get(runApiPath(runId, `/comments/${encodeURIComponent(commentId)}/history?${query}`),
    { cache: 'no-store' })
}

// super-tasks: a user-managed, flat grouping of runs by the global task they attack (parallel axis
// to projects). create / rename / delete the bucket, then assign any run (existing or new) to it.
export const listSupertasks = options => get('/api/supertasks', options)
export const createSupertask = (name, task_id = null) => post('/api/supertasks', { name, task_id })
export const renameSupertask = (id, name) => send(`/api/supertasks/${encodeURIComponent(id)}`, 'PATCH', { name })
export const deleteSupertask = (id) => send(`/api/supertasks/${encodeURIComponent(id)}`, 'DELETE')
export const assignSupertask = (
  runId, supertask_id, expectedGeneration, expectedSupertaskId,
) => post(
  runApiPath(runId, '/supertask'),
  runOrganizationBody(
    'supertask_id', supertask_id, expectedGeneration, expectedSupertaskId))

export const gpuStat = () => get('/api/gpu')

// ---- settings + run launch ----
export const getSettings = () => get('/api/settings')
export const getSettingsSchema = (options = {}) => get('/api/settings/schema/2', options)
export const saveSettings = (settings, { expectedRevision, ...options } = {}) =>
  send('/api/settings', 'PUT', {
    settings, ...(expectedRevision == null ? {} : { expected_revision: expectedRevision }),
  }, options)
// Store (or clear, value='') a secret credential. The write is fenced by the exact settings +
// secret snapshot displayed by the owner: accepting only one revision could bind a key to an
// endpoint the user never reviewed. The value itself is never echoed back.
export const saveSecret = (key, value, {
  expectedSettingsRevision, expectedSecretRevision, ...options
} = {}) =>
  send('/api/settings/secret', 'PUT', {
    key,
    value,
    ...(expectedSettingsRevision == null
      ? {} : { expected_settings_revision: expectedSettingsRevision }),
    ...(expectedSecretRevision == null
      ? {} : { expected_secret_revision: expectedSecretRevision }),
  }, options)
// Per-run settings: edit a specific run's config.snapshot.json so the next RESUME picks up the
// change (only changed fields are sent). A live engine keeps its in-memory copy until restart.
export const saveRunConfig = (rid, settings, {
  expectedRevision, expectedGeneration, ...options
} = {}) =>
  send(runApiPath(rid, '/config'), 'PUT', {
    settings, ...(expectedRevision == null ? {} : { expected_revision: expectedRevision }),
    ...(expectedGeneration == null ? {} : { expected_generation: expectedGeneration }),
  }, options)

// Experimental Research Atlas: owner-only, read-only projections over the shared memory portfolio.
// Bypass browser caches so Refresh observes newly finalized runs/governance without a stale intermediary.
const crossRunRead = (path, options = {}) => get(path, { ...options, cache: 'no-store' })
export function boundedAtlasText(value, max = 360) {
  if (!['string', 'number', 'boolean'].includes(typeof value)) return ''
  const limit = Number.isSafeInteger(max) ? Math.max(0, Math.min(2000, max)) : 360
  const text = String(value).slice(0, limit)
  return text.replace(/[\u0000-\u001f\u007f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, ' ')
    .replace(/\s+/gu, ' ').trim().slice(0, limit)
}
const CROSS_RUN_STATE_FIELDS = `portfolio_id n_runs n_concepts n_contested concept_source
  claim_source explored thin_coverage thin_coverage_total thin_coverage_omitted contradictions
  revisions claims n revision v status complete entries limit source_complete partial_capsules
  source_unknown_capsules source_concepts_omitted source_outcomes_omitted source_store_complete
  source_rows_total source_rows_quarantined source_malformed_rows source_invalid_capsule_rows
  source_duplicate_run_rows concept runs run_id task_id task scope task_scope metric direction
  n_helped n_neutral n_hurt
  claim_uid statement epistemic maturity decision_fresh n_support n_oppose n_unverified
  n_contradicts support oppose unverified contradicts scopes receipt_known read_complete
  research_source_complete lessons research snapshot_digest rows_total rows_retained
  rows_quarantined malformed_rows invalid_rows outcome proposals receipt merges splits purges
  decisions applied concept_governance`.split(/\s+/)
const CROSS_RUN_STATE_CAPS = {
  explored: 24, thin_coverage: 24, contradictions: 12, claims: 40, entries: 20,
  runs: 6, support: 6, oppose: 6, unverified: 6, contradicts: 6, scopes: 6,
}
const CROSS_RUN_COUNT_ARRAYS = new Set(['merges', 'splits', 'purges', 'decisions', 'applied'])
function projectCrossRunValue(value, key = '', depth = 0) {
  if (typeof value === 'string') return boundedAtlasText(value, 500)
  if (typeof value === 'number' || typeof value === 'boolean' || value == null) return value
  if (Array.isArray(value)) {
    if (CROSS_RUN_COUNT_ARRAYS.has(key)) return value.length
    return value.slice(0, CROSS_RUN_STATE_CAPS[key] || 6)
      .map(item => projectCrossRunValue(item, key, depth + 1))
  }
  if (typeof value !== 'object' || depth >= 7) return null
  const out = {}
  for (const field of CROSS_RUN_STATE_FIELDS) {
    if (Object.hasOwn(value, field)) {
      out[field] = projectCrossRunValue(value[field], field, depth + 1)
    }
  }
  return out
}
export function projectResearchAtlasSource(key, value) {
  const projected = projectCrossRunValue(value)
  if (key === 'atlas') {
    const contested = Array.isArray(value?.contradictions) ? value.contradictions.length : 0
    projected.n_contested = Math.max(
      Number.isSafeInteger(projected.n_contested) && projected.n_contested >= 0
        ? projected.n_contested : 0,
      contested,
    )
  }
  return projected
}
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
const boundedLaunchTimeout = (value, fallback) => {
  const parsed = Number(value)
  return Number.isFinite(parsed)
    ? Math.max(1, Math.min(MAX_LAUNCH_REQUEST_TIMEOUT_MS, Math.trunc(parsed)))
    : fallback
}

const launchDeadline = async (read, timeoutMs, code, { submission = false } = {}) => {
  try {
    return await deadlineRequest(read, timeoutMs).promise
  } catch (cause) {
    const status = Number(cause?.status)
    const ambiguous = cause?.name === 'TimeoutError' || cause?.status == null
      || (Number.isFinite(status) && (status >= 500 || TRANSIENT_HTTP.has(status)))
    if (cause?.name !== 'TimeoutError' && !(submission && ambiguous)) throw cause
    const error = Object.assign(new Error(submission
      ? 'Startup submission did not return before its deadline.'
      : code === 'LAUNCH_PREFLIGHT_TIMEOUT'
        ? 'Launch validation did not return before its deadline.'
        : 'Startup status did not return before its deadline.'), {
      name: cause?.name === 'TimeoutError' ? 'TimeoutError' : (cause?.name || 'Error'),
      code, transient: true, cause,
      ...(submission ? { submissionMayHaveSucceeded: true } : {}),
    })
    throw error
  }
}

export const preflightRunStart = (body, { requestTimeoutMs = LAUNCH_PREFLIGHT_TIMEOUT_MS } = {}) =>
  launchDeadline(
    signal => post('/api/start/preflight', body, { signal }),
    boundedLaunchTimeout(requestTimeoutMs, LAUNCH_PREFLIGHT_TIMEOUT_MS),
    'LAUNCH_PREFLIGHT_TIMEOUT')

// Exactly one POST leaves for a caller invocation. Any lost/late response is marked ambiguous and
// recovered only through getStartStatus with the caller's already-durable idempotency key.
export const startRun = (body, { requestTimeoutMs = LAUNCH_SUBMISSION_TIMEOUT_MS } = {}) =>
  launchDeadline(
    signal => post('/api/start', body, { signal }),
    boundedLaunchTimeout(requestTimeoutMs, LAUNCH_SUBMISSION_TIMEOUT_MS),
    'LAUNCH_SUBMISSION_UNKNOWN', { submission: true })

export const getStartStatus = (runId, idempotencyKey, {
  requestTimeoutMs = LAUNCH_STATUS_TIMEOUT_MS,
} = {}) => launchDeadline(
  signal => get(`/api/start/${encodeURIComponent(runId)}/status`, {
    cache: 'no-store', signal, headers: { 'Idempotency-Key': String(idempotencyKey || '') },
  }),
  boundedLaunchTimeout(requestTimeoutMs, LAUNCH_STATUS_TIMEOUT_MS),
  'LAUNCH_STATUS_TIMEOUT')

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

// once paid work may have crossed the POST boundary, every error carries only bounded
// client-owned identity metadata. Presentation never needs the server/provider body to recover the
// exact action, and callers can never mistake an identity-less failure for permission to re-bill.
const scopeGenerationErrorIdentity = (cause, actionId, jobId = null, ambiguous = true) => {
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

// ---- assistant (general chat agent — the evolution of Genesis) ----
export const assistantSessions = (options) => get('/api/assistant/sessions', options)
export const assistantCreate = (title = '', mode = 'plan', options) =>
  post('/api/assistant/sessions', { title, mode }, options)
export const assistantGet = (sid, options) =>
  get(`/api/assistant/sessions/${encodeURIComponent(sid)}`, options)
export const assistantDelete = (sid, options) =>
  send(`/api/assistant/sessions/${encodeURIComponent(sid)}`, 'DELETE', undefined, options)
const validAssistantForkActionId = value => typeof value === 'string'
  && value === value.toLowerCase() && UUID_V4_RE.test(value)
const assistantForkPath = (sid, actionId = null) => {
  const base = `/api/assistant/sessions/${encodeURIComponent(sid)}/fork`
  return actionId == null ? base : `${base}/${encodeURIComponent(actionId)}`
}
const assistantForkIdentity = actionId => {
  const normalized = String(actionId || '').toLowerCase()
  if (!validAssistantForkActionId(normalized)) {
    const error = new Error('Invalid Assistant fork action identity')
    error.code = 'ASSISTANT_FORK_PROTOCOL_ERROR'
    throw error
  }
  return normalized
}
export const assistantFork = (sid, {
  actionId = createIdempotencyKey(), expectedMessages = null,
} = {}, options = {}) => {
  const identity = assistantForkIdentity(actionId)
  if (expectedMessages != null
      && (!Number.isSafeInteger(expectedMessages) || expectedMessages < 0)) {
    const error = new Error('Invalid Assistant fork source version')
    error.code = 'ASSISTANT_FORK_PROTOCOL_ERROR'
    throw error
  }
  return post(assistantForkPath(sid), {
    action_id: identity,
    ...(expectedMessages == null ? {} : { expected_messages: expectedMessages }),
  }, options)
}
export const assistantForkStatus = (sid, actionId, {
  expectedMessages = null, ...options
} = {}) => {
  if (expectedMessages != null
      && (!Number.isSafeInteger(expectedMessages) || expectedMessages < 0)) {
    const error = new Error('Invalid Assistant fork source version')
    error.code = 'ASSISTANT_FORK_PROTOCOL_ERROR'
    throw error
  }
  const query = expectedMessages == null ? '' : `?expected_messages=${expectedMessages}`
  return get(`${assistantForkPath(sid, assistantForkIdentity(actionId))}${query}`,
    { ...options, cache: 'no-store' })
}
// Streaming turn: POST and read the SSE stream, invoking callbacks for token/step/todos/done/error.
// Real token streaming of the final answer (Claude-Desktop feel). Returns the final result dict.
const incompleteAssistantStream = reason => {
  const error = new Error(`Assistant stream ended before a terminal event: ${reason}`)
  error.code = 'assistant_stream_incomplete'
  error.transient = true
  error.ambiguous = true
  error.submissionMayHaveSucceeded = true
  return error
}

const abortedAssistantStream = () => {
  const error = new Error('Assistant stream aborted')
  error.name = 'AbortError'
  return error
}

const ASSISTANT_LIVE_SHARE_ACK_MAX = 4096
const assistantLiveShareAckIds = value => {
  if (!Array.isArray(value) || value.length > ASSISTANT_LIVE_SHARE_ACK_MAX) {
    const error = new Error('Invalid Assistant live-share acknowledgement list')
    error.code = 'assistant_live_share_ack_invalid'
    throw error
  }
  const seen = new Set()
  const ids = []
  for (const id of value) {
    if (typeof id !== 'string' || !/^[0-9a-f]{32}$/.test(id) || seen.has(id)) {
      const error = new Error('Invalid Assistant live-share acknowledgement id')
      error.code = 'assistant_live_share_ack_invalid'
      throw error
    }
    seen.add(id)
    ids.push(id)
  }
  return ids
}

export async function assistantMessageStream(sid, instruction, mode, cbs = {}, signal, display = null,
  acknowledgedLiveShareIds = []) {
  const body = {
    instruction,
    mode,
    acknowledged_live_share_ids: assistantLiveShareAckIds(acknowledgedLiveShareIds),
  }
  if (display != null) body.display = display
  const r = await fetch(apiUrl(`/api/assistant/sessions/${encodeURIComponent(sid)}/message_stream`),
    { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
      // Recovery must send the persisted clean display even when it happens to equal `instruction`:
      // the explicit body is the exact durable-turn contract, not a newly composed send. The live-share
      // acknowledgement is always present (including an empty array) so legacy omission cannot bypass
      // the server's compare-before-stage privacy precondition.
      body: JSON.stringify(body), signal })
  if (!r.ok) { await _throw(r, 'message_stream'); return null }
  if (signal?.aborted) throw abortedAssistantStream()
  // A 2xx only proves that the turn may have been accepted. Without a readable stream there is no
  // terminal receipt, so the caller must reconcile the existing turn instead of inventing success.
  if (!r.body || typeof r.body.getReader !== 'function') {
    throw incompleteAssistantStream('no readable response body')
  }

  let result = null
  let terminal = false
  const parser = createEventStreamParser(({ type: ev, data }) => {
    if (terminal) return
    let parsed
    try { parsed = JSON.parse(data) } catch { parsed = data }
    if (ev === 'token') cbs.onToken?.(parsed)
    else if (ev === 'text') cbs.onText?.(parsed)
    else if (ev === 'step') cbs.onStep?.(parsed)
    else if (ev === 'todos') cbs.onTodos?.(parsed)
    else if (ev === 'error') {
      cbs.onError?.(parsed)
      result = { ok: false, error: parsed }
      terminal = true
    } else if (ev === 'done') {
      result = parsed
      cbs.onDone?.(parsed)
      terminal = true
    }
  })
  const reader = r.body.getReader()
  const dec = new TextDecoder()
  for (;;) {
    let chunk
    try { chunk = await reader.read() }
    catch (error) {
      if (signal?.aborted) throw abortedAssistantStream()
      throw error
    }
    const { done, value } = chunk
    if (done) break
    parser.push(dec.decode(value, { stream: true }))
    if (terminal) {
      try { await reader.cancel() } catch { /* the terminal receipt already owns the outcome */ }
      return result
    }
  }
  if (signal?.aborted) throw abortedAssistantStream()
  parser.push(dec.decode())
  parser.finish()
  if (!terminal) throw incompleteAssistantStream('connection closed')
  return result
}
// Bounded/redacted I/O projection for one observation, with explicit omission metadata.
export const spanDetail = (runId, spanId) =>
  get(runApiPath(runId, `/spans/${encodeURIComponent(spanId)}`))

// Linear, de-duplicated conversation view of a node's trace (request once per sub-loop, then each
// generation's delta interleaved with tool calls) — the readable alternative to the raw span tree.
export const nodeConversation = (runId, nid, options) =>
  get(runNodeApiPath(runId, nid, '/conversation'), options)

// Stop an in-flight assistant turn server-side (survives a page reload, unlike aborting the local
// stream). Also used to poll whether a turn is still running (reattach after switch/reload).
export const assistantCancel = (sid, options) =>
  post(`/api/assistant/sessions/${encodeURIComponent(sid)}/cancel`, {}, options)
export const assistantProgress = (sid) => get(`/api/assistant/progress?session=${encodeURIComponent(sid)}`)

export const assistantCommands = () => get('/api/assistant/commands')
export const assistantRevert = (change, options) => {
  // Both values are opaque receipt material. A legal POSIX path may contain leading/trailing spaces;
  // normalizing either field would turn an exact request into a different request.
  const path = typeof change?.abs_path === 'string' ? change.abs_path : ''
  const recoveryId = typeof change?.recovery_id === 'string' ? change.recovery_id : ''
  const exists = change?.recovery_postimage_exists
  const digest = change?.recovery_postimage_digest
  const mode = change?.recovery_postimage_mode
  if (!path || !recoveryId || typeof exists !== 'boolean'
      || (exists && (typeof digest !== 'string' || !/^[0-9a-f]{64}$/.test(digest)))
      || (exists && (!Number.isInteger(mode) || mode < 0 || mode > 0o7777))
      || (!exists && (digest != null || mode != null))) {
    const error = new Error('This file change has no exact recovery identity.')
    error.code = 'ASSISTANT_REVERT_IDENTITY_REQUIRED'
    throw error
  }
  return post('/api/assistant/revert', {
    path,
    recovery_id: recoveryId,
    expected_postimage: { exists, digest: exists ? digest : null, mode: exists ? mode : null },
  }, options)
}
// A share link is its own capability: the response carries the token-bearing URL, when it expires,
// and whether it follows the chat (`live`) or is frozen at the turns that existed when it was minted.
// The session id is NOT a share link — unshare revokes every link without touching the conversation.
export const assistantShare = (sid, live = false, options) =>
  post(`/api/assistant/sessions/${encodeURIComponent(sid)}/share`, { live }, options)
export const assistantUnshare = (sid, options) =>
  send(`/api/assistant/sessions/${encodeURIComponent(sid)}/share`, 'DELETE', null, options)
// Pending human-in-the-loop confirm requests for a session, and resolving one.
export const assistantPermissions = (sid = null, options = {}) => get(sid == null
  ? '/api/assistant/permissions'
  : `/api/assistant/permissions?session=${encodeURIComponent(sid)}`, options)
export const assistantResolve = (reqId, decision) =>
  post(`/api/assistant/permissions/${encodeURIComponent(reqId)}`, { decision })
export const attentionFeed = (limit = 200, cursor = null, options = {}) => get(
  `/api/attention?limit=${encodeURIComponent(limit)}`
  + (cursor == null ? '' : `&cursor=${encodeURIComponent(cursor)}`),
  options,
)
