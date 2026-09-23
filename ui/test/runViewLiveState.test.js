// RunView FOLLOWS A LIVE RUN — the data flow the run workspace exists for, driven through the whole
// component with its effects running (review 2026-09-22, UI-05, on `_mount.js::mountLive`).
//
// Nothing mounted RunView with its effects before this file. `mountRunView.test.js` renders it
// statically, and a static render runs no effects, so all it can show is the "Opening run…"
// resource state; the run-state pipeline was driven only in PIECES — `stateDelta.test.js` applies
// deltas to plain objects, and `runStateModel.test.js` / `runStateLargeFrame.test.js` drive
// `useRunState` under a probe component that renders nothing. None of them can see the whole chain
// an operator depends on: the `/state` probe painting the workspace, the owner stream's
// `state_delta` frames reaching the lineage graph and the header, and a frame that must NOT reach
// the screen.
//
// Two properties, each read off what the operator sees AND off the requests the page made:
//   1. a delta frame moves the workspace IN PLACE — a new experiment in the lineage graph, the
//      champion mark moving to it, the suspicious-win alarm and the eval budget in the header — and
//      nothing resyncs: one `/state` probe and one `/events` connection carry the whole drive;
//   2. a delta computed against a snapshot this connection does not hold is REFUSED (the rule
//      `stateDelta.js` states and `hooks.js::useRunState` enforces): nothing of it is painted, the
//      stream is dropped, and after exactly the reconnect backoff a fresh connection with NO cursor
//      carries a full frame that repaints the run.
//
// The flow chosen, among the three largest components, because it is the one every other surface
// of the workspace hangs off: a run whose screen stops moving, or moves to numbers that are not the
// run's, is wrong everywhere at once.
//
// TIME: the only timer this drive needs to FIRE is the reconnect backoff, and node:test's
// `mock.timers` fires it — at exactly `MIN_BACKOFF_MS`, not "some time later" — so the load of the
// box changes how long the drive takes, never what it observes.
import test from 'node:test'
import assert from 'node:assert/strict'

import React from 'react'

import { fetchStub, mountLive, settle, sseStream, until } from './_mount.js'

const RUN = 'demo'
const GENERATION = 'a'.repeat(64)

// A minimal-but-real public run payload: the `{generation, seq, event_count, state}` envelope
// `runStateModel.js::runSnapshotIdentity` accepts, and a `quadratic` run (direction `min`) with two
// evaluated experiments and #1 as champion.
const experiment = (id, metric) => ({
  id, parent_ids: id === 0 ? [] : [0], operator: id === 0 ? 'draft' : 'improve',
  idea: { operator: id === 0 ? 'draft' : 'improve', params: { x: 3 - metric }, rationale: '' },
  metric, status: 'evaluated',
})
const snapshot = (seq, state = {}) => ({
  generation: GENERATION, seq, event_count: seq + 1,
  state: {
    run_id: RUN, label: RUN, goal: 'min (x-3)^2', task_id: 'quadratic', direction: 'min',
    phase: 'search', engine_running: true, finished: false,
    nodes: { 0: experiment(0, 1), 1: experiment(1, 0.5) }, best_node_id: 1,
    total_eval_seconds: 30, reward_hacks: [],
    ...state,
  },
})

// The frame a run emits when a suspicious win lands: experiment #2 scores exactly the theoretical
// floor, becomes the champion, and is flagged `perfect_metric` (`trust/reward_hack.py`). Ops in
// `events/state_delta.py`'s vocabulary: a changed list arrives whole, a new key is a `set`.
const SUSPICIOUS_WIN = {
  version: 1, base_seq: 3, seq: 4, generation: GENERATION, event_count: 5,
  ops: [
    ['set', ['seq'], 4],
    ['set', ['event_count'], 5],
    ['set', ['state', 'nodes', '2'], experiment(2, 0)],
    ['set', ['state', 'best_node_id'], 2],
    ['set', ['state', 'total_eval_seconds'], 42],
    ['set', ['state', 'reward_hacks'], [{
      node_id: 2, generation: 0, evidence_version: 1, code_digest: null,
      signals: [{ signal: 'perfect_metric', detail: 'metric 0 at the theoretical floor (0.0)' }],
    }]],
  ],
}

// The server. Everything the workspace reads is answered; the owner stream is a live body the test
// writes frame by frame, recorded with the cursor its request carried (`Last-Event-ID`, absent on a
// fresh connection). `holdProbe` keeps the `/state` probe unanswered until the test releases it.
function runServer({ holdProbe = false } = {}) {
  const streams = []
  let releaseProbe = () => {}
  const probe = holdProbe ? new Promise(resolve => { releaseProbe = resolve }) : null
  const fetch = fetchStub({
    [`GET /api/runs/${RUN}/state`]: async () => { await probe; return snapshot(3) },
    [`GET /api/runs/${RUN}/events`]: ({ init }) => {
      const stream = sseStream({ signal: init.signal })
      streams.push({ stream, cursor: init.headers?.['Last-Event-ID'] ?? null })
      return stream.response()
    },
    [`GET /api/runs/${RUN}/config`]: { max_eval_seconds: 600 },
    [`GET /api/runs/${RUN}/log-page`]: {
      events: [], generation: GENERATION, cursors: { older: null, newer: null },
      has_more: { older: false, newer: false }, torn_tail: false, total_events: 0,
    },
  })
  const reads = path => fetch.calls.filter(call => call.method === 'GET'
    && call.path === `/api/runs/${RUN}${path}`).length
  return { fetch, streams, reads, releaseProbe: () => releaseProbe() }
}

// What the operator reads, as strings or null — never DOM nodes in an assertion (the reporter would
// serialize the whole jsdom window into a failure message).
const read = (view, selector) => view.container.querySelector(selector)?.textContent.trim() ?? null
const heading = view => read(view, 'h1#run-state')
const runLine = view => read(view, '.run-head b')
const liveLabel = view => {
  const status = view.container.querySelector('.run-head .live')
  return status ? status.textContent.replace('Current run status: ', '') : null
}
const alarm = view => read(view, '.run-head button.chip.alarm')
const evalChip = view => [...view.container.querySelectorAll('.run-head button.run-metric-chip')]
  .map(chip => chip.textContent).find(text => text.startsWith('eval')) ?? null
// The lineage graph's accessible name for one experiment ("Experiment #2, …, current champion, …").
const lineage = (view, id) => view.container
  .querySelector(`button.node-select-trigger[data-node-select-id="${id}"]`)
  ?.getAttribute('aria-label') ?? null

const frame = (stream, event, data, id) => React.act(async () => {
  stream.send(event, data, { id })
})

let harness
let RunView
let MIN_BACKOFF_MS

test.before(async () => {
  harness = await mountLive({ visible: true })
  ;({ default: RunView } = await harness.load('/src/RunView.jsx'))
  ;({ MIN_BACKOFF_MS } = await harness.load('/src/runStateModel.js'))
  // The workspace's lazy parts, transformed up front: `React.lazy` then resolves each one inside
  // the commit that asked for it instead of whenever the transform finishes.
  await Promise.all(['/src/Dag.jsx', '/src/Dock.jsx', '/src/ConceptChipBar.jsx']
    .map(path => harness.load(path)))
})

test.after(async () => {
  await harness?.close()
})

async function openWorkspace(server) {
  globalThis.fetch = server.fetch
  sessionStorage.clear()
  localStorage.clear()
  return harness.mount(RunView, { runId: RUN, onBack() {} })
}

test('the workspace follows a live run: the probe paints it and a delta frame moves it in place',
  async () => {
    const server = runServer({ holdProbe: true })
    const view = await openWorkspace(server)
    try {
      // Until the probe answers: the resource state, and no stream — the probe is what turns a
      // mistyped or deleted run into a 404 instead of an endless "Connecting…".
      await settle()
      assert.equal(heading(view), 'Opening run…')
      assert.equal(server.reads('/events'), 0, 'no stream opens before the probe answers')

      await React.act(async () => { server.releaseProbe() })
      await until(() => server.streams.length === 1, 'the owner stream to open after the probe')
      await until(() => lineage(view, 1) != null, 'the lineage graph to draw the probed run')
      assert.equal(runLine(view), 'demo · search · gen aaaaaaaa')
      assert.match(lineage(view, 1), /^Experiment #1, .*metric 0\.5, .*current champion/)
      assert.equal(lineage(view, 2), null)
      assert.equal(liveLabel(view), 'offline', 'painted from the probe, not live until a frame')
      assert.equal(server.streams[0].cursor, null, 'the first connection carries no cursor')

      const { stream } = server.streams[0]
      await frame(stream, 'state', snapshot(3), 3)
      await until(() => liveLabel(view) === 'live', 'the first frame to make the workspace live')
      assert.equal(alarm(view), null)
      assert.equal(evalChip(view), 'eval 30s / 600s')

      await frame(stream, 'state_delta', SUSPICIOUS_WIN, 4)
      await until(() => lineage(view, 2) != null,
        'the delta\'s experiment to reach the lineage graph')
      assert.match(lineage(view, 2), /^Experiment #2, .*metric 0, .*current champion/)
      assert.doesNotMatch(lineage(view, 1), /current champion/, 'the champion mark moved with it')
      assert.equal(alarm(view), 'hack? 1', 'the suspicious win raises the header alarm')
      assert.equal(evalChip(view), 'eval 42s / 600s')
      assert.equal(liveLabel(view), 'live')

      // Nothing resynced: the delta applied to the snapshot this connection held.
      assert.equal(server.reads('/state'), 1, 'one probe for the whole drive')
      assert.equal(server.reads('/events'), 1, 'one stream connection for the whole drive')
      assert.equal(stream.open, true, 'and it is still open')
    } finally {
      await view.unmount()
    }
  })

test('a delta against a snapshot the connection does not hold is refused, unpainted, and resynced',
  async t => {
    const server = runServer()
    const view = await openWorkspace(server)
    try {
      await until(() => server.streams.length === 1, 'the owner stream to open after the probe')
      const { stream } = server.streams[0]
      await frame(stream, 'state', snapshot(3), 3)
      await until(() => liveLabel(view) === 'live' && lineage(view, 1) != null,
        'the first frame to make the workspace live')

      // From here on the reconnect backoff is a timer the test fires, not one it waits for.
      t.mock.timers.enable({ apis: ['setTimeout'] })
      // The same frame, computed against seq 2: this connection holds seq 3.
      await frame(stream, 'state_delta', { ...SUSPICIOUS_WIN, base_seq: 2 }, 4)
      await until(() => !stream.open, 'the client to drop the diverged stream')
      await settle()
      assert.equal(liveLabel(view), 'offline', 'a refused frame takes the workspace off live')
      assert.equal(lineage(view, 2), null, 'nothing of the refused delta is painted')
      assert.equal(alarm(view), null)
      assert.equal(evalChip(view), 'eval 30s / 600s')
      assert.match(lineage(view, 1), /current champion/, 'the last good snapshot stays on screen')

      // The reconnect waits exactly the backoff.
      await React.act(async () => { t.mock.timers.tick(MIN_BACKOFF_MS - 1) })
      await settle()
      assert.equal(server.streams.length, 1, 'no reconnect before the backoff has passed')
      await React.act(async () => { t.mock.timers.tick(1) })
      await until(() => server.streams.length === 2, 'the reconnect once the backoff has passed')
      assert.equal(server.streams[1].cursor, null,
        'the fresh connection carries no cursor, so the server answers with a full frame')

      // The full frame — the run as it really is at seq 4 — repaints the workspace.
      await frame(server.streams[1].stream, 'state', snapshot(4, {
        nodes: { 0: experiment(0, 1), 1: experiment(1, 0.5), 2: experiment(2, 0.25) },
        best_node_id: 2, total_eval_seconds: 42,
      }), 4)
      await until(() => liveLabel(view) === 'live' && lineage(view, 2) != null,
        'the full frame to repaint the run')
      assert.match(lineage(view, 2), /^Experiment #2, .*metric 0\.25, .*current champion/)
      assert.equal(alarm(view), null, 'the resynced run carries no alarm: none was ever real')
      assert.equal(evalChip(view), 'eval 42s / 600s')
      assert.equal(server.reads('/state'), 1, 'the resync came through the stream, not a new probe')
    } finally {
      t.mock.timers.reset()
      await view.unmount()
    }
  })
