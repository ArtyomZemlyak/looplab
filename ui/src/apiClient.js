// The browser's HTTP boundary: owner/review credential selection, the proxy path prefix, the review
// read-namespace translation, the review read-only refusal, and the get/post/putText/send wrappers
// every endpoint function is built on. Split out of api.js (doc 25 UI-02 — bodies verbatim); api.js
// re-exports everything, so importers are unchanged.
//
// This is the security boundary for the browser. `_authHeaders` decides WHICH principal's credential
// travels, `reviewReadPath` decides which namespace a read addresses, and `assertNotReviewMutation`
// refuses a mutation before the request leaves. It is the module every other split module imports and
// it imports none of them: a helper two of them share is hoisted DOWN into here or into
// commandModel.js, never back up into api.js, because a barrel<->member cycle under the build's
// native ESM execution order is a load-time TDZ crash rather than a build error.

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

// Owner and reviewer are distinct principals. A review fragment wins even if this tab has stale
// owner state, so the read-only surface can never accidentally send both credentials.
export const _authHeaders = (base) => {
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
export async function _throw(r, path) {
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

export function assertNotReviewMutation(path) {
  if (!isReviewLocation()) return
  const error = new Error('This review link is read-only')
  error.code = 'REVIEW_READ_ONLY'
  error.path = path
  throw error
}

const readResponse = (path, { headers = {}, ...options } = {}) =>
  fetch(apiUrl(reviewReadPath(path)), {
    ...options, headers: _authHeaders(headers),
    ...(isReviewLocation() ? { cache: 'no-store' } : {}),
  })

export async function get(path, options = {}) {
  // Carry the UI token on reads too: most GETs don't need it, but the artifact routes (raw file
  // content) are token-gated server-side. _authHeaders is a no-op when no token is set (local), so
  // ordinary local use is unchanged.
  // Every review bearer addresses the same small URL namespace.  Force a cache bypass so a cached
  // 401/410 from a revoked capability can never poison a subsequently created link in this tab.
  const r = await readResponse(path, options || {})
  if (!r.ok) await _throw(r, path)
  return r.json()
}

const entityTag = value => /^W\/"llconv1-[0-9a-f]{64}"$/.test(value) ? value : null

// Conditional JSON transport for application-owned last-good snapshots. A 304 is deliberately a
// tagged result rather than flowing through generic get(): Fetch marks 304 as `ok === false`, and it
// has no JSON body. Callers may reuse `data` only from the exact scope that supplied `validator`.
// Owner/review path translation and credentials stay at this same boundary as every other GET.
export async function conditionalGet(path, validator, options = {}) {
  const { headers = {}, ...fetchOptions } = options || {}
  const sent = entityTag(validator)
  // `Headers` normalizes names case-insensitively and also accepts an existing Headers instance.
  // Re-materialize a plain object because `_authHeaders` deliberately spreads its base before it
  // chooses one credential; spreading a Headers instance would silently discard every entry.
  const requestHeaders = Object.fromEntries(new Headers(headers))
  delete requestHeaders['if-none-match']
  if (sent) requestHeaders['If-None-Match'] = sent
  const requestOptions = { ...fetchOptions, headers: requestHeaders }
  let r = await readResponse(path, requestOptions)
  // A bodyless response without a validator has nothing a caller can safely reuse. Retry once as an
  // unconditional reload; a second 304 is a protocol failure and follows the normal error path.
  if (r.status === 304 && !sent) {
    r = await readResponse(path, { ...requestOptions, cache: 'no-store' })
  }
  const etag = entityTag(r.headers?.get?.('ETag'))
  if (r.status === 304) {
    if (!sent) await _throw(r, path)
    return { unchanged: true, etag, data: null }
  }
  if (!r.ok) await _throw(r, path)
  return { unchanged: false, etag, data: await r.json() }
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

export async function send(path, method, body, { signal } = {}) {
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
