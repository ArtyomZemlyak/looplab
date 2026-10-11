// A finished run that later received a stop (`looplab stop --wait` records one so a queued server
// action cannot reopen it) showed a yellow "paused" chip beside "Finished" — found on the finalized
// offline demo, 2026-10-11. Driven live through RunView with the run's own folded state.
import test from 'node:test'
import assert from 'node:assert/strict'

import { fetchStub, mountLive, sseStream, until } from './_mount.js'

const RUN = 'demo'
const GENERATION = 'c'.repeat(64)
const snapshot = (extra) => ({
  generation: GENERATION, seq: 9, event_count: 10,
  state: {
    run_id: RUN, label: RUN, goal: 'min (x-3)^2', task_id: 'toy_quadratic', direction: 'min',
    phase: 'finished', engine_running: false, nodes: {}, best_node_id: null,
    total_eval_seconds: 0, budget_overrides: {}, reward_hacks: [], ...extra,
  },
})

let harness
let RunView
test.before(async () => {
  harness = await mountLive({ visible: true })
  ;({ default: RunView } = await harness.load('/src/RunView.jsx'))
})
test.after(async () => { await harness?.close() })

const server = state => fetchStub({
  [`GET /api/runs/${RUN}/state`]: state,
  [`GET /api/runs/${RUN}/events`]: ({ init }) => sseStream({ signal: init.signal }).response(),
  [`GET /api/runs/${RUN}/config`]: { max_eval_seconds: 600, external_harness: false },
  [`GET /api/runs/${RUN}/log-page`]: {
    events: [], generation: GENERATION, cursors: { older: null, newer: null },
    has_more: { older: false, newer: false }, torn_tail: false, total_events: 0,
  },
})
const pausedChip = view => [...view.container.querySelectorAll('.run-head .chip.warn')]
  .some(chip => /paused/.test(chip.textContent))

for (const [name, extra, expected] of [
  ['a finished run with a later stop shows no "paused" chip', { finished: true, paused: true }, false],
  ['a paused, unfinished run still shows it', { finished: false, paused: true, phase: 'search' }, true],
]) {
  test(name, async () => {
    sessionStorage.clear(); localStorage.clear()
    globalThis.fetch = server(snapshot(extra))
    const view = await harness.mount(RunView, { runId: RUN, onBack() {} })
    try {
      await until(() => view.container.querySelector('.run-head'), 'the run header')
      await until(() => view.container.textContent.includes(RUN), 'the run loaded')
      assert.equal(pausedChip(view), expected)
    } finally { await view.unmount() }
  })
}
