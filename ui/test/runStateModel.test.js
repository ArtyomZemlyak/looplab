// Guard for the run-state connection RULES (doc 25 UI-09) — the decisions `hooks.js::useRunState`
// used to spell inside one ~325-line effect over ~20 mutable closure variables. Nothing here reads
// source text: the pure rules get a truth table, and the two properties that only exist once the
// rules meet React (a snapshot identity that dedupes, and an event count that participates in it)
// are driven through a real `useRunState` with a controllable transport.
//
// The React half deliberately records the SEQUENCE of committed renders rather than sampling the
// hook while a poll interval ticks — a live sample is a wall-clock flake, and the property being
// guarded is "how many commits happened and with what", which is exactly what a sequence states.
import test from 'node:test'
import assert from 'node:assert/strict'

import { JSDOM } from 'jsdom'
import React from 'react'

import { useRunState } from '../src/hooks.js'
import {
  MAX_BACKOFF_MS, MIN_BACKOFF_MS, TERMINAL_PROBE_MAX_MS, TERMINAL_PROBE_MS,
  classifyRunProbeFailure, identityChanged, initialRunIdentity, nextBackoffMs, nextStreamCursor,
  normalizeEventCount, ownerRunEnded, reviewLinkEnded, runSnapshotIdentity, snapshotMovedBackwards,
  streamCursorMatchesSnapshot, terminalSnapshot,
} from '../src/runStateModel.js'

const GEN_A = 'a'.repeat(64)
const GEN_B = 'b'.repeat(64)
const snapshot = (overrides = {}) => ({
  generation: GEN_A, seq: 12, event_count: 13,
  state: { run_id: 'demo', nodes: {}, engine_running: true, finished: false, phase: 'search' },
  ...overrides,
})

test('a run snapshot identity is [seq, alive, generation, eventCount] or an outright rejection', () => {
  assert.deepEqual(runSnapshotIdentity(snapshot()), [12, true, GEN_A, 13])
  // The compact `/lifecycle` probe carries `engine_running` at the top level and no `state` at all;
  // only the probe form may omit it, and only the snapshot form may omit `schema`.
  assert.deepEqual(
    runSnapshotIdentity({ schema: 1, generation: GEN_A, seq: 12, event_count: 13, engine_running: false }, true),
    [12, false, GEN_A, 13])

  // `event_count` is additive: a legacy server that omits it is tolerated as a null component, which
  // is NOT the same as a server that sends a malformed one.
  assert.deepEqual(runSnapshotIdentity(snapshot({ event_count: undefined })), [12, true, GEN_A, null])
  assert.equal(normalizeEventCount(null), null)
  for (const bad of [-1, 1.5, '13', Number.MAX_SAFE_INTEGER + 2, NaN]) {
    assert.equal(normalizeEventCount(bad), undefined, String(bad))
  }

  // A generation is either absent or exact. A malformed one must not silently degrade to "no
  // generation": that is the value the monotonicity and cursor rules below key off.
  assert.deepEqual(runSnapshotIdentity(snapshot({ generation: undefined })), [12, true, null, 13])
  const rejected = [
    [null, false, 'a missing payload'],
    ['{}', false, 'a string body'],
    [[snapshot()], false, 'an array body'],
    [snapshot({ seq: -2 }), false, 'a sequence below the empty-run sentinel'],
    [snapshot({ seq: 1.5 }), false, 'a fractional sequence'],
    [snapshot({ state: undefined }), false, 'a snapshot with no state'],
    [snapshot({ state: [] }), false, 'a snapshot whose state is an array'],
    [snapshot({ generation: 'nope' }), false, 'a malformed generation'],
    [snapshot({ event_count: -1 }), false, 'a negative event count'],
    [{ generation: GEN_A, seq: 12, engine_running: false }, true, 'a probe with no schema marker'],
    [{ schema: 2, generation: GEN_A, seq: 12, engine_running: false }, true, 'a probe of a future schema'],
  ]
  for (const [payload, probe, why] of rejected) {
    assert.throws(() => runSnapshotIdentity(payload, probe), /Invalid/, why)
  }
})

test('the initial identity commits the first frame, and every component participates in dedupe', () => {
  const first = runSnapshotIdentity(snapshot())
  assert.equal(identityChanged(initialRunIdentity(), first), true,
    'the very first accepted snapshot must always commit')
  assert.equal(identityChanged(first, runSnapshotIdentity(snapshot())), false)

  // Each of the four components alone is enough to make a frame new. `event_count` is the one that
  // matters most and is easiest to lose: it is what the timeline's exact lag and unread counts read,
  // and a run can genuinely append events without its seq, liveness or generation changing.
  const moved = {
    seq: snapshot({ seq: 13 }),
    alive: snapshot({ state: { ...snapshot().state, engine_running: false } }),
    generation: snapshot({ generation: GEN_B }),
    eventCount: snapshot({ event_count: 14 }),
  }
  for (const [component, payload] of Object.entries(moved)) {
    assert.equal(identityChanged(first, runSnapshotIdentity(payload)), true,
      `a changed ${component} must count as a new snapshot`)
  }
})

test('snapshot monotonicity is scoped to one durable generation', () => {
  const at = (seq, eventCount, generation = GEN_A) =>
    runSnapshotIdentity(snapshot({ seq, event_count: eventCount, generation }))
  const current = at(12, 13)
  assert.equal(snapshotMovedBackwards(current, at(13, 14)), false)
  assert.equal(snapshotMovedBackwards(current, at(12, 13)), false)
  assert.equal(snapshotMovedBackwards(current, at(11, 13)), true, 'a rewound seq is a rejected frame')
  assert.equal(snapshotMovedBackwards(current, at(12, 12)), true, 'a rewound event count is too')
  // A start-over re-spawns the run under a NEW generation whose seq legitimately restarts at -1.
  // Rejecting that would strand the workspace on the archived generation's last frame forever.
  assert.equal(snapshotMovedBackwards(current, at(-1, 0, GEN_B)), false)
  // A run with no generation yet cannot be compared: there is no durable identity to be monotone in.
  assert.equal(snapshotMovedBackwards(at(12, 13, null), at(-1, 0, null)), false)
})

test('an ended run and an ended review link are different terminal vocabularies', () => {
  // 401 is the whole point of the asymmetry. An OWNER whose token blipped must reconnect; telling
  // them the run was removed would be a lie that also stops anything from retrying. A REVIEWER's
  // grant is the only thing that authorized the read, so its withdrawal ends the link.
  assert.equal(ownerRunEnded({ status: 401 }), false)
  assert.equal(reviewLinkEnded({ status: 401 }), true)
  for (const status of [404, 410]) {
    assert.equal(ownerRunEnded({ status }), true, String(status))
    assert.equal(reviewLinkEnded({ status }), true, String(status))
  }
  for (const status of [500, 502, 504, undefined]) {
    assert.equal(ownerRunEnded({ status }), false, String(status))
    assert.equal(reviewLinkEnded({ status }), false, String(status))
  }
  assert.equal(ownerRunEnded(undefined), false)
  assert.equal(reviewLinkEnded(null), false)
})

test('the one-shot probe failure has three outcomes and only one of them strands the workspace', () => {
  const owner = error => classifyRunProbeFailure(error, false)
  const review = error => classifyRunProbeFailure(error, true)

  assert.equal(owner({ status: 404 }), 'not_found')
  assert.equal(owner({ status: 410 }), 'not_found')
  assert.equal(owner({ status: 401 }), 'transient', 'an owner token blip is not a removed run')
  assert.equal(review({ status: 401 }), 'gone')
  assert.equal(review({ status: 404 }), 'gone')
  assert.equal(review({ status: 410 }), 'gone',
    'a revoked link reads as an expired link, never as "this run does not exist"')
  // Everything else is transient on BOTH paths: a proxy 504, a dropped connection and a
  // keepalive-starved idle drop must leave something scheduled to retry (UI-2).
  for (const error of [{ status: 500 }, { status: 504 }, { status: 429 }, new Error('offline'), null]) {
    assert.equal(owner(error), 'transient', JSON.stringify(error))
    assert.equal(review(error), 'transient', JSON.stringify(error))
  }
})

test('every backoff in the subsystem doubles to a named ceiling', () => {
  assert.equal(MIN_BACKOFF_MS, 1500)
  assert.equal(MAX_BACKOFF_MS, 30000)
  assert.equal(TERMINAL_PROBE_MS, 60000)
  assert.equal(TERMINAL_PROBE_MAX_MS, 300000)
  assert.equal(nextBackoffMs(MIN_BACKOFF_MS), 3000)
  assert.equal(nextBackoffMs(20000), MAX_BACKOFF_MS, 'the reconnect ramp stops at 30s')
  assert.equal(nextBackoffMs(MAX_BACKOFF_MS), MAX_BACKOFF_MS)
  assert.equal(nextBackoffMs(TERMINAL_PROBE_MS, TERMINAL_PROBE_MAX_MS), 120000)
  assert.equal(nextBackoffMs(200000, TERMINAL_PROBE_MAX_MS), TERMINAL_PROBE_MAX_MS,
    'the terminal probe ramp stops at five minutes')
})

test('a stream cursor must name its own snapshot and never outlive its generation', () => {
  assert.equal(streamCursorMatchesSnapshot('12', { seq: 12 }), true)
  assert.equal(streamCursorMatchesSnapshot('12', { seq: 13 }), false,
    'a cursor that does not name its frame means the stream and the state have diverged')
  assert.equal(streamCursorMatchesSnapshot('', { seq: 12 }), false)
  assert.equal(streamCursorMatchesSnapshot('12', undefined), false)

  assert.equal(nextStreamCursor('12', [12, true, GEN_A, 13]), '12')
  assert.equal(nextStreamCursor('', [12, true, GEN_A, 13]), '')
  // A reset/empty startup state has no generation yet, so carrying its id into the next connect
  // would ask the server to resume from a cursor scoped to a run generation that no longer exists.
  assert.equal(nextStreamCursor('-1', [-1, false, null, 0]), '')
})

test('a terminal snapshot is finished, stopped, and past finalization', () => {
  const state = extra => ({ state: { engine_running: false, finished: true, phase: 'finished', ...extra } })
  assert.equal(terminalSnapshot(state()), true)
  assert.equal(terminalSnapshot(state({ phase: 'finalizing' })), false,
    'finalization is still work; a stream must stay open for it')
  assert.equal(terminalSnapshot(state({ engine_running: true })), false)
  assert.equal(terminalSnapshot(state({ finished: false })), false)
  assert.equal(terminalSnapshot({}), false)
  assert.equal(terminalSnapshot(null), false)
})

// ---------------------------------------------------------------------------------------------
// The React half. These two properties cannot be stated by the pure rules alone: they are about
// `useRunState` CONSULTING them — that a frame whose identity did not move commits nothing, and
// that the event count reaches the hook's return value. The review (`pollOnly`) path is used
// because it is the one of the three machines that is driven entirely by `/state` responses.
// ---------------------------------------------------------------------------------------------
async function withRunState(body, options = { pollOnly: true, pollMs: 5 }) {
  const realSetTimeout = globalThis.setTimeout
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const queue = []
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node, Event: dom.window.Event,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    fetch: (input, init = {}) => new Promise((resolve, reject) => {
      const entry = { url: String(input), resolve, reject }
      queue.push(entry)
      init.signal?.addEventListener('abort',
        () => reject(new DOMException('aborted', 'AbortError')), { once: true })
    }),
    // Compress exactly ONE delay: the review re-probe ramp's first step. Compressing every timer
    // would also compress `deadlineGet`'s 8s request deadline and abort every read in this file.
    setTimeout: (callback, delay, ...args) =>
      realSetTimeout(callback, delay === MIN_BACKOFF_MS ? 4 : delay, ...args),
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
    // The SEQUENCE of distinct run snapshots this hook has published, captured in the render body
    // and appended to only when something actually moved. `live` participates BY OBJECT IDENTITY on
    // purpose: `setLive(withBuilding(payload.state))` is handed a freshly parsed object on every
    // poll, so an identity dedupe that stopped covering it would look correct in every value while
    // handing each consumer a new object — and a new object is a re-render for all of them.
    //
    // Rendering-count assertions would be the wrong instrument here: the poll path also re-asserts
    // `connected`/`status`/`error` on every tick, so React legitimately re-runs the component for a
    // run that did not move. What must not change is what it publishes.
    const published = []
    let latest = null
    const Harness = () => {
      latest = useRunState('demo', options)
      const row = { seq: latest.seq, generation: latest.generation, eventCount: latest.eventCount }
      const previous = published.at(-1)
      if (!previous || previous.live !== latest.live || previous.seq !== row.seq
        || previous.generation !== row.generation || previous.eventCount !== row.eventCount) {
        published.push({ ...row, live: latest.live })
      }
      return null
    }
    root = createRoot(dom.window.document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness)) })
    // Answer one queued request; each answer is its own `act` so the commit it causes is flushed
    // before the next frame is queued. Nothing here waits on wall-clock.
    const answer = async (payload, status = 200) => {
      const entry = queue.shift()
      assert.ok(entry, 'the hook did not have a request outstanding')
      await React.act(async () => {
        if (status >= 400) {
          entry.resolve({
            ok: false, status, headers: { get: () => null }, json: async () => ({ detail: 'x' }),
          })
        } else {
          entry.resolve({ ok: true, status, headers: { get: () => null }, json: async () => payload })
        }
        await Promise.resolve()
      })
    }
    // The poll re-arms on a timer; wait for the NEXT request to be queued rather than for a delay.
    const nextRequest = async () => {
      for (let attempt = 0; attempt < 200 && !queue.length; attempt += 1) {
        await React.act(async () => { await new Promise(resolve => setTimeout(resolve, 2)) })
      }
      assert.ok(queue.length, 'the poll did not re-arm')
    }
    await body({ answer, nextRequest, published, current: () => latest, queue })
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
}

const identityRow = ({ seq, generation, eventCount }) => [seq, generation, eventCount]

test('useRunState publishes one snapshot per moved frame and republishes nothing for the rest',
  async () => {
    await withRunState(async ({ answer, nextRequest, published, current }) => {
      await answer(snapshot())
      assert.equal(current().status, 'ready')
      const settled = published.length
      const live = current().live
      assert.deepEqual(identityRow(published.at(-1)), [12, GEN_A, 13])

      // Three identical frames. Without the identity dedupe each poll tick would hand every consumer
      // of the run state a brand-new `live` object on a 4s cadence for a run that is not moving.
      for (let tick = 0; tick < 3; tick += 1) {
        await nextRequest()
        await answer(snapshot())
      }
      assert.equal(published.length, settled, 'an unchanged snapshot must publish nothing')
      assert.equal(current().live, live, 'and must not replace the state object either')

      // Only the event count moves: same seq, same liveness, same generation. That is the ordinary
      // shape of a busy run between two seq bumps, and it is what the timeline's exact lag and its
      // unread counts read — so an identity that stopped carrying it would silently freeze both.
      await nextRequest()
      await answer(snapshot({ event_count: 14 }))
      assert.equal(published.length, settled + 1, 'a moved event count must publish a new snapshot')
      assert.deepEqual(identityRow(published.at(-1)), [12, GEN_A, 14])
      assert.notEqual(current().live, live)
    })
  })

test('useRunState refuses a rewound frame within a generation and accepts a reset into a new one',
  async () => {
    await withRunState(async ({ answer, nextRequest, published, current }) => {
      await answer(snapshot({ seq: 20, event_count: 30 }))
      assert.deepEqual(identityRow(published.at(-1)), [20, GEN_A, 30])
      const settled = published.length

      // A rewound frame inside the SAME generation is a corrupt read, not history: committing it
      // would roll the workspace backwards and, on the timeline, invent unread events.
      await nextRequest()
      await answer(snapshot({ seq: 19, event_count: 30 }))
      await nextRequest()
      await answer(snapshot({ seq: 20, event_count: 29 }))
      assert.equal(published.length, settled, 'a rejected frame publishes nothing at all')
      assert.deepEqual(identityRow(published.at(-1)), [20, GEN_A, 30])

      // A start-over is a NEW generation whose seq legitimately restarts. Refusing it here is what
      // would strand the workspace on the archived generation's last frame.
      await nextRequest()
      await answer(snapshot({ seq: -1, event_count: 0, generation: GEN_B }))
      assert.deepEqual(identityRow(published.at(-1)), [-1, GEN_B, 0])
    })
  })

test('a revoked review link is terminal while a proxy blip keeps re-probing', async () => {
  await withRunState(async ({ answer, current }) => {
    await answer(null, 410)
    assert.equal(current().status, 'gone')
    assert.match(current().error, /expired, was revoked, or is invalid/)
  })
  await withRunState(async ({ answer, nextRequest, current }) => {
    await answer(null, 504)
    assert.equal(current().status, 'error')
    // The whole point of 'transient': something is still scheduled. A `gone`/`not_found` screen with
    // nothing left to retry is the stranded workspace UI-2 was about.
    await nextRequest()
    await answer(snapshot())
    assert.equal(current().status, 'ready')
  })
})
