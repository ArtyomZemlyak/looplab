import test from 'node:test'
import assert from 'node:assert/strict'
import {
  EVENT_STREAM_FRAME_TOO_LARGE, clearOwnerToken, createEventStreamParser, fetchEventStream,
  setOwnerToken,
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

// ---------------------------------------------------------------------------------------------
// THE FRAME BOUND IS THE CALLER'S (review 2026-09-22, UI-01). One 2 MiB constant bounded every
// stream, and the run stream's FIRST frame is the whole folded state: past ~468 toy nodes (real runs
// far earlier) the owner stream threw on every connect and reconnected forever. The bound is now a
// per-call option whose overflow is a TYPED error, the default stays 2 MiB for every other stream
// (the Assistant's), and the parser scans only new text for line breaks so a large bound does not
// buy a quadratic re-scan of the partial line on every network chunk.
// ---------------------------------------------------------------------------------------------

const frameOf = chars => `id: 7\nevent: state\ndata: ${JSON.stringify({ pad: 'x'.repeat(chars) })}\n\n`
const feed = (parser, text, chunk = 65536) => {
  for (let i = 0; i < text.length; i += chunk) parser.push(text.slice(i, i + chunk))
}

test('the frame bound is a per-call option and its overflow is a typed error', () => {
  const big = frameOf(3 * 1024 * 1024)
  // The default is unchanged for every stream that does not ask for more.
  assert.throws(() => feed(createEventStreamParser(() => {}), big),
    error => error.code === EVENT_STREAM_FRAME_TOO_LARGE && error.maxFrameChars === 2 * 1024 * 1024)
  const events = []
  const parser = createEventStreamParser(event => events.push(event), '',
    { maxFrameChars: 4 * 1024 * 1024 })
  feed(parser, big)
  assert.equal(events.length, 1, 'a 3 MiB frame under a 4 MiB bound is delivered whole')
  assert.equal(JSON.parse(events[0].data).pad.length, 3 * 1024 * 1024)
  assert.equal(events[0].lastEventId, '7')
  assert.throws(() => feed(createEventStreamParser(() => {}, '', { maxFrameChars: 1024 }), frameOf(2048)),
    error => error.code === EVENT_STREAM_FRAME_TOO_LARGE && error.maxFrameChars === 1024)
})

test('a network chunk carrying many small frames is not one oversized frame', () => {
  // The old push appended the WHOLE chunk before splitting lines and refused it when the chunk was
  // longer than the bound — a burst of small events was refused as if it were one big one.
  const events = []
  const parser = createEventStreamParser(event => events.push(event), '', { maxFrameChars: 4096 })
  const burst = Array.from({ length: 400 },
    (_, i) => `id: ${i}\nevent: state\ndata: {"i":${i}}\n\n`).join('')
  assert.ok(burst.length > 4096)
  parser.push(burst)
  assert.equal(events.length, 400)
  assert.equal(events.at(-1).lastEventId, '399')
})

test('a stream whose parser refuses a frame is cancelled, not left streaming into nothing', async () => {
  const previous = globals()
  let cancelled = false
  let pulls = 0
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} }
  globalThis.fetch = async () => ({
    ok: true,
    body: new ReadableStream({
      pull(controller) {
        pulls += 1
        controller.enqueue(new TextEncoder().encode(pulls === 1 ? 'event: state\ndata: ' : 'x'.repeat(1024)))
      },
      cancel() { cancelled = true },
    }),
  })
  try {
    await assert.rejects(
      fetchEventStream('/api/runs/demo/events', { onEvent: () => {}, maxFrameChars: 4096 }),
      error => error.code === EVENT_STREAM_FRAME_TOO_LARGE)
    assert.equal(cancelled, true, 'the reader must be cancelled when the parser throws')
    const pullsAtRefusal = pulls
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.equal(pulls, pullsAtRefusal, 'nothing reads the body after the refusal')
  } finally {
    restore(previous)
  }
})
