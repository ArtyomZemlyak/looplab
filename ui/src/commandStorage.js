// Durable command recovery in tab-scoped sessionStorage: the Dock/Assistant command envelopes, the
// shared per-run command lock, the new-run launch transports, and the report-refresh intent. Split
// out of api.js (doc 25 UI-02 — bodies verbatim); api.js re-exports everything, so importers are
// unchanged.
//
// Everything that decides WHAT MAY BE WRITTEN is here in one place: the key allow-lists, the stored
// status/code allow-lists, and the sanitizers that rebuild a record from client-owned metadata alone.
// No server free text, no payload, no credential and no raw `lastError` may reach storage — adding a
// field to one of the *_KEYS sets below is exactly what would change that answer, which is why they
// stay together rather than sitting beside the endpoint that happens to write them.

import {
  COMMAND_FAILED, COMMAND_ID_RE, COMMAND_PENDING, COMMAND_STATUSES, STORED_ERROR_CODES,
  STORED_ERROR_KEYS, UUID_V4_RE, commandEventForAction, createIdempotencyKey, hasOnlyKeys,
  safeIdentityText, validRunGeneration,
} from './commandModel.js'

const TRANSPORT_STORAGE_PREFIX = 'll.command-transport.'
const TRANSPORT_ACTIONS = new Set(['stop', 'finalize', 'resume'])
const ASSISTANT_TRANSPORT_STORAGE_PREFIX = 'll.assistant-command-transport.'
const ASSISTANT_TRANSPORT_ACTIONS = new Set(['stop', 'finalize', 'resume', 'pause', 'abort', 'ratify', 'approve'])
const RUN_COMMAND_LOCK_PREFIX = 'll.command-lock.'
const LAUNCH_TRANSPORT_PREFIX = 'll.launch-transport.'

const RUN_COMMAND_LOCK_EVENT = 'll:command-lock'
const LAUNCH_TRANSPORT_EVENT = 'll:launch-transport'

const STORED_COMMAND_STATUSES = new Set(['submitting', ...COMMAND_STATUSES])
const OBSERVATION_KINDS = new Set([null, 'transport', 'access', 'protocol', 'missing', 'request'])

const STORED_RECORD_KEYS = new Set(['id', 'status', 'event_type', 'error'])
// THE ENVELOPE'S OWN VERSION. Without it, this reader's only answer to a payload it does not fully
// recognise is `protocolInvalid` — and the shape that produces is a DEPLOY: ship a build that adds
// one envelope key, and a tab still running the previous build reads a perfectly valid in-flight
// command, fails `hasOnlyKeys`, and calls it a protocol violation. That is the outcome-unknown state
// the whole two-phase commit exists to avoid, manufactured by the client's own key set.
//
// A version distinguishes "corrupt" from "newer than me", which need opposite answers: corruption
// means trust nothing, while a newer protocol means the fields I DO understand — above all the
// command id — are still exactly what they claim, so the command can be re-checked rather than
// declared dead. Two sibling stores in this tree already version theirs
// (`attentionStorage.js::ATTENTION_STATE_VERSION`, `settingsSchema.js::SETTINGS_SCHEMA_VERSION`).
//
// MIGRATION: an envelope with no `v` is version 1 — the shape that shipped before this field — and
// is read exactly as before. That is the whole migration, because the field is additive; a future
// bump is what needs a step here, and this is the hook it will hang from.
export const COMMAND_ENVELOPE_VERSION = 1

const RUN_ENVELOPE_KEYS = new Set([
  'v',
  'runId', 'action', 'expectedGeneration', 'idempotencyKey', 'commandId', 'record', 'statusUnavailable',
  'observationKind', 'retrying', 'checking', 'updatedAt', 'committed',
])
const ASSISTANT_ENVELOPE_KEYS = new Set([...RUN_ENVELOPE_KEYS, 'arg', 'nodeGeneration'])
const LOCK_KEYS = new Set([
  'runId', 'source', 'action', 'expectedGeneration', 'idempotencyKey', 'commandId', 'status', 'statusUnavailable', 'updatedAt',
])
const LAUNCH_TRANSPORT_KEYS = new Set(['identity', 'runId', 'idempotencyKey', 'updatedAt'])

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
    v: COMMAND_ENVELOPE_VERSION,
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
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      return protocolTransport(runId, source)
    }
    // An absent `v` is version 1 (the shape before the field existed). A HIGHER one was written by a
    // newer build of this app, so its unfamiliar keys are expected rather than corruption — skip the
    // key check, keep what is still readable (the command id above all), and let the surface re-check
    // the command's status instead of calling a live command dead.
    const envelopeVersion = payload.v === undefined ? 1 : payload.v
    if (!Number.isSafeInteger(envelopeVersion) || envelopeVersion < 1) {
      return protocolTransport(runId, source, payload)
    }
    if (envelopeVersion > COMMAND_ENVELOPE_VERSION) {
      const transport = protocolTransport(runId, source, payload)
      // A NEWER PROTOCOL IS NOT A VIOLATION, and until this line it was answered as one.
      // `protocolTransport` hard-codes `protocolInvalid: true, canResubmit: false` — the
      // outcome-unknown state — so the newer-envelope branch returned exactly what the key check it
      // was added to skip already returned, and `protocolNewer` was a flag no surface reads. The
      // comment above states the intent: the fields this build understands are still what they
      // claim, so the command is RE-CHECKED rather than declared dead.
      //
      // Only with an id, because that is what makes a re-check possible: `Dock.jsx::
      // onCheckTransport` fetches `record.id` when the state is not `protocolInvalid`, and refuses
      // with the outcome-unknown toast when there is none. `canResubmit` stays FALSE either way —
      // re-checking a command by id is safe; REPLAYING an envelope this build does not fully
      // understand is not.
      return transport.commandId
        ? { ...transport, protocolInvalid: false, protocolNewer: true }
        : { ...transport, protocolNewer: true }
    }
    if (!hasOnlyKeys(payload, commandEnvelopeKeys(source))
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
