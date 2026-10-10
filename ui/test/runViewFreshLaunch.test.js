// A run this tab just started opens when it becomes readable, instead of stranding the user on
// "Run not found" (doc 74; `freshLaunch.js`). Driven live: the `/state` probe answers 404 twice — the
// engine has not written its first event yet — and then the run; the screen says the run is starting
// and opens it with no click. Without the marker the same 404 is "Run not found" at once.
import test from 'node:test'
import assert from 'node:assert/strict'

import { fetchStub, jsonResponse, mountLive, sseStream, until } from './_mount.js'

const RUN = 'offline-demo'
const GENERATION = 'b'.repeat(64)
const snapshot = {
  generation: GENERATION, seq: 3, event_count: 4,
  state: {
    run_id: RUN, label: RUN, goal: 'min (x-3)^2', task_id: 'toy_quadratic', direction: 'min',
    phase: 'search', engine_running: true, finished: false, nodes: {}, best_node_id: null,
    total_eval_seconds: 0, budget_overrides: {}, reward_hacks: [],
  },
}

let harness
let RunView
let fresh

test.before(async () => {
  harness = await mountLive({ visible: true })
  ;({ default: RunView } = await harness.load('/src/RunView.jsx'))
  fresh = await harness.load('/src/freshLaunch.js')
})
test.after(async () => { await harness?.close() })

function server(missingProbes) {
  let probes = 0
  return fetchStub({
    [`GET /api/runs/${RUN}/state`]: () => (++probes <= missingProbes
      ? jsonResponse({ detail: 'run not found' }, 404) : snapshot),
    [`GET /api/runs/${RUN}/events`]: ({ init }) => sseStream({ signal: init.signal }).response(),
    [`GET /api/runs/${RUN}/config`]: { max_eval_seconds: 600, external_harness: false },
    [`GET /api/runs/${RUN}/log-page`]: {
      events: [], generation: GENERATION, cursors: { older: null, newer: null },
      has_more: { older: false, newer: false }, torn_tail: false, total_events: 0,
    },
  })
}
const heading = view => view.container.querySelector('h1#run-state')?.textContent.trim() ?? null

test('a run this tab just started waits for its first records instead of reading "not found"', async () => {
  sessionStorage.clear(); localStorage.clear()
  globalThis.fetch = server(2)
  fresh.markFreshLaunch(RUN)
  const view = await harness.mount(RunView, { runId: RUN, onBack() {} })
  try {
    await until(() => heading(view) === 'Starting the run…', 'the starting state')
    assert.ok([...view.container.querySelectorAll('.resource-state-actions button')]
      .some(button => button.textContent.trim() === 'Back to runs'),
    'the starting screen offers the same way out as its siblings')
    await until(() => !view.container.querySelector('h1#run-state')
      && view.container.textContent.includes(RUN), 'the run opens by itself', { ceilingMs: 15_000 })
    assert.equal(fresh.isFreshLaunch(RUN), false, 'the marker is spent once the run is readable')
  } finally { await view.unmount() }
})

test('the starting screen holds still across its retries: one heading, focus kept', async () => {
  // Each retry used to reset the status to `loading`: the heading alternated with "Opening run…"
  // once a second and focus jumped to <main> (code review of the doc 74 work, driven).
  sessionStorage.clear(); localStorage.clear()
  let probes = 0
  globalThis.fetch = fetchStub({ [`GET /api/runs/${RUN}/state`]: () => {
    probes += 1
    return jsonResponse({ detail: 'run not found' }, 404)
  } })
  fresh.markFreshLaunch(RUN)
  const view = await harness.mount(RunView, { runId: RUN, onBack() {} })
  try {
    await until(() => heading(view) === 'Starting the run…', 'the starting state')
    await new Promise(resolve => setTimeout(resolve, 300))     // the screen's own landing focus
    const back = [...view.container.querySelectorAll('.resource-state-actions button')]
      .find(button => button.textContent.trim() === 'Back to runs')
    back.focus()
    const headings = new Set()
    const started = Date.now()
    const before = probes
    while (Date.now() - started < 3_500) {
      headings.add(heading(view))
      await new Promise(resolve => setTimeout(resolve, 10))
    }
    assert.ok(probes - before >= 2, `the run was asked again (${probes - before} probes)`)
    assert.deepEqual([...headings], ['Starting the run…'])
    assert.equal(document.activeElement, back, 'focus stays where the user put it')
  } finally { await view.unmount() }
})

test('without the marker a 404 is still "Run not found" at once', async () => {
  sessionStorage.clear(); localStorage.clear()
  globalThis.fetch = server(Infinity)
  const view = await harness.mount(RunView, { runId: RUN, onBack() {} })
  try {
    await until(() => heading(view) === 'Run not found', 'the not-found state')
  } finally { await view.unmount() }
})
