import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'

import { click, fetchStub, jsonResponse, mountLive, until } from './_mount.js'

const generation = 'a'.repeat(64)
const progress = {
  generation, event_seq: 12, at_node: 2, complete: true,
  source_health: Object.fromEntries(['decisions', 'reviews', 'checkpoints']
    .map(kind => [kind, { accepted_rows: 0 }])),
  history: Object.fromEntries(['decisions', 'reviews', 'checkpoints']
    .map(kind => [kind, { total: 0, items: [], has_more: false }])),
  candidate_requirements: { effective_concepts: false, hypothesis_statement: true },
  candidate_blockers_if_expanding: [], candidate_decisions_per_idea: {},
  pending_checkpoint_count: 0, pending_checkpoints: [],
  finish_pending_nodes: [2], finish_report_due: false, finish_reviews_due: [],
}

test('main status refreshes engine observations without events and withdraws failed or old reads', async t => {
  const harness = await mountLive({ visible: true })
  try {
    const { HarnessProgressPanel } = await harness.load('/src/HarnessProgressPanel.jsx')
    const next_step = { code: 'inspect_pending', owner: 'external_agent', title: 'Inspect evaluations',
      detail: 'Engine last observed: running. Agent connection: not measured.', reads: [], action: null, phase_id: null }
    let payload = { generation, event_seq: 12, next_step, execution: { engine_running: true } }
    let failure = false
    const requests = []
    globalThis.fetch = fetchStub({ '/api/runs/mnist/harness-progress': request => {
      requests.push(request)
      return jsonResponse(payload, failure ? 503 : 200)
    } })
    let opened = 0
    const props = { compact: true, runId: 'mnist', expectedGeneration: generation, seq: 12,
      externalMode: true, engineRunning: true, onOpen: () => opened++ }
    t.mock.timers.enable({ apis: ['setInterval'] })
    const view = await harness.mount(HarnessProgressPanel, props)
    await until(() => view.container.textContent.includes('Inspect evaluations'), 'main status')
    assert.equal(requests[0].url.searchParams.get('brief'), 'true')
    assert.equal(requests[0].url.searchParams.get('expected_generation'), generation)
    assert.equal(requests[0].init.cache, 'no-store')
    payload = { ...payload, execution: { engine_running: false }, next_step: { ...next_step,
      title: 'Engine stopped', detail: 'Recorded starts do not prove training continues.' } }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Engine stopped'), 'same-event engine probe')
    assert.ok(!view.container.textContent.includes('Engine last observed: running'))
    failure = true
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Next step unavailable'), 'failed probe')
    assert.ok(!view.container.textContent.includes('Engine stopped'))
    failure = false
    await click([...view.container.querySelectorAll('button')].find(x => x.textContent === 'Retry'))
    await until(() => view.container.textContent.includes('Engine stopped'), 'explicit read retry')
    await click([...view.container.querySelectorAll('button')].find(x => x.textContent === 'Agent cycle'))
    assert.equal(opened, 1)
    await view.rerender({ ...props, seq: 13 })
    await until(() => requests.length >= 4, 'new event read')
    assert.ok(!view.container.textContent.includes('Engine stopped'), 'older event cannot advise the current run')
    await view.rerender({ ...props, expectedGeneration: 'b'.repeat(64) })
    await until(() => requests.at(-1).url.searchParams.get('expected_generation') === 'b'.repeat(64), 'new generation read')
    assert.ok(!view.container.textContent.includes('Engine stopped'))
    assert.ok(requests.every(request => request.init.method === 'GET' || request.init.method == null))
  } finally { await harness.close() }
})

test('external cycle renders candidate and finalization obligations from progress', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { HarnessProgressPanel } = await harness.load('/src/panels.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/mnist/harness-progress': progress })
    const view = await harness.mount(HarnessProgressPanel, {
      runId: 'mnist', expectedGeneration: generation, externalMode: true,
      configStatus: 'ready', onOpenEvents() {}, onClose() {},
    })
    await until(() => view.container.textContent.includes('Measured prefix: 2 nodes'),
      'harness progress payload to render')
    const content = view.container.textContent.replace(/\s+/g, ' ')
    assert.match(content, /A nonempty hypothesis statement is required/)
    assert.match(content, /Wait for or explicitly abort pending nodes: 2/)
    assert.ok(globalThis.fetch.calls.some(call =>
      call.method === 'GET' && call.path === '/api/runs/mnist/harness-progress'))
  } finally {
    await harness.close()
  }
})

test('next step follows server advice, refreshes on events and hides after a failed refresh', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { HarnessProgressPanel } = await harness.load('/src/panels.jsx')
    const step = { code: 'answer_checkpoint', owner: 'external_agent',
      title: 'Answer the evaluation question', detail: 'This is a recorded question from a stopped engine. Resume may supersede this question; refresh after resume before answering.',
      reads: ['GET /api/runs/{run_id}/harness-checkpoints'],
      action: 'POST /api/runs/{run_id}/harness-checkpoints', phase_id: 'evaluation' }
    let payload = { ...progress, next_step: step }
    globalThis.fetch = fetchStub({ '/api/runs/mnist/harness-progress': () => jsonResponse(payload) })
    const props = { runId: 'mnist', expectedGeneration: generation, seq: 12,
      externalMode: true, configStatus: 'ready', onOpenEvents() {}, onClose() {} }
    const view = await harness.mount(HarnessProgressPanel, props)
    await until(() => view.container.textContent.includes('Next step · Answer'), 'server next step')
    assert.match(view.container.textContent, /GET \/api\/runs\/mnist\/harness-checkpoints/)
    assert.match(view.container.textContent, /phase_info: evaluation/)
    assert.match(view.container.textContent, /recorded question from a stopped engine/)
    assert.match(view.container.textContent, /refresh after resume before answering/)
    payload = { ...payload, event_seq: 13, next_step: { ...step, code: 'choose_direction',
      title: 'Choose the next experiment or finish', action: null, phase_id: null } }
    await view.rerender({ ...props, seq: 13 })
    await until(() => view.container.textContent.includes('Next step · Choose'), 'event-driven refresh')
    globalThis.fetch = fetchStub({ '/api/runs/mnist/harness-progress': () => jsonResponse({}, 503) })
    await view.rerender({ ...props, seq: 14 })
    await until(() => view.container.textContent.includes('refresh failed'), 'failed refresh')
    assert.ok(!view.container.textContent.includes('Next step · Choose'))
    assert.match(view.container.textContent, /Next step unavailable/)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await harness.close() }
})

test('malformed next step cannot become a successful read', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { HarnessProgressPanel } = await harness.load('/src/panels.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/mnist/harness-progress': {
      ...progress, next_step: { title: 'Everything is ready' },
    } })
    const view = await harness.mount(HarnessProgressPanel, { runId: 'mnist',
      expectedGeneration: generation, externalMode: true, configStatus: 'ready' })
    await until(() => view.container.textContent.includes('Agent cycle: Unavailable'), 'invalid payload')
    assert.ok(!view.container.textContent.includes('Everything is ready'))
  } finally { await harness.close() }
})

test('external cycle shows changing engine observations without implying agent connection', async t => {
  const harness = await mountLive({ visible: true })
  try {
    const { HarnessProgressPanel } = await harness.load('/src/panels.jsx')
    const next_step = { code: 'inspect_pending', owner: 'external_agent',
      title: 'Inspect evaluations already started',
      detail: 'Engine last observed: running. Agent connection: not measured. Recorded activity: 1 admitted, 1 queued, 0 building, 0 untracked.',
      reads: ['GET /api/runs/{run_id}/state?observe_only=true'], action: null, phase_id: null }
    let payload = { ...progress, next_step }
    globalThis.fetch = fetchStub({ '/api/runs/mnist/harness-progress': () => jsonResponse(payload) })
    const props = { runId: 'mnist', expectedGeneration: generation, seq: 12,
      externalMode: true, configStatus: 'ready' }
    t.mock.timers.enable({ apis: ['setInterval'] })
    const view = await harness.mount(HarnessProgressPanel, props)
    await until(() => view.container.textContent.includes('Next step · Inspect evaluations'), 'admitted evaluation')
    assert.match(view.container.textContent, /1 admitted, 1 queued/)
    payload = { ...payload, next_step: { ...next_step,
      title: 'Engine stopped · inspect submitted experiments',
      detail: 'Recorded evaluation starts do not mean training continues. Engine last observed: stopped. Agent connection: not measured.' } }
    // A lock change has no event_seq. The panel's periodic refresh must still
    // withdraw its old running label, without needing a new event.
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Next step · Engine stopped'), 'engine lock observation')
    assert.ok(!view.container.textContent.includes('Engine last observed: running'))
    assert.match(view.container.textContent, /Agent connection: not measured/)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await harness.close() }
})
