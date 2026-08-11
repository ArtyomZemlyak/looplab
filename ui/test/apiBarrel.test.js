// api.js is a BARREL: the fetch client, the command model, the sessionStorage command envelopes, the
// durable command protocol, the event-stream transport and the paid scope-report saga live in their
// own modules (doc 25 UI-02) and are re-exported from it. Three things silently break that
// arrangement, and none of them shows up as a red test anywhere else:
//
//   1. A name is dropped from one of the explicit re-export lists while its code moves. Nothing
//      references those lists, so a missing name is not a build error anywhere — the importing module
//      just binds `undefined`, and the failure surfaces the first time an operator clicks the control
//      that calls it. `export *` would paper over that, and buy a worse problem: two modules exporting
//      one name makes it AMBIGUOUS, which ESM resolves by silently omitting it.
//   2. A member imports the barrel back. Under the build's native ESM execution ordering a
//      barrel<->member cycle is a load-time TDZ crash, not a build error.
//   3. Some other module imports an extracted file directly. The one deliberate exception is the
//      two-name reviewRouteApi facade: App needs the review credential/read boundary before a run
//      route exists, and importing the aggregate barrel there made every command/storage/SSE member
//      eager. All other consumers still go through the barrel, so this is one named route boundary
//      rather than an invitation to reach through it ad hoc.
//
// So the expected export set is DERIVED from the real call sites rather than hand-written here, and
// the credential check below is behavioural: the whole point of hoisting `_authHeaders` DOWN into
// apiClient.js is that every transport that left api.js still reaches the SAME one.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'

const SRC = new URL('../src/', import.meta.url)
const TEST = new URL('./', import.meta.url)

// The modules api.js re-exports. Adding another split module means adding it here.
const EXTRACTED = ['apiClient.js', 'commandModel.js', 'commandStorage.js', 'commandProtocol.js',
                   'eventStream.js', 'scopeReportActions.js']
const NARROW_FACADES = ['reviewRouteApi.js']

const readDirSource = async base => {
  const names = (await readdir(base)).filter(name => /\.(js|jsx)$/.test(name))
  const entries = await Promise.all(
    names.map(async name => [name, await readFile(new URL(name, base), 'utf8')]))
  return new Map(entries)
}

const importedNames = (text, specifier) => {
  const re = new RegExp(`import\\s*\\{([^}]*)\\}\\s*from\\s*'${specifier}'`, 'g')
  const out = []
  for (const match of text.matchAll(re)) {
    for (const part of match[1].split(',')) {
      const name = part.trim().split(/\s+as\s+/)[0].trim()
      if (name) out.push(name)
    }
  }
  return out
}

const REVIEW_TOKEN = 'rv_0123456789ab_abcdefghijklmnopqrstuvwxyzABCDEFG'
const OWNER_SECRET = 'owner-secret-that-must-not-cross-the-boundary'

const withReviewTab = async body => {
  const previous = {
    location: globalThis.location, sessionStorage: globalThis.sessionStorage,
    fetch: globalThis.fetch,
  }
  const calls = []
  globalThis.location = { pathname: '/user/a/proxy/8765/review', hash: `#/${REVIEW_TOKEN}` }
  globalThis.sessionStorage = { getItem: () => OWNER_SECRET }
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, headers: options.headers || {} })
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true, exists: false }),
      headers: { get: () => null },
      body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
    }
  }
  try { await body(calls) } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
}

test('every name a consumer takes from the barrel really resolves through it', async () => {
  const [src, tests] = await Promise.all([readDirSource(SRC), readDirSource(TEST)])
  const wanted = new Map()   // name -> the file that asks for it
  const sources = new Set()
  const want = (name, file) => { wanted.set(name, file); sources.add(file) }

  for (const [file, text] of src) {
    if (file === 'util.js') continue          // the outer barrel re-exports, it does not consume
    for (const name of importedNames(text, '\\./api\\.js')) want(name, file)
    for (const name of importedNames(text, '\\./util\\.js')) want(name, file)
  }
  for (const [file, text] of tests) {
    if (file === 'apiBarrel.test.js') continue
    for (const name of importedNames(text, '\\.\\./src/api\\.js')) want(name, file)
    for (const name of importedNames(text, '\\.\\./src/util\\.js')) want(name, file)
  }

  // Anti-vacuity: a derivation that quietly stopped finding call sites would make the loop below
  // pass over an empty set. These six names are literal on purpose — one per extracted module, each
  // of them a name this split MOVED — so a canary is not satisfied by whatever the scan happens to
  // find, and a shrinking derivation is a failure rather than a smaller green run.
  assert.ok(sources.size >= 20, `the scan found consumers in only ${sources.size} file(s)`)
  assert.ok(wanted.size >= 100, `the scan derived only ${wanted.size} barrel names`)
  for (const canary of ['apiPrefix', 'commandFeedback', 'saveRunTransport', 'runCommand',
                        'fetchEventStream', 'genScopeReport']) {
    assert.ok(wanted.has(canary), `${canary} is no longer derived from any call site`)
  }

  // Behavioural, not a source scan: load both barrels and look at what they actually give back.
  const [api, util] = await Promise.all([import('../src/api.js'), import('../src/util.js')])
  const missing = [...wanted.keys()].filter(name => api[name] === undefined && util[name] === undefined)
  assert.deepEqual(missing.sort(), [],
    `the barrel no longer exports ${missing.join(', ')} — a consumer binds undefined`)
  for (const [name, file] of wanted) {
    if (api[name] === undefined) continue     // format.js / layout.js / util's own helpers
    assert.notEqual(util[name], undefined,
      `${name} resolves through api.js but no longer through util.js, which ${file} reads`)
  }
})

test('one credential decision serves every transport that left api.js', async () => {
  // apiClient.js owns `_authHeaders`. A review tab must send the capability from its fragment and
  // never the stale owner token beside it — through the plain client, through the SSE transport in
  // eventStream.js, and through commandProtocol.js's bounded command read (which is what
  // scopeReportActions.js reads a paid receipt with). A module that re-derived its own headers after
  // the split would leak the owner credential from exactly one of these three.
  const { get, fetchEventStream, getScopeReport, runApiPath } = await import('../src/api.js')
  await withReviewTab(async calls => {
    await get('/api/runs/demo/state')
    await fetchEventStream(runApiPath('demo', '/events'), { onEvent: () => {} })
    await getScopeReport('project', 'p1')
    assert.equal(calls.length, 3, 'each transport must have reached fetch exactly once')
    for (const call of calls) {
      assert.equal(call.headers['X-LoopLab-Review'], REVIEW_TOKEN,
        `${call.url} did not carry the review capability`)
      assert.equal(call.headers['X-LoopLab-Token'], undefined,
        `${call.url} carried the owner credential out of a review tab`)
    }
    // reviewReadPath is shared too: an owner run path becomes the capability namespace, and the
    // proxy prefix the page was served from is still prepended by the one apiUrl().
    assert.equal(calls[0].url, '/user/a/proxy/8765/api/review/state')
    assert.equal(calls[1].url, '/user/a/proxy/8765/api/review/events')
  })
})

test('conditional reads keep the review/auth boundary and make every 304 explicit', async () => {
  const { conditionalGet } = await import('../src/api.js')
  const previous = {
    location: globalThis.location, sessionStorage: globalThis.sessionStorage,
    fetch: globalThis.fetch,
  }
  const current = 'W/"llconv1-' + 'a'.repeat(64) + '"'
  const foreign = 'W/"llconv1-' + 'b'.repeat(64) + '"'
  const calls = []
  let responses = []
  globalThis.location = { pathname: '/user/a/proxy/8765/review', hash: `#/${REVIEW_TOKEN}` }
  globalThis.sessionStorage = { getItem: () => OWNER_SECRET }
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, ...options })
    return responses.shift()
  }
  const response = (status, etag, data, json = async () => data) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: name => name.toLowerCase() === 'etag' ? etag : null },
    json,
  })
  try {
    const controller = new AbortController()
    responses = [response(304, current, null,
      async () => { throw new Error('a 304 body must never be parsed') })]
    const unchanged = await conditionalGet('/api/runs/demo/nodes/7/conversation', current, {
      signal: controller.signal,
      // A caller cannot smuggle a differently-cased second validator into the exact-scope read.
      headers: { 'IF-NONE-MATCH': foreign, 'X-Probe': 'kept' },
    })
    assert.deepEqual(unchanged, { unchanged: true, etag: current, data: null })
    assert.equal(calls[0].url, '/user/a/proxy/8765/api/review/nodes/7/conversation')
    const sent = new Headers(calls[0].headers)
    assert.equal(sent.get('If-None-Match'), current)
    assert.equal(sent.get('X-Probe'), 'kept')
    assert.equal(sent.get('X-LoopLab-Review'), REVIEW_TOKEN)
    assert.equal(sent.get('X-LoopLab-Token'), null)
    assert.equal(calls[0].signal, controller.signal)

    // A mismatched/missing response tag is exposed to the exact-scope caller, never silently
    // replaced with the tag it sent. Conversation retries either anomaly unconditionally.
    responses = [response(304, foreign)]
    const mismatched = await conditionalGet('/api/runs/demo/nodes/7/conversation', current)
    assert.deepEqual(mismatched, { unchanged: true, etag: foreign, data: null })
    responses = [response(304, null), response(200, current, { fresh: true })]
    const recovered = await conditionalGet('/api/runs/demo/nodes/7/conversation', null)
    assert.deepEqual(recovered, { unchanged: false, etag: current, data: { fresh: true } })
    const retryHeaders = new Headers(calls.at(-1).headers)
    assert.equal(retryHeaders.get('If-None-Match'), null,
      'a first 304 without reusable state must retry without any validator')

    // AbortSignal reaches the underlying fetch unchanged; deadline/unmount aborts therefore reject
    // the conditional read just like every other Inspector transport.
    const aborter = new AbortController()
    globalThis.fetch = (_url, options = {}) => new Promise((resolve, reject) => {
      const abort = () => reject(new DOMException('aborted', 'AbortError'))
      if (options.signal?.aborted) abort()
      else options.signal?.addEventListener('abort', abort, { once: true })
    })
    const pending = conditionalGet('/api/runs/demo/nodes/7/conversation', current,
      { signal: aborter.signal })
    await Promise.resolve()
    aborter.abort()
    await assert.rejects(pending, error => error?.name === 'AbortError')
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
})

test('the review read-only refusal still fires before a command reaches the wire', async () => {
  const { submitRunCommand } = await import('../src/api.js')
  await withReviewTab(async calls => {
    await assert.rejects(
      submitRunCommand('demo', 'pause', {}, { expectedGeneration: 'a'.repeat(64) }),
      error => error.code === 'REVIEW_READ_ONLY')
    assert.equal(calls.length, 0, 'a blocked review mutation must not reach fetch')
  })
})

test('the extracted modules never import the barrel they are part of', async () => {
  const src = await readDirSource(SRC)
  for (const member of EXTRACTED) {
    const text = src.get(member)
    assert.ok(text, `${member} is missing`)
    assert.doesNotMatch(text, /from\s*'\.\/api\.js'/, `${member} imports the barrel it is part of`)
    assert.doesNotMatch(text, /from\s*'\.\/util\.js'/, `${member} imports the outer barrel`)
  }
})

test('only the barrel, its members, and the named initial facade import extracted modules', async () => {
  const src = await readDirSource(SRC)
  const allowed = new Set(['api.js', ...EXTRACTED, ...NARROW_FACADES])
  for (const member of EXTRACTED) {
    const importers = [...src]
      .filter(([file, text]) => file !== member && text.includes(`from './${member}'`))
      .map(([file]) => file)
      .sort()
    assert.ok(importers.includes('api.js'), `${member} is not re-exported through api.js`)
    const outside = importers.filter(file => !allowed.has(file))
    assert.deepEqual(outside, [],
      `${member} has undeclared public reachability roots: ${outside.join(', ')}`)
  }
  assert.match(src.get('App.jsx'), /from '\.\/reviewRouteApi\.js'/)
  assert.equal(src.get('reviewRouteApi.js').trim().split('\n').at(-1),
    "export { get, reviewTokenFromLocation } from './apiClient.js'",
    'the initial facade must not grow beyond the review gate')
})

test('the shared primitives are defined once, not re-derived per module', async () => {
  const src = await readDirSource(SRC)
  const owners = {
    'apiClient.js': ['_authHeaders', '_throw', 'apiPrefix', 'reviewReadPath'],
    'commandModel.js': ['UUID_V4_RE', 'COMMAND_ID_RE', 'validRunGeneration', 'hasOnlyKeys',
                        'safeIdentityText', 'COMMAND_REQUEST_TIMEOUT_MS'],
  }
  for (const [owner, names] of Object.entries(owners)) {
    for (const name of names) {
      assert.match(src.get(owner), new RegExp(`^export (?:const|async function|function) ${name}\\b`, 'm'),
        `${owner} must own ${name}`)
      // Negative pins stay substrings on purpose: what must not come back is a second DEFINITION,
      // and splitting a module is exactly when someone reaches for a local copy over an import.
      for (const consumer of ['api.js', ...EXTRACTED]) {
        if (consumer === owner) continue
        assert.doesNotMatch(src.get(consumer),
          new RegExp(`^(?:export )?(?:const|async function|function) ${name}\\b`, 'm'),
          `${consumer} re-derives ${name} instead of importing the shared one`)
      }
    }
  }
})
