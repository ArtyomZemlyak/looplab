// The pure half of the Assistant share-link CREATE-RECOVERY contract (doc 25 SC-10) — no React and
// no I/O beyond one storage object, so `node --test` drives every decision directly.
//
// What it is for: `POST /api/assistant/sessions/{sid}/share` publishes a bearer capability, and its
// response can be lost. Before this, the operator was told "Share uncertain · revoke before
// retrying" and a second click published a SECOND live link — an un-revoked bearer NOBODY holds,
// invisible in the copy surface and removable only by revoking the whole chat. The fix is that this
// browser owns the create identity: it mints a request id and a 256-bit secret, saves them BEFORE
// the request, and the server derives the link id and the bearer from them. A retry with the same
// saved envelope replays the original capability instead of minting another.
//
// The derivation is `capabilityRecovery.js` (the browser twin of `serve/capability_store.py`), so
// the two capability surfaces cannot drift apart. What lives here is only the share store's own
// shapes: its 32-hex id, its `<id>.<secret>` token, and what the client declares about a link.
import {
  REQUEST_ID_RE, TOKEN_SECRET_RE, asciiJsonString, assertRecoveryCrypto, createRecoveryIdentity,
  onlyKeys, recoveryBearer, recoveryDigest, recoveryError, safeText, storageTarget,
} from './capabilityRecovery.js'

const STORAGE_PREFIX = 'll.assistant-share-create.v1.'
const VERSION = 1
const CONTRACT = 1                       // matches `assistant.py::_SHARE_CREATE_CONTRACT`
const SHARE_ID_RE = /^[0-9a-f]{32}$/
const SHARE_TOKEN_RE = /^([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$/
const SESSION_ID_RE = /^[0-9a-f]{16}$/
const ENVELOPE_KEYS = new Set([
  'version', 'scope', 'sessionId', 'live', 'ttlSeconds', 'requestId', 'tokenSecret', 'updatedAt',
])
const TOMBSTONE_KEYS = new Set(['version', 'terminal'])
const TERMINAL_KINDS = new Set(['revoked', 'expired'])

const CRYPTO_UNAVAILABLE = 'ASSISTANT_SHARE_RECOVERY_CRYPTO_UNAVAILABLE'
const PROTOCOL_ERROR = 'ASSISTANT_SHARE_RECOVERY_PROTOCOL_ERROR'
const STORAGE_UNAVAILABLE = 'ASSISTANT_SHARE_RECOVERY_STORAGE_UNAVAILABLE'
const INVALID = 'ASSISTANT_SHARE_RECOVERY_INVALID'

const storageKey = (scope, sessionId) =>
  `${STORAGE_PREFIX}${encodeURIComponent(scope)}.${encodeURIComponent(sessionId)}`

export function assertShareRecoveryCrypto(source = globalThis.crypto) {
  return assertRecoveryCrypto(source, CRYPTO_UNAVAILABLE,
    'Secure browser cryptography is unavailable; no public-link request was sent.')
}

// `assistant.py::_share_recovery_identity`. The server takes the first 128 bits of the SHA-256 so
// the id keeps the exact 32-hex shape every share URL already speaks; the full hash is stored beside
// the record, which is what makes a truncated-prefix collision a conflict and never a replay.
export async function deriveShareLinkId(sessionId, requestId, source = globalThis.crypto) {
  if (!REQUEST_ID_RE.test(requestId || '') || !safeText(String(sessionId || ''), 255)) {
    throw recoveryError(PROTOCOL_ERROR, 'The saved public-link identity is malformed.')
  }
  assertShareRecoveryCrypto(source)
  const body = `{"request_id":${asciiJsonString(requestId)},`
    + `"session":${asciiJsonString(sessionId)},"v":${CONTRACT}}`
  const digest = await recoveryDigest('looplab-share-create-id-v1', body, source,
    CRYPTO_UNAVAILABLE, 'The saved public-link identity could not be verified.')
  return digest.slice(0, 32)
}

// `assistant.py::_share_recovery_token`. The bearer the server will publish the digest of — this
// browser can rebuild it from the secret it kept, and nobody else can rebuild it at all.
export async function deriveShareToken(shareId, tokenSecret, source = globalThis.crypto) {
  if (!SHARE_ID_RE.test(shareId || '')) {
    throw recoveryError(PROTOCOL_ERROR, 'The saved public-link id is malformed.')
  }
  assertShareRecoveryCrypto(source)
  const bearer = await recoveryBearer('looplab-share-bearer-v1', tokenSecret, shareId, source,
    CRYPTO_UNAVAILABLE, 'The saved public link could not be verified with browser cryptography.')
  if (bearer === null) {
    throw recoveryError(PROTOCOL_ERROR, 'The saved public-link secret is malformed.')
  }
  return `${shareId}.${bearer}`
}

const validateEnvelope = (value, scope, sessionId) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !onlyKeys(value, ENVELOPE_KEYS) || Object.keys(value).length !== ENVELOPE_KEYS.size
      || value.version !== VERSION || value.scope !== scope
      || value.sessionId !== String(sessionId)
      || !safeText(value.scope, 1024) || !SESSION_ID_RE.test(value.sessionId || '')
      || typeof value.live !== 'boolean'
      // null means "whatever the server's default is". Sending no `ttl_seconds` at all is what
      // keeps both attempts hashing to one create intent without this tab having to know the number.
      || (value.ttlSeconds !== null
        && (!Number.isSafeInteger(value.ttlSeconds) || value.ttlSeconds < 60
          || value.ttlSeconds > 90 * 24 * 60 * 60))
      || !REQUEST_ID_RE.test(value.requestId || '')
      || !TOKEN_SECRET_RE.test(value.tokenSecret || '')
      || !Number.isSafeInteger(value.updatedAt) || value.updatedAt < 1_577_836_800_000
      || value.updatedAt > Date.now() + 300_000) return null
  return value
}

export function readShareCreateIntent(scope, sessionId, storage = undefined) {
  const target = storageTarget(storage)
  if (!target) return { invalid: true, code: STORAGE_UNAVAILABLE }
  let raw
  try { raw = target.getItem(storageKey(scope, sessionId)) }
  catch { return { invalid: true, code: STORAGE_UNAVAILABLE } }
  if (raw == null) return null
  if (raw === '') return { invalid: true, code: INVALID }
  let value
  try { value = JSON.parse(raw) } catch { return { invalid: true, code: INVALID } }
  // A tombstone is what a storage that refuses `removeItem` leaves behind; it means "cleared".
  if (value && typeof value === 'object' && !Array.isArray(value)
      && onlyKeys(value, TOMBSTONE_KEYS) && Object.keys(value).length === TOMBSTONE_KEYS.size
      && value.version === VERSION && value.terminal === true) {
    try { target.removeItem(storageKey(scope, sessionId)) } catch { /* inert tombstone stays */ }
    return null
  }
  return validateEnvelope(value, scope, sessionId) || { invalid: true, code: INVALID }
}

const writeIntent = (intent, storage = undefined) => {
  const target = storageTarget(storage)
  const checked = validateEnvelope(intent, intent?.scope, intent?.sessionId)
  if (!target || !checked) throw recoveryError(STORAGE_UNAVAILABLE,
    'Public-link recovery could not be saved; no request was sent.')
  const raw = JSON.stringify(checked)
  try {
    target.setItem(storageKey(checked.scope, checked.sessionId), raw)
    // Read it back: a quota-exceeded or private-mode write can be silently dropped, and an identity
    // that is not durable is worse than none — it is exactly the second capability all over again.
    if (target.getItem(storageKey(checked.scope, checked.sessionId)) !== raw) {
      throw new Error('storage write was not durable')
    }
  } catch (cause) {
    throw recoveryError(STORAGE_UNAVAILABLE,
      'Public-link recovery could not be saved; no request was sent.', cause)
  }
  return checked
}

// Two different questions, deliberately not one predicate. The TERMS decide whether a saved
// envelope may be reused (that reuse is the recovery); the IDENTITY decides whether this exact
// envelope still owns the slot, which is what stops one tab clearing another's in-flight create.
const sameTerms = (left, right) => !!left && !left.invalid
  && left.scope === right.scope && left.sessionId === right.sessionId
  && left.live === right.live && left.ttlSeconds === right.ttlSeconds

const sameIdentity = (left, right) => sameTerms(left, right)
  && left.requestId === right.requestId && left.tokenSecret === right.tokenSecret

// The saved envelope is REUSED when one already exists for these exact terms: that reuse IS the
// recovery. A first click that never came back leaves the identity here, and the second click sends
// the same one, so the server replays instead of publishing a second link.
export function beginShareCreateIntent({
  scope, sessionId, live = false, ttlSeconds = null,
}, storage = undefined, cryptoSource = globalThis.crypto) {
  const existing = readShareCreateIntent(scope, sessionId, storage)
  if (existing?.invalid) throw recoveryError(existing.code,
    existing.code === STORAGE_UNAVAILABLE
      ? 'Session recovery storage is unavailable; no public-link request was sent.'
      : 'Saved public-link recovery data is unreadable. Resolve it before creating another link.')
  const wanted = {
    version: VERSION, scope, sessionId: String(sessionId), live, ttlSeconds,
    requestId: '', tokenSecret: '', updatedAt: 0,
  }
  if (existing && sameTerms(existing, wanted)) return { intent: existing, created: false }
  if (existing) {
    // Different terms under a saved identity would be refused by the server as a conflict, so the
    // stale envelope is discarded and a fresh identity is minted rather than sent to be rejected.
    discardShareCreateIntent(scope, sessionId, storage)
  }
  assertShareRecoveryCrypto(cryptoSource)
  const { requestId, tokenSecret } = createRecoveryIdentity(cryptoSource, CRYPTO_UNAVAILABLE,
    'Secure browser randomness failed; no public-link request was sent.')
  return {
    intent: writeIntent({ ...wanted, requestId, tokenSecret, updatedAt: Date.now() }, storage),
    created: true,
  }
}

const tombstone = (target, key) => {
  const raw = JSON.stringify({ version: VERSION, terminal: true })
  try {
    target.setItem(key, raw)
    return target.getItem(key) === raw
  } catch { return false }
}

export function discardShareCreateIntent(scope, sessionId, storage = undefined) {
  const target = storageTarget(storage)
  if (!target) return false
  const key = storageKey(scope, sessionId)
  try {
    target.removeItem(key)
    if (target.getItem(key) == null) return true
  } catch { /* tombstone below */ }
  return tombstone(target, key)
}

export function clearShareCreateIntent(intent, storage = undefined) {
  const target = storageTarget(storage)
  if (!target) return false
  const current = readShareCreateIntent(intent.scope, intent.sessionId, target)
  // Another tab's identity now owns this chat; clearing it would strand ITS in-flight create.
  if (!sameIdentity(current, intent)) return current == null
  return discardShareCreateIntent(intent.scope, intent.sessionId, target)
}

// Exactly the body the route's `_share_recovery_envelope` parses. `ttl_seconds` is omitted when the
// tab has no opinion: PRESENCE selects the recovery contract, and both attempts must send the same
// fields or they hash to different create intents.
export const shareCreateBody = intent => ({
  live: intent.live,
  request_id: intent.requestId,
  token_secret: intent.tokenSecret,
  ...(intent.ttlSeconds == null ? {} : { ttl_seconds: intent.ttlSeconds }),
})

// The receipt is checked against what this tab can DERIVE, not against what the server says about
// itself: the URL must carry the exact token our own secret rebuilds, over the exact id our own
// request identity derives. A server that answered with any other capability — a second link minted
// by a racing create, or a link for another chat — fails here instead of being copied to a clipboard.
export async function validateShareCreateReceipt(value, intent, cryptoSource = globalThis.crypto) {
  const url = typeof value?.url === 'string' ? value.url : ''
  const shareId = typeof value?.share_id === 'string' ? value.share_id : ''
  const expiresAt = Number(value?.expires_at)
  const expectedId = await deriveShareLinkId(intent.sessionId, intent.requestId, cryptoSource)
  const expectedToken = SHARE_ID_RE.test(shareId)
    ? await deriveShareToken(shareId, intent.tokenSecret, cryptoSource) : ''
  const match = SHARE_TOKEN_RE.exec(url.replace('#/assistant/shared/', ''))
  if (value?.ok !== true || String(value?.session || '') !== intent.sessionId
      || value?.live !== intent.live || typeof value?.replayed !== 'boolean'
      || shareId !== expectedId || !match || match[1] !== shareId
      || url !== `#/assistant/shared/${expectedToken}`
      || !Number.isFinite(expiresAt) || expiresAt <= 0) {
    throw recoveryError(PROTOCOL_ERROR,
      'The server returned a public link that did not match the saved request.')
  }
  return { shareId, relativeUrl: url, expiresAt, replayed: value.replayed }
}

// A 410 says the recovered link is already dead. It carries no token on purpose, so the only thing
// to validate is that it names OUR link — after which the saved identity is spent and a new one has
// to be minted. Anything malformed leaves the identity alone rather than releasing it on a guess.
export async function validateShareReplayTerminal(error, intent, cryptoSource = globalThis.crypto) {
  const detail = error?.detail
  const kind = typeof detail?.kind === 'string' ? detail.kind : ''
  const shareId = typeof detail?.share_id === 'string' ? detail.share_id : ''
  const expectedId = await deriveShareLinkId(intent.sessionId, intent.requestId, cryptoSource)
  const revokedAt = detail?.revoked_at
  // `revoked` must name WHEN, and `expired` must name no revocation at all: a receipt that says
  // both, or neither, is not a lifecycle this server writes.
  const revocationMatches = kind === 'revoked'
    ? (typeof revokedAt === 'number' && Number.isFinite(revokedAt) && revokedAt > 0)
    : revokedAt === null
  if (error?.status !== 410 || error?.code !== 'assistant_share_replay_terminal'
      || !TERMINAL_KINDS.has(kind) || shareId !== expectedId || !revocationMatches) {
    throw recoveryError(PROTOCOL_ERROR,
      'The server returned an invalid terminal public-link receipt.')
  }
  return { kind, shareId }
}

// A 4xx about the ENVELOPE is authoritative and spends the identity: retrying the same one can only
// be refused the same way. Every other failure — a timeout, a 5xx, a dropped connection — is exactly
// the case the saved envelope exists for, so it stays put and the next click recovers.
export const shareRecoverySpent = error => error?.status === 400 || error?.status === 409
  || error?.status === 410
