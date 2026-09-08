// The create-recovery DERIVATION and its storage/validation helpers live in
// `capabilityRecovery.js` since 2026-09-08: the Assistant share links needed the same protocol, and
// a second copy of these bytes would validate a receipt against a token that authenticates nothing
// (doc 25 SC-10, the browser twin of `serve/capability_store.py`). What stays here is the review
// link's own shapes and its route-query envelope.
import {
  REQUEST_ID_RE, TOKEN_SECRET_RE, asciiJsonString, assertRecoveryCrypto, createRecoveryIdentity,
  onlyKeys, recoveryBearer, recoveryDigest, recoveryError, safeText, storageTarget,
} from './capabilityRecovery.js'
import { encodeRunRouteState, parseRunRouteState } from './runRouteState.js'

const STORAGE_PREFIX = 'll.review-create.v1.'
const VERSION = 1
const GENERATION_RE = /^[0-9a-f]{64}$/
const LINK_ID_RE = /^rvl_[0-9a-f]{32}$/
const ANY_LINK_ID_RE = /^rvl_(?:[0-9a-f]{12}|[0-9a-f]{32})$/
const TOKEN_RE = /^rv_([0-9a-f]{32})_([A-Za-z0-9_-]{43})$/
const PHASES = new Set(['pending', 'confirmed', 'revoking', 'conflict'])
const TERMINAL_STATUSES = new Set(['stale', 'expired', 'revoked'])
const ENVELOPE_KEYS = new Set([
  'version', 'scope', 'runId', 'expectedGeneration', 'ttlSeconds', 'includeEvidence',
  'requestId', 'tokenSecret', 'routeQuery', 'phase', 'linkId', 'token', 'expiresAt', 'updatedAt',
])
const TOMBSTONE_KEYS = new Set(['version', 'terminal'])


export function assertReviewLinkCrypto(source = globalThis.crypto) {
  return assertRecoveryCrypto(source, 'REVIEW_RECOVERY_CRYPTO_UNAVAILABLE',
    'Secure browser cryptography is unavailable; no review-link request was sent.')
}

export async function deriveReviewLinkId(runId, requestId, source = globalThis.crypto) {
  if (!REQUEST_ID_RE.test(requestId || '') || !safeText(String(runId || ''), 255)) {
    throw recoveryError('REVIEW_RECOVERY_PROTOCOL_ERROR',
      'The saved review-link identity is malformed.')
  }
  assertReviewLinkCrypto(source)
  const identity = `{"request_id":${asciiJsonString(requestId)},"run_id":${asciiJsonString(runId)},"v":1}`
  const digest = await recoveryDigest('looplab-review-create-id-v1', identity, source,
    'REVIEW_RECOVERY_CRYPTO_UNAVAILABLE', 'The saved review-link identity could not be verified.')
  return `rvl_${digest.slice(0, 32)}`
}

export function createReviewLinkIdentity(source = globalThis.crypto) {
  assertReviewLinkCrypto(source)
  return createRecoveryIdentity(source, 'REVIEW_RECOVERY_CRYPTO_UNAVAILABLE',
    'Secure browser randomness failed; no review-link request was sent.')
}

export async function deriveReviewLinkToken(linkId, tokenSecret, source = globalThis.crypto) {
  if (!LINK_ID_RE.test(linkId || '') || !source?.subtle
      || typeof source.subtle.importKey !== 'function' || typeof source.subtle.sign !== 'function'
      || typeof TextEncoder === 'undefined') {
    throw recoveryError('REVIEW_RECOVERY_CRYPTO_UNAVAILABLE',
      'Secure browser cryptography is unavailable; the saved review link cannot be verified.')
  }
  const bearer = await recoveryBearer('looplab-review-bearer-v1', tokenSecret, linkId, source,
    'REVIEW_RECOVERY_CRYPTO_UNAVAILABLE',
    'The saved review link could not be verified with secure browser cryptography.')
  if (bearer === null) throw recoveryError('REVIEW_RECOVERY_PROTOCOL_ERROR',
    'The saved review-link secret is malformed.')
  return `rv_${linkId.slice(4)}_${bearer}`
}

export const reviewRecoveryScope = (origin, prefix = '') => `${String(origin || '')}${String(prefix || '')}`

const storageKey = (scope, runId) => `${STORAGE_PREFIX}${encodeURIComponent(scope)}.${encodeURIComponent(runId)}`

const canonicalRouteQuery = (query, generation) => {
  if (typeof query !== 'string' || query.length > 2048 || /[\u0000-\u001f\u007f]/.test(query)) return false
  const parsed = parseRunRouteState(`#/?${query}`, { reviewMode: true })
  if (parsed.issues.length || parsed.state.generation !== generation) return false
  return encodeRunRouteState(parsed.state, { reviewMode: true, forceGeneration: true }) === query
}

const validateEnvelope = (value, scope, runId) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !onlyKeys(value, ENVELOPE_KEYS) || Object.keys(value).length !== ENVELOPE_KEYS.size
      || value.version !== VERSION || value.scope !== scope || value.runId !== String(runId)
      || !safeText(value.scope, 1024) || !safeText(value.runId, 255)
      || !GENERATION_RE.test(value.expectedGeneration || '')
      || !Number.isSafeInteger(value.ttlSeconds) || value.ttlSeconds < 300
      || value.ttlSeconds > 30 * 24 * 60 * 60
      || typeof value.includeEvidence !== 'boolean'
      || !REQUEST_ID_RE.test(value.requestId || '')
      || !TOKEN_SECRET_RE.test(value.tokenSecret || '')
      || !canonicalRouteQuery(value.routeQuery, value.expectedGeneration)
      || !PHASES.has(value.phase)
      || !Number.isSafeInteger(value.updatedAt) || value.updatedAt < 1_577_836_800_000
      || value.updatedAt > Date.now() + 300_000) return null
  const linked = value.phase === 'confirmed' || value.phase === 'revoking'
  const conflicted = value.phase === 'conflict'
  if (linked && (!LINK_ID_RE.test(value.linkId || '')
      || !TOKEN_RE.test(value.token || '')
      || value.token.slice(3, 35) !== value.linkId.slice(4)
      || !Number.isFinite(value.expiresAt) || value.expiresAt <= 0)) return null
  if (conflicted && (!LINK_ID_RE.test(value.linkId || '')
      || value.token !== null || value.expiresAt !== null)) return null
  if (value.phase === 'pending'
      && (value.linkId !== null || value.token !== null || value.expiresAt !== null)) return null
  return value
}

export function readReviewCreateIntent(scope, runId, storage = undefined) {
  const target = storageTarget(storage)
  if (!target) return { invalid: true, code: 'REVIEW_RECOVERY_STORAGE_UNAVAILABLE' }
  let raw
  try { raw = target.getItem(storageKey(scope, runId)) }
  catch { return { invalid: true, code: 'REVIEW_RECOVERY_STORAGE_UNAVAILABLE' } }
  if (raw == null) return null
  if (raw === '') return { invalid: true, code: 'REVIEW_RECOVERY_INVALID' }
  let value
  try { value = JSON.parse(raw) } catch { return { invalid: true, code: 'REVIEW_RECOVERY_INVALID' } }
  if (value && typeof value === 'object' && !Array.isArray(value)
      && onlyKeys(value, TOMBSTONE_KEYS) && Object.keys(value).length === TOMBSTONE_KEYS.size
      && value.version === VERSION && value.terminal === true) {
    try { target.removeItem(storageKey(scope, runId)) } catch { /* inert tombstone stays */ }
    return null
  }
  return validateEnvelope(value, scope, runId)
    || { invalid: true, code: 'REVIEW_RECOVERY_INVALID' }
}

const writeIntent = (intent, storage = undefined) => {
  const target = storageTarget(storage)
  const checked = validateEnvelope(intent, intent?.scope, intent?.runId)
  if (!target || !checked) throw recoveryError('REVIEW_RECOVERY_STORAGE_UNAVAILABLE',
    'Review-link recovery could not be saved; no request was sent.')
  const raw = JSON.stringify(checked)
  try {
    target.setItem(storageKey(checked.scope, checked.runId), raw)
    if (target.getItem(storageKey(checked.scope, checked.runId)) !== raw) throw new Error('storage write was not durable')
  } catch (cause) {
    throw recoveryError('REVIEW_RECOVERY_STORAGE_UNAVAILABLE',
      'Review-link recovery could not be saved; no request was sent.', cause)
  }
  return checked
}

const sameIdentity = (left, right) => !!left && !left.invalid
  && left.scope === right.scope && left.runId === right.runId
  && left.requestId === right.requestId && left.tokenSecret === right.tokenSecret
  && left.expectedGeneration === right.expectedGeneration
  && left.ttlSeconds === right.ttlSeconds && left.includeEvidence === right.includeEvidence
  && left.routeQuery === right.routeQuery

export function beginReviewCreateIntent({
  scope, runId, expectedGeneration, ttlSeconds, includeEvidence, routeQuery,
}, storage = undefined, cryptoSource = globalThis.crypto) {
  const existing = readReviewCreateIntent(scope, runId, storage)
  if (existing?.invalid) throw recoveryError(existing.code,
    existing.code === 'REVIEW_RECOVERY_STORAGE_UNAVAILABLE'
      ? 'Session recovery storage is unavailable; no review-link request was sent.'
      : 'Saved review-link recovery data is unreadable. Resolve it before creating another link.')
  if (existing) return { intent: existing, created: false }
  const { requestId, tokenSecret } = createReviewLinkIdentity(cryptoSource)
  const intent = {
    version: VERSION, scope, runId: String(runId), expectedGeneration,
    ttlSeconds, includeEvidence, requestId, tokenSecret, routeQuery,
    phase: 'pending', linkId: null, token: null, expiresAt: null, updatedAt: Date.now(),
  }
  return { intent: writeIntent(intent, storage), created: true }
}

export function transitionReviewCreateIntent(intent, {
  phase, linkId = null, token = null, expiresAt = null,
}, storage = undefined) {
  const current = readReviewCreateIntent(intent.scope, intent.runId, storage)
  if (!sameIdentity(current, intent)) throw recoveryError('REVIEW_RECOVERY_IDENTITY_CHANGED',
    'A different review-link recovery now owns this run.')
  return writeIntent({
    ...current, phase, linkId, token, expiresAt, updatedAt: Date.now(),
  }, storage)
}

const tombstone = (target, key) => {
  const raw = JSON.stringify({ version: VERSION, terminal: true })
  try {
    target.setItem(key, raw)
    return target.getItem(key) === raw
  } catch { return false }
}

export function clearReviewCreateIntent(intent, storage = undefined) {
  const target = storageTarget(storage)
  if (!target) return false
  const current = readReviewCreateIntent(intent.scope, intent.runId, target)
  if (!sameIdentity(current, intent)) return current == null
  const key = storageKey(intent.scope, intent.runId)
  try {
    target.removeItem(key)
    if (target.getItem(key) == null) return true
  } catch { /* tombstone below */ }
  return tombstone(target, key)
}

export function discardInvalidReviewCreateIntent(scope, runId, storage = undefined) {
  const target = storageTarget(storage)
  if (!target) return false
  const key = storageKey(scope, runId)
  try {
    target.removeItem(key)
    if (target.getItem(key) == null) return true
  } catch { /* tombstone below */ }
  return tombstone(target, key)
}

export const reviewCreateBody = intent => ({
  ttl_seconds: intent.ttlSeconds,
  include_evidence: intent.includeEvidence,
  expected_generation: intent.expectedGeneration,
  request_id: intent.requestId,
  token_secret: intent.tokenSecret,
})

export async function validateReviewCreateReceipt(value, intent, cryptoSource = globalThis.crypto) {
  const status = typeof value?.status === 'string' ? value.status : ''
  const token = typeof value?.token === 'string' ? value.token : ''
  const match = token.match(TOKEN_RE)
  const linkId = typeof value?.id === 'string' ? value.id : ''
  const scopes = Array.isArray(value?.scopes) ? value.scopes : []
  const expectedScopes = intent.includeEvidence ? ['summary', 'evidence'] : ['summary']
  const scopesMatch = scopes.length === expectedScopes.length
    && expectedScopes.every(scope => scopes.includes(scope))
  const createdAt = Number(value?.created_at)
  const expiresAt = Number(value?.expires_at)
  const ttlMatches = Math.abs((expiresAt - createdAt) - intent.ttlSeconds) <= 0.01
  const expectedLinkId = await deriveReviewLinkId(intent.runId, intent.requestId, cryptoSource)
  const expectedToken = LINK_ID_RE.test(linkId)
    ? await deriveReviewLinkToken(linkId, intent.tokenSecret, cryptoSource) : ''
  if (value?.ok !== true || !['active', ...TERMINAL_STATUSES].includes(status)
      || !match || linkId !== expectedLinkId || token !== expectedToken || !LINK_ID_RE.test(linkId)
      || linkId !== `rvl_${match[1]}` || value.run_id !== intent.runId
      || value.generation !== intent.expectedGeneration || !scopesMatch
      || !Number.isFinite(createdAt) || createdAt <= 0
      || createdAt > Date.now() / 1000 + 300
      || !Number.isFinite(expiresAt) || expiresAt <= createdAt
      || !ttlMatches
      || typeof value.replayed !== 'boolean'
      || (value.path != null && value.path !== `review#/${token}`)
      || (status === 'active' && value.revoked_at != null)
      || (status === 'revoked' && !Number.isFinite(Number(value.revoked_at)))) {
    throw recoveryError('REVIEW_RECOVERY_PROTOCOL_ERROR',
      'The server returned a review link that did not match the saved request.')
  }
  return { ...value, status, token, id: linkId, createdAt, expiresAt }
}

export async function validateReviewReplayTerminal(error, intent, cryptoSource = globalThis.crypto) {
  const detail = error?.detail
  const kind = typeof detail?.kind === 'string' ? detail.kind : ''
  const linkId = typeof detail?.existing_link_id === 'string' ? detail.existing_link_id : ''
  const expiresAt = Number(detail?.expires_at)
  const revokedAt = detail?.revoked_at
  const expectedLinkId = await deriveReviewLinkId(intent.runId, intent.requestId, cryptoSource)
  const knownExpiryMatches = intent.expiresAt == null
    || Math.abs(expiresAt - intent.expiresAt) <= 0.01
  const revocationMatches = kind === 'revoked'
    ? Number.isFinite(Number(revokedAt)) && Number(revokedAt) > 0
    : revokedAt === null
  const expiredAtMatches = kind !== 'expired' || expiresAt <= Date.now() / 1000 + 300
  if (error?.status !== 410 || error?.code !== 'review_replay_terminal'
      || !TERMINAL_STATUSES.has(kind) || linkId !== expectedLinkId
      || detail?.generation !== intent.expectedGeneration
      || !Number.isFinite(expiresAt) || expiresAt <= 0 || !knownExpiryMatches
      || !revocationMatches || !expiredAtMatches) {
    throw recoveryError('REVIEW_RECOVERY_PROTOCOL_ERROR',
      'The server returned an invalid terminal review-link receipt.')
  }
  return { kind, linkId, expiresAt, revokedAt }
}

export function reviewUrlForIntent(intent, { origin, prefix = '' } = {}) {
  const match = typeof intent?.token === 'string' ? intent.token.match(TOKEN_RE) : null
  if (!LINK_ID_RE.test(intent?.linkId || '') || !match
      || `rvl_${match[1]}` !== intent.linkId) return ''
  const token = intent.token
  const base = `${String(origin || '')}${String(prefix || '')}/`
  const target = new URL(`review#/${token}`, base)
  target.hash = `#/${token}${intent.routeQuery ? `?${intent.routeQuery}` : ''}`
  return target.href
}

export async function validateStoredReviewCreateIntent(intent, cryptoSource = globalThis.crypto) {
  if (!intent || intent.phase === 'pending') return intent
  const expectedLinkId = await deriveReviewLinkId(intent.runId, intent.requestId, cryptoSource)
  if (intent.linkId !== expectedLinkId) throw recoveryError('REVIEW_RECOVERY_INVALID',
    'Saved review-link recovery no longer matches its request identity.')
  if (intent.phase === 'conflict') return intent
  const expected = await deriveReviewLinkToken(intent.linkId, intent.tokenSecret, cryptoSource)
  if (intent.token !== expected) throw recoveryError('REVIEW_RECOVERY_INVALID',
    'Saved review-link recovery no longer matches its cryptographic identity.')
  return intent
}

export const reviewRecoveryLinkId = value => ANY_LINK_ID_RE.test(String(value || '')) ? String(value) : null
export const reviewRecoveryGenerationValid = value => GENERATION_RE.test(String(value || ''))
export const reviewRecoveryTerminalStatus = value => TERMINAL_STATUSES.has(value)
