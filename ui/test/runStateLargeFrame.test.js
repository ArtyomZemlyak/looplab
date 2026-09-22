// THE OWNER STREAM ON A LARGE RUN (review 2026-09-22, UI-01), driven through the real hook.
//
// The run stream's FIRST frame on every connection is the whole folded state, and the transport
// refused any frame over one hard-coded 2 MiB. Past it — ~468 toy nodes at the measured ~4.5 KB per
// node, far earlier on a real run — `useRunState` threw on the first frame of every connection and
// reconnected on its backoff ramp FOREVER: the workspace never went live and the server re-folded and
// re-encoded the whole state for every attempt. Two properties, each driven below:
//
//   * a first frame past the old 2 MiB bound connects (the run stream now asks for a state-sized
//     bound, `runStateModel.js::RUN_STATE_MAX_FRAME_CHARS`);
//   * a frame past even that bound DEGRADES the connection instead of looping: exactly one /events
//     request, the stream's reader given back, and the run then followed by the terminal machine's
//     small /lifecycle probe, re-reading /state (the uncapped GET) only when the run moved — with
//     `degraded` returned so the workspace can say so.
//
// Timers are compressed 100x (the reviewer's harness): the 1.5 s reconnect backoff becomes 15 ms, so
// a reconnect loop shows up as dozens of /events requests in the observation window, and the minute
// probe becomes 600 ms.
import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import { useRunState } from '../src/hooks.js'
import { RUN_STATE_MAX_FRAME_CHARS } from '../src/runStateModel.js'

const MIB = 1024 * 1024
const GENERATION = 'a'.repeat(64)
const snapshot = seq => ({
  generation: GENERATION, seq, event_count: seq + 1,
  state: { run_id: 'demo', nodes: {}, finished: false, engine_running: true, phase: 'search' },
})
const lifecycleOf = payload => ({
  schema: 1, generation: payload.generation, seq: payload.seq, event_count: payload.event_count,
  engine_running: payload.state.engine_running,
})
const jsonResponse = body => ({
  ok: true, status: 200, headers: { get: () => null }, json: async () => body,
})

// A live `/events` body carrying ONE `state` frame whose `state.pad` is `padChars` long, generated in
// 1 MiB pieces so a frame past the bound never exists as one string here. It then stays open, as a
// live stream does, until the caller aborts. `cancelled` says whether the reader gave the body back.
function stateStream(payload, padChars, signal) {
  const encoder = new TextEncoder()
  const json = JSON.stringify({ ...payload, state: { ...payload.state, pad: '' } })
  const at = json.indexOf('"pad":""') + '"pad":"'.length
  const pieces = [encoder.encode(`id: ${payload.seq}\nevent: state\ndata: ${json.slice(0, at)}`)]
  const mebibyte = encoder.encode('x'.repeat(MIB))
  for (let left = padChars; left > 0; left -= MIB) {
    pieces.push(left >= MIB ? mebibyte : encoder.encode('x'.repeat(left)))
  }
  pieces.push(encoder.encode(`${json.slice(at)}\n\n`))
  const handle = { cancelled: false, pulled: 0 }
  let sink = null
  signal?.addEventListener('abort', () => {
    try { sink?.error(new DOMException('aborted', 'AbortError')) } catch { /* already closed */ }
  }, { once: true })
  handle.body = new ReadableStream({
    start(controller) { sink = controller },
    pull(controller) {
      if (handle.pulled < pieces.length) controller.enqueue(pieces[handle.pulled++])
      else return new Promise(() => {})   // live and quiet: nothing more until an abort
    },
    cancel() { handle.cancelled = true },
  }, { highWaterMark: 0 })
  return handle
}

async function withRunStateHarness({ routes }, body) {
  const realSetTimeout = globalThis.setTimeout
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const calls = []
  const fetchStub = (input, options = {}) => {
    const url = String(input)
    calls.push(url)
    const route = Object.keys(routes).find(suffix => url.endsWith(suffix))
    if (!route) throw new Error(`unexpected request: ${url}`)
    return Promise.resolve(routes[route](options))
  }
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node, Event: dom.window.Event,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage, fetch: fetchStub,
    setTimeout: (callback, delay, ...args) =>
      realSetTimeout(callback, Math.max(1, Math.round((delay || 0) / 100)), ...args),
    IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  let root
  try {
    const { createRoot } = await import('react-dom/client')
    const hook = { latest: null }
    const Harness = () => { hook.latest = useRunState('demo'); return null }
    root = createRoot(document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness)) })
    const count = suffix => calls.filter(url => url.endsWith(suffix)).length
    const settle = ms => React.act(async () => { await new Promise(resolve => realSetTimeout(resolve, ms)) })
    const waitFor = async (predicate, message, timeoutMs = 10000) => {
      const deadline = Date.now() + timeoutMs
      while (Date.now() < deadline) {
        if (predicate()) return
        await settle(5)
      }
      assert.fail(message)
    }
    await body({ hook, count, settle, waitFor })
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
}

test('a first state frame past the old 2 MiB bound connects the owner stream', async () => {
  const streams = []
  await withRunStateHarness({
    routes: {
      '/state': () => jsonResponse(snapshot(12)),
      '/events': options => {
        const stream = stateStream(snapshot(13), 3 * MIB, options.signal)
        streams.push(stream)
        return { ok: true, status: 200, headers: { get: () => null }, body: stream.body }
      },
    },
  }, async ({ hook, count, settle, waitFor }) => {
    await waitFor(() => hook.latest?.seq === 13, 'the 3 MiB first frame was never accepted')
    assert.equal(hook.latest.live.pad.length, 3 * MIB, 'the frame arrived whole')
    assert.equal(hook.latest.connected, true)
    assert.equal(hook.latest.status, 'ready')
    assert.equal(hook.latest.degraded, false)
    await settle(300)
    assert.equal(count('/events'), 1, 'an accepted stream is not reopened')
    assert.equal(streams[0].cancelled, false)
  })
})

test('a frame past the stream bound degrades to the lifecycle probe: one /events, never a loop', async () => {
  const streams = []
  let stateReads = 0
  await withRunStateHarness({
    routes: {
      // The GET is uncapped, as it always was; it answers the moved run after the first read.
      '/state': () => jsonResponse(snapshot(stateReads++ === 0 ? 12 : 13)),
      '/lifecycle': () => jsonResponse(lifecycleOf(snapshot(13))),
      '/events': options => {
        const stream = stateStream(snapshot(12), RUN_STATE_MAX_FRAME_CHARS + MIB, options.signal)
        streams.push(stream)
        return { ok: true, status: 200, headers: { get: () => null }, body: stream.body }
      },
    },
  }, async ({ hook, count, settle, waitFor }) => {
    await waitFor(() => hook.latest?.status === 'ready', 'the initial /state never committed')
    // The observation window: 1.5 s real is 150 s of the hook's clock — dozens of reconnects on the
    // old backoff ramp, two and a half minute-scale probes on the degraded path.
    await settle(1500)
    assert.equal(count('/events'), 1,
      `an oversized first frame must not reopen the stream: ${count('/events')} /events requests`)
    assert.equal(streams[0].cancelled, true, 'the refused stream\'s reader must be given back')
    assert.equal(hook.latest.degraded, true, 'the workspace must be able to say it is degraded')
    assert.equal(hook.latest.status, 'ready', 'the last good snapshot stays on screen')
    assert.ok(count('/lifecycle') >= 1, 'the degraded connection follows the run by the small probe')
    await waitFor(() => hook.latest.seq === 13, 'a moved run was never re-read through /state')
    assert.equal(hook.latest.connected, true)
    assert.equal(count('/events'), 1, 're-reading the moved run must not reopen the stream either')
  })
})
