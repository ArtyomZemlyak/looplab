import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, mountLive, until, click } from './_mount.js'

test('opens recorded question and navigates to its declared node', async () => {
  const h = await mountLive()
  try {
    const { default: ResearchMap } = await h.load('/src/ResearchMap.jsx')
    const gen = 'a'.repeat(64)
    globalThis.fetch = fetchStub({
      ['GET /api/runs/r/experiments/e/comparison']: { contrasts: [{ id: 'c', control: 'base', treatment: 'candidate', metric: 'recall@200', test: 'test', status: 'computed', control_value: .4, treatment_value: .45, delta: .05, ci95_low: -.01, ci95_high: .11, p_two_sided: .1, p_adjusted: .2, n: 100, clusters: 100, same_protocol: false }], caveats: ['different protocol'] },
      ['GET /api/runs/r/research-map']: {
        source_health: { protocol: { read_complete: true } },
        campaign_overview: { published_seq: 1, content: { summary: 'Earlier and current directions', learnings: ['Prior finding'] } },
        experiments: { total: 1, has_more: false, items: [{ experiment_id: 'e', title: 'Wider search',
          question: 'Does it help?', hypothesis: 'Effect may be negative', primary_metric: 'recall@200',
          arms: [{ name: 'candidate', node_id: 3, node_status: 'evaluated' }], relationships: [{ subject_arm: 'candidate', kind: 'model_producer', node_id: 8, explanation: 'weights' }], comparison: '/api/runs/r/experiments/e/comparison' }] },
        nodes: { has_more: false }, reports: { items: [{ seq: 2, headline: 'Earlier result' }] },
      },
    })
    let selected = null
    const view = await h.mount(ResearchMap, { runId: 'r', generation: gen, onPickNode: id => { selected = id } })
    try {
      const details = view.container.querySelector('details')
      await React.act(async () => { details.open = true; details.dispatchEvent(new window.Event('toggle')) })
      await until(() => view.container.textContent.includes('Does it help?'), 'question')
      assert.match(view.container.textContent, /Earlier result/)
      assert.match(view.container.textContent, /Earlier and current directions/)
      assert.match(view.container.textContent, /Prior finding/)
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Show paired comparison'))
      await until(() => view.container.textContent.includes('different protocol'), 'comparison')
      assert.match(view.container.textContent, /0.45/)
      assert.match(view.container.textContent, /same protocol: false/)
      assert.ok(view.container.querySelector('svg[role="img"]'))
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === '#3'))
      assert.equal(selected, 3)
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === '#8'))
      assert.equal(selected, 8)
      assert.match(view.container.textContent, /Model producer/)
      assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})
