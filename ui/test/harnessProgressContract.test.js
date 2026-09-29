import test from 'node:test'
import assert from 'node:assert/strict'

import { fetchStub, mountLive, until } from './_mount.js'

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
