import assert from 'node:assert/strict'
import test from 'node:test'

import { getScopeReport } from '../src/api.js'

test('stored scope reports always bypass intermediary browser caches', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return { ok: true, json: async () => ({ exists: false }) }
  }
  try {
    const controller = new AbortController()
    await getScopeReport('task', 'opaque/id', { signal: controller.signal })
    assert.match(request.url, /\/api\/scope-report\/task\/opaque%2Fid$/)
    assert.equal(request.options.cache, 'no-store')
    assert.ok(request.options.signal)
    assert.equal(request.options.signal.aborted, false)
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})
