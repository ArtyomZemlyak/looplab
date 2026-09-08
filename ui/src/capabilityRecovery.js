// The browser half of the CREATE-RECOVERY contract, shared by the two bearer capabilities the UI
// mints: a review link (`reviewLinkRecovery.js`) and an Assistant share link
// (`assistantShareRecovery.js`). It is the twin of `looplab/serve/capability_store.py`'s derivation
// section — doc 25 SC-10 — and it exists for the same reason that one does.
//
// A create is not idempotent: the server has no way to recognise a second POST as the same request,
// so a response lost in flight leaves the client holding NO token while a live capability exists,
// and a plain retry publishes a SECOND one — an un-revoked bearer nobody holds. The fix moves the
// identity to the CLIENT: it mints a request id and a 256-bit secret, keeps them, and the server
// derives the link id from (subject, request id) and the bearer from an HMAC of that secret over
// the id. The retry lands on the same record and BOTH sides rebuild the same token.
//
// Which is why these bytes cannot be written twice. The server's digest and the browser's must
// agree exactly — a canonical JSON body, an ASCII label, one NUL, unpadded base64url — or the
// client validates a receipt against a token that authenticates nothing and reports a protocol
// error on a link that is actually fine. `test/assistantShareRecovery.test.js` pins them against
// vectors computed by the Python implementation, in both directions.

const BASE64_URL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
// 43 base64url characters carry 258 bits, so the last character may only spell a 6-bit group whose
// low two bits are zero. Accepting the others would admit two spellings of one 256-bit secret, and
// the server (which round-trips its own decode) refuses exactly those.
export const TOKEN_SECRET_RE = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/
export const REQUEST_ID_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/

export const recoveryError = (code, message, cause = null) => Object.assign(
  new Error(message, cause ? { cause } : undefined), { code },
)

export const encodeBase64Url = bytes => {
  let out = ''
  for (let offset = 0; offset < bytes.length; offset += 3) {
    const left = bytes[offset]
    const middle = offset + 1 < bytes.length ? bytes[offset + 1] : null
    const right = offset + 2 < bytes.length ? bytes[offset + 2] : null
    out += BASE64_URL[left >> 2]
    out += BASE64_URL[((left & 3) << 4) | (middle == null ? 0 : middle >> 4)]
    if (middle != null) {
      out += BASE64_URL[((middle & 15) << 2) | (right == null ? 0 : right >> 6)]
    }
    if (right != null) out += BASE64_URL[right & 63]
  }
  return out
}

export const decodeBase64Url = value => {
  if (!TOKEN_SECRET_RE.test(value || '')) return null
  const output = []
  let bits = 0
  let buffer = 0
  for (const character of value) {
    const index = BASE64_URL.indexOf(character)
    if (index < 0) return null
    buffer = (buffer << 6) | index
    bits += 6
    if (bits >= 8) {
      bits -= 8
      output.push((buffer >> bits) & 0xff)
      buffer &= (1 << bits) - 1
    }
  }
  return output.length === 32 && buffer === 0 ? new Uint8Array(output) : null
}

export const uuidFromBytes = bytes => {
  const copy = new Uint8Array(bytes)
  copy[6] = (copy[6] & 0x0f) | 0x40
  copy[8] = (copy[8] & 0x3f) | 0x80
  const hex = [...copy].map(value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

// The server hashes `json.dumps(..., ensure_ascii=True, separators=(',', ':'), sort_keys=True)`.
// `JSON.stringify` already writes compact separators; this escapes every non-ASCII code unit the
// way Python's `ensure_ascii` does, so a session id or run id outside ASCII still digests alike.
export const asciiJsonString = value => JSON.stringify(String(value)).replace(
  /[\u0080-\uffff]/g,
  character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`,
)

export function assertRecoveryCrypto(source, code, message) {
  if (!source || typeof source.getRandomValues !== 'function' || !source.subtle
      || typeof source.subtle.digest !== 'function' || typeof source.subtle.importKey !== 'function'
      || typeof source.subtle.sign !== 'function'
      || typeof TextEncoder === 'undefined') {
    throw recoveryError(code, message)
  }
  return source
}

// A create identity is only ever as good as its randomness, so both halves come from the CSPRNG and
// a failure REFUSES rather than falling back to anything weaker — an identity a second tab could
// also mint is an identity that cannot tell a retry from a different request.
export function createRecoveryIdentity(source, code, message) {
  const secret = new Uint8Array(32)
  try { source.getRandomValues(secret) } catch (cause) {
    throw recoveryError(code, message, cause)
  }
  let requestId = ''
  if (typeof source.randomUUID === 'function') {
    try { requestId = String(source.randomUUID()).toLowerCase() } catch { /* CSPRNG fallback below */ }
  }
  if (!REQUEST_ID_RE.test(requestId)) {
    const requestBytes = new Uint8Array(16)
    try { source.getRandomValues(requestBytes) } catch (cause) {
      throw recoveryError(code, message, cause)
    }
    requestId = uuidFromBytes(requestBytes)
  }
  const tokenSecret = encodeBase64Url(secret)
  if (!REQUEST_ID_RE.test(requestId) || !TOKEN_SECRET_RE.test(tokenSecret)) {
    throw recoveryError(code, message)
  }
  return { requestId, tokenSecret }
}

// `capability_store.recovery_digest`: SHA-256 over `<ascii label>` NUL `<canonical JSON>`. The caller
// passes the canonical body itself because each store's envelope has its own field names, and the
// ORDER of those fields is the sorted order the server writes — spelled out at each call site
// rather than sorted here, so a reader can see the exact bytes being hashed.
export async function recoveryDigest(label, canonicalBody, source, code, message) {
  try {
    const material = new TextEncoder().encode(`${label}\u0000${canonicalBody}`)
    const digest = new Uint8Array(await source.subtle.digest('SHA-256', material))
    return [...digest].map(value => value.toString(16).padStart(2, '0')).join('')
  } catch (cause) {
    if (cause?.code) throw cause
    throw recoveryError(code, message, cause)
  }
}

// `capability_store.recovery_bearer`: HMAC-SHA256(secret, `<ascii label>` NUL `<link id>`), base64url.
export async function recoveryBearer(label, tokenSecret, linkId, source, code, message) {
  const secret = decodeBase64Url(tokenSecret)
  if (!secret) return null
  try {
    const key = await source.subtle.importKey(
      'raw', secret, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
    )
    const material = new TextEncoder().encode(`${label}\u0000${linkId}`)
    return encodeBase64Url(new Uint8Array(await source.subtle.sign('HMAC', key, material)))
  } catch (cause) {
    throw recoveryError(code, message, cause)
  }
}

// sessionStorage, not localStorage: a recovery envelope names ONE tab's in-flight create. It must
// survive a reload (that is the whole point) and must not outlive the tab, because a stale identity
// from a closed session would be replayed onto a capability its owner already forgot about.
export const storageTarget = storage => {
  if (storage !== undefined) return storage
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage }
  catch { return null }
}

export const onlyKeys = (value, allowed) => Object.keys(value).every(key => allowed.has(key))
export const safeText = (value, max) => typeof value === 'string' && value.length > 0
  && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value)
