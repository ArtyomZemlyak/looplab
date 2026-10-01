import test from 'node:test'
import assert from 'node:assert/strict'

import { fetchStub, jsonResponse, mountLive, until } from './_mount.js'

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
      title: 'Answer the evaluation question', detail: 'Waiting for an explicit verdict.',
      reads: ['GET /api/runs/{run_id}/harness-checkpoints'],
      action: 'POST /api/runs/{run_id}/harness-checkpoints', phase_id: 'stage_check' }
    let payload = { ...progress, next_step: step }
    globalThis.fetch = fetchStub({ '/api/runs/mnist/harness-progress': () => jsonResponse(payload) })
    const props = { runId: 'mnist', expectedGeneration: generation, seq: 12,
      externalMode: true, configStatus: 'ready', onOpenEvents() {}, onClose() {} }
    const view = await harness.mount(HarnessProgressPanel, props)
    await until(() => view.container.textContent.includes('Next step · Answer'), 'server next step')
    assert.match(view.container.textContent, /GET \/api\/runs\/mnist\/harness-checkpoints/)
    assert.match(view.container.textContent, /phase_info: stage_check/)
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
