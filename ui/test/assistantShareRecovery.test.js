// The Assistant share link's CREATE-RECOVERY contract, browser side (doc 25 SC-10).
//
// Two things are driven here and neither is a source pin:
//
//   1. The DERIVATION agrees with the server, byte for byte. The vectors below were produced by
//      `looplab/serve/assistant.py::_share_recovery_identity` / `_share_recovery_token` and by
//      `reviews.py`'s pair, against a fixed secret and request id. If either side ever computes one
//      byte differently, the client validates a real receipt against a token that authenticates
//      nothing — a healthy link reported as a protocol error, or worse, a dead one copied out.
//   2. A lost response is RECOVERABLE: the saved envelope survives, the second attempt sends the
//      identical identity, and the receipt it validates is the original capability.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  beginShareCreateIntent, clearShareCreateIntent, deriveShareLinkId, deriveShareToken,
  discardShareCreateIntent, readShareCreateIntent, shareCreateBody, shareRecoverySpent,
  validateShareCreateReceipt, validateShareReplayTerminal,
} from '../src/assistantShareRecovery.js'
import { deriveReviewLinkId, deriveReviewLinkToken } from '../src/reviewLinkRecovery.js'

// Produced by the Python implementation; see the header. The secret is bytes 0..31 so it is
// reproducible by hand on either side.
const VECTORS = {
  tokenSecret: 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8',
  requestId: '3f2504e0-4f89-41d3-9a0c-0305e82c3301',
  sessionId: 'a1b2c3d4e5f60718',
  runId: 'demo-run-ünïcode',
  shareId: 'c22d0bf1d15470a0d3209b0ebd938c3f',
  shareToken: 'c22d0bf1d15470a0d3209b0ebd938c3f.aON3KBkThCAXRk6pjq75cP1gbTR2r9H0kR9H6W5BPok',
  reviewLinkId: 'rvl_1a2bbc65e25769e227cdabfa01088662',
  reviewToken: 'rv_1a2bbc65e25769e227cdabfa01088662_A9Nyd7FHzbAwxL6qv0U8aC-EhlLntDeg0ax1bf2K2Es',
}

const memoryStorage = () => {
  const map = new Map()
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)) },
    removeItem: key => { map.delete(key) },
    size: () => map.size,
  }
}

const SCOPE = 'https://looplab.example/ui'
const begin = (storage, overrides = {}) => beginShareCreateIntent(
  { scope: SCOPE, sessionId: VECTORS.sessionId, live: false, ttlSeconds: null, ...overrides },
  storage,
)

// A server that answers exactly as `routers/assistant.py::_share_replay_payload` does.
const serverReceipt = async (intent, { replayed = false, expiresAt = 1789482017.59 } = {}) => {
  const shareId = await deriveShareLinkId(intent.sessionId, intent.requestId)
  const token = await deriveShareToken(shareId, intent.tokenSecret)
  return {
    ok: true,
    url: `#/assistant/shared/${token}`,
    session: intent.sessionId,
    share_id: shareId,
    expires_at: expiresAt,
    live: intent.live,
    replayed,
  }
}

test('the share derivation agrees with the server byte for byte', async () => {
  assert.equal(await deriveShareLinkId(VECTORS.sessionId, VECTORS.requestId), VECTORS.shareId)
  assert.equal(await deriveShareToken(VECTORS.shareId, VECTORS.tokenSecret), VECTORS.shareToken)
  // The subject is INSIDE the derived id, so one saved envelope can never be replayed onto another
  // chat — the same property the server states by hashing the session into the identity.
  assert.notEqual(await deriveShareLinkId('b1b2c3d4e5f60718', VECTORS.requestId), VECTORS.shareId)
})

test('the review derivation still agrees after both moved onto one implementation', async () => {
  // The extraction, driven from the other side: `reviewLinkRecovery.js` now computes through the
  // same primitives, and these vectors are the ones the review server produces.
  assert.equal(await deriveReviewLinkId(VECTORS.runId, VECTORS.requestId), VECTORS.reviewLinkId)
  assert.equal(await deriveReviewLinkToken(VECTORS.reviewLinkId, VECTORS.tokenSecret),
    VECTORS.reviewToken)
  // Non-ASCII in the subject is the case `ensure_ascii` decides: Python escapes it before hashing
  // and so must the browser, or a run id with an umlaut derives two different link ids.
  assert.notEqual(await deriveReviewLinkId('demo-run-uncode', VECTORS.requestId),
    VECTORS.reviewLinkId)
})

test('the two capabilities never derive the same bearer from one secret', async () => {
  // Domain separation, driven: the labels are what stop a digest lifted from one surface being
  // replayed as the other's.
  assert.notEqual(await deriveShareToken(VECTORS.shareId, VECTORS.tokenSecret),
    await deriveReviewLinkToken(`rvl_${VECTORS.shareId}`, VECTORS.tokenSecret))
})

test('a lost response is recovered by the identical saved envelope', async () => {
  const storage = memoryStorage()
  const first = begin(storage)
  assert.equal(first.created, true)
  const body = shareCreateBody(first.intent)
  // The response to THIS request never arrives. Nothing about that touches storage.
  const second = begin(storage)
  assert.equal(second.created, false, 'a retry minted a second create identity')
  assert.deepEqual(shareCreateBody(second.intent), body)
  assert.equal(body.ttl_seconds, undefined, 'an unopinionated tab must send no ttl_seconds')

  const receipt = await validateShareCreateReceipt(
    await serverReceipt(second.intent, { replayed: true }), second.intent)
  assert.equal(receipt.replayed, true)
  assert.equal(receipt.shareId, await deriveShareLinkId(VECTORS.sessionId, first.intent.requestId))
  assert.equal(clearShareCreateIntent(second.intent, storage), true)
  assert.equal(readShareCreateIntent(SCOPE, VECTORS.sessionId, storage), null)
})

test('changed terms mint a fresh identity instead of a request the server will refuse', async () => {
  const storage = memoryStorage()
  const frozen = begin(storage)
  const followsChat = begin(storage, { live: true })
  assert.equal(followsChat.created, true)
  assert.notEqual(followsChat.intent.requestId, frozen.intent.requestId)
  assert.equal(shareCreateBody(followsChat.intent).live, true)
  // And the replacement is what a later retry now recovers — one identity per set of terms.
  assert.equal(begin(storage, { live: true }).intent.requestId, followsChat.intent.requestId)
})

test('a receipt that is not this envelope’s own capability is refused', async () => {
  const storage = memoryStorage()
  const { intent } = begin(storage)
  const good = await serverReceipt(intent)
  const other = await serverReceipt(begin(memoryStorage(), { live: true }).intent)
  const cases = [
    { ...good, url: other.url },                                  // a different capability's token
    { ...good, share_id: other.share_id },                        // a different link id
    { ...good, session: 'b1b2c3d4e5f60718' },                     // another chat
    { ...good, live: true },                                      // terms we never asked for
    { ...good, ok: false },
    { ...good, expires_at: 0 },
    { ...good, replayed: 'yes' },
    { ...good, url: `#/assistant/shared/${VECTORS.shareToken}` },  // a token from another secret
  ]
  for (const value of cases) {
    await assert.rejects(() => validateShareCreateReceipt(value, intent),
      error => error.code === 'ASSISTANT_SHARE_RECOVERY_PROTOCOL_ERROR',
      JSON.stringify(value))
  }
  assert.deepEqual(await validateShareCreateReceipt(good, intent), {
    shareId: good.share_id, relativeUrl: good.url, expiresAt: good.expires_at, replayed: false,
  })
})

test('a terminal 410 is accepted only when it names this envelope’s own link', async () => {
  const storage = memoryStorage()
  const { intent } = begin(storage)
  const shareId = await deriveShareLinkId(intent.sessionId, intent.requestId)
  const dead = {
    status: 410,
    code: 'assistant_share_replay_terminal',
    detail: { kind: 'revoked', share_id: shareId, expires_at: 1789482017.59, revoked_at: 1788877277 },
  }
  assert.deepEqual(await validateShareReplayTerminal(dead, intent), { kind: 'revoked', shareId })
  const expired = { ...dead, detail: { ...dead.detail, kind: 'expired', revoked_at: null } }
  assert.deepEqual(await validateShareReplayTerminal(expired, intent), { kind: 'expired', shareId })
  for (const bad of [
    { ...dead, status: 409 },
    { ...dead, code: 'something_else' },
    { ...dead, detail: { ...dead.detail, kind: 'stale' } },
    { ...dead, detail: { ...dead.detail, share_id: VECTORS.shareId } },
    { ...dead, detail: { ...dead.detail, revoked_at: null } },     // "revoked" with no revocation
    { ...expired, detail: { ...expired.detail, revoked_at: 1788877277 } },
  ]) {
    await assert.rejects(() => validateShareReplayTerminal(bad, intent),
      error => error.code === 'ASSISTANT_SHARE_RECOVERY_PROTOCOL_ERROR', JSON.stringify(bad))
  }
})

test('only an authoritative refusal spends the saved identity', () => {
  // The asymmetry the whole contract rests on: a 4xx about the envelope can only be refused the
  // same way twice, but a timeout or a 5xx is exactly the case the envelope was saved for.
  for (const status of [400, 409, 410]) assert.equal(shareRecoverySpent({ status }), true)
  for (const status of [500, 502, 503, undefined]) assert.equal(shareRecoverySpent({ status }), false)
  assert.equal(shareRecoverySpent(null), false)
})

test('unreadable saved recovery is reported, never silently replaced', () => {
  const storage = memoryStorage()
  const key = `ll.assistant-share-create.v1.${encodeURIComponent(SCOPE)}.${VECTORS.sessionId}`
  storage.setItem(key, '{not json')
  assert.equal(readShareCreateIntent(SCOPE, VECTORS.sessionId, storage).code,
    'ASSISTANT_SHARE_RECOVERY_INVALID')
  assert.throws(() => begin(storage), error => error.code === 'ASSISTANT_SHARE_RECOVERY_INVALID')
  // Discarding it is the operator's explicit way out, and then a fresh identity may be minted.
  assert.equal(discardShareCreateIntent(SCOPE, VECTORS.sessionId, storage), true)
  assert.equal(begin(storage).created, true)

  // A field outside the envelope's exact key set is unreadable too — never merged over.
  const saved = JSON.parse(storage.getItem(key))
  storage.setItem(key, JSON.stringify({ ...saved, extra: 1 }))
  assert.equal(readShareCreateIntent(SCOPE, VECTORS.sessionId, storage).code,
    'ASSISTANT_SHARE_RECOVERY_INVALID')
})

test('a storage that cannot hold the identity refuses before any request is sent', () => {
  // A dropped write is the second-capability defect all over again: the request would go out with
  // an identity the retry can never reproduce. Refuse instead, and say nothing was sent.
  const dropping = { ...memoryStorage(), setItem: () => {} }
  assert.throws(() => begin(dropping),
    error => error.code === 'ASSISTANT_SHARE_RECOVERY_STORAGE_UNAVAILABLE'
      && /no request was sent/.test(error.message))
  const throwing = { getItem: () => { throw new Error('blocked') }, setItem: () => {}, removeItem: () => {} }
  assert.throws(() => begin(throwing),
    error => error.code === 'ASSISTANT_SHARE_RECOVERY_STORAGE_UNAVAILABLE')
})

test('another tab’s identity for this chat is never cleared by ours', () => {
  const storage = memoryStorage()
  const ours = begin(storage).intent
  discardShareCreateIntent(SCOPE, VECTORS.sessionId, storage)
  const theirs = begin(storage).intent
  assert.notEqual(theirs.requestId, ours.requestId)
  assert.equal(clearShareCreateIntent(ours, storage), false)
  assert.equal(readShareCreateIntent(SCOPE, VECTORS.sessionId, storage).requestId, theirs.requestId)
})
