import test from 'node:test'
import assert from 'node:assert/strict'

import { CONTROL } from '../src/api.js'
import { STORED_ERROR_CODES, commandErrorMessage, commandFeedback } from '../src/commandModel.js'

// A node_reset served as a DRAIN (doc 68 68.3b): `drain_only` rides the command BODY — how the reset
// is served, never a field of the event — and only when asked, so every other body is byte-for-byte
// what it was (`commandLifecycle.test.js` deep-equals those).
const GEN = 'a'.repeat(64)
const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => body, headers: { get: () => null },
})

const posted = async (action) => {
  const calls = []
  const previous = { location: globalThis.location, fetch: globalThis.fetch, sessionStorage: globalThis.sessionStorage }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = { getItem: () => null }
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/lifecycle') && options.method == null) {
      return jsonResponse({ schema: 1, seq: 0, event_count: 1, generation: GEN, engine_running: false })
    }
    calls.push({ url, options })
    return jsonResponse({ id: `cmd_${'d'.repeat(32)}`, status: 'succeeded', event_type: 'node_reset' })
  }
  try { await action() }
  finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
  const post = calls.find(call => call.options.method === 'POST')
  return JSON.parse(post.options.body)
}

test('a drain reset carries drain_only, and a plain one carries no such key', async () => {
  const drain = await posted(() => CONTROL.resetNode('demo', 3, 'eval', 1, { drainOnly: true }))
  assert.deepEqual(drain, {
    type: 'node_reset', data: { node_id: 3, generation: 1, from_stage: 'eval' },
    expected_generation: GEN, drain_only: true,
  })
  const plain = await posted(() => CONTROL.resetNode('demo', 3, 'eval', 1))
  assert.deepEqual(plain, {
    type: 'node_reset', data: { node_id: 3, generation: 1, from_stage: 'eval' },
    expected_generation: GEN,
  })
  const falsy = await posted(() => CONTROL.resetNode('demo', 3, 'eval', 1, { drainOnly: 'yes' }))
  assert.equal('drain_only' in falsy, false, 'only a real `true` asks for a drain')
})

test('the two drain refusals are stored codes, restored with their own remedy', () => {
  assert.ok(STORED_ERROR_CODES.has('drain_refused'))
  assert.ok(STORED_ERROR_CODES.has('drain_needs_stopped_run'))
  // The COPY, not only the membership (critic 2026-09-26): a restored record carries the code alone.
  const restored = code => commandErrorMessage({ status: 'failed', error: { code } })
  assert.match(restored('drain_refused'), /A drain would not drive this run/)
  assert.match(restored('drain_refused'), /when the reset was already recorded, resume the run/)
  assert.match(restored('drain_needs_stopped_run'), /An engine is already driving this run/)
})

test('a drain a running search served is not reported as the drain', () => {
  const labels = { success: 'Reset #3 from eval, then pause applied', superseded: 'Reset #3 applied — by a running search, not a drain' }
  assert.equal(commandFeedback({ status: 'succeeded' }, labels).message, labels.success)
  assert.equal(commandFeedback({ status: 'succeeded', drain_superseded: true }, labels).message,
    labels.superseded)
  assert.equal(commandFeedback({ status: 'succeeded', drain_superseded: true },
    { success: 'ok' }).message, 'ok', 'a caller without the label keeps its success copy')
})
