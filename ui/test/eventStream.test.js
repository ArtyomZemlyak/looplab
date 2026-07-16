import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clearOwnerToken, createEventStreamParser, fetchEventStream, setOwnerToken,
} from '../src/api.js'

const globals = () => ({
  fetch: globalThis.fetch,
  location: globalThis.location,
  sessionStorage: globalThis.sessionStorage,
})

const restore = previous => {
  for (const [name, value] of Object.entries(previous)) {
    if (value === undefined) delete globalThis[name]
    else globalThis[name] = value
  }
}

test('incremental event-stream parser preserves frame, id, retry, CRLF, and incomplete-tail semantics', () => {
  const events = []
  const parser = createEventStreamParser(event => events.push(event))
  parser.push(': keepalive\r\ni')
  parser.push('d: 41\r\nevent: state\r\ndata: {"part":')
  parser.push('1}\r\ndata: second\r\nretry: 1750\r\n\r\n')
  parser.push('event: done\ndata: {}\n\n')
  parser.push('id: 99\nevent: state\ndata: torn')
  const state = parser.finish()

  assert.deepEqual(events, [
    { type: 'state', data: '{"part":1}\nsecond', lastEventId: '41', retry: 1750 },
    { type: 'done', data: '{}', lastEventId: '41', retry: 1750 },
  ])
  assert.equal(state.lastEventId, '99')
  assert.equal(state.retry, 1750)
})

test('owner fetch-SSE uses proxy prefix, auth, Last-Event-ID, and caller abort signal', async () => {
  const previous = globals()
  const stored = new Map()
  const calls = []
  globalThis.location = { pathname: '/proxy/looplab/', hash: '' }
  globalThis.sessionStorage = {
    getItem: key => stored.get(key) || '',
    setItem: (key, value) => stored.set(key, value),
    removeItem: key => stored.delete(key),
  }
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options })
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('id: 42\nevent: state\ndata: {"ok":true}\n\n'))
        controller.close()
      },
    })
    return { ok: true, body }
  }
  try {
    setOwnerToken('owner-secret')
    const controller = new AbortController()
    const events = []
    const result = await fetchEventStream('/api/runs/demo/events', {
      signal: controller.signal,
      lastEventId: '41',
      onEvent: event => events.push(event),
    })
    assert.equal(calls[0].url, '/proxy/looplab/api/runs/demo/events')
    assert.equal(calls[0].options.headers['X-LoopLab-Token'], 'owner-secret')
    assert.equal(calls[0].options.headers['Last-Event-ID'], '41')
    assert.equal(calls[0].options.headers.Accept, 'text/event-stream')
    assert.equal(calls[0].options.signal, controller.signal)
    assert.equal(result.lastEventId, '42')
    assert.equal(events[0].type, 'state')
  } finally {
    clearOwnerToken()
    restore(previous)
  }
})

test('review fetch-SSE translates the run namespace and never falls back to owner auth', async () => {
  const previous = globals()
  const calls = []
  globalThis.location = { pathname: '/proxy/review', hash: '#/rv_review-token' }
  globalThis.sessionStorage = { getItem: () => 'stale-owner-secret' }
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options })
    return { ok: true, body: new ReadableStream({ start: controller => controller.close() }) }
  }
  try {
    await fetchEventStream('/api/runs/ignored/events')
    assert.equal(calls[0].url, '/proxy/api/review/events')
    assert.equal(calls[0].options.headers['X-LoopLab-Review'], 'rv_review-token')
    assert.equal(calls[0].options.headers['X-LoopLab-Token'], undefined)
  } finally {
    restore(previous)
  }
})
