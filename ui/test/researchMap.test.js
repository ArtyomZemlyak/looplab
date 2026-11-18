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

test('Next follows unequal collection cursors and Previous restores the prior request', async () => {
  const h = await mountLive()
  try {
    const { default: ResearchMap } = await h.load('/src/ResearchMap.jsx')
    const names = ['uploads', 'cards', 'research_memos', 'reviews', 'decisions', 'experiments', 'nodes', 'reports']
    const tok = n => `map1:${n}:${'a'.repeat(64)}`
    const requests = []
    const data = second => ({ response_budget: {version: 1, limited: !second, omitted_sections: []},
      source_health: {protocol: {read_complete: true}},
      ...Object.fromEntries(names.map(name => [name, {items: [], total: 0, selected: true, has_more: false, next_cursor: tok(0)}])),
      experiments: {items: [{experiment_id: second ? 'e2' : 'e1', title: second ? 'Second question' : 'First question', arms: []}],
        total: 2, selected: true, has_more: !second, next_cursor: tok(second ? 2 : 1)},
      nodes: {items: (second ? [3,4] : [0,1,2]).map(node_id => ({node_id,status: 'evaluated',title: 'Candidate',parent_ids: [], uses: [],extra_metric_keys: []})),
        total: 5, selected: true, has_more: !second, next_cursor: tok(second ? 5 : 3)} })
    globalThis.fetch = fetchStub({'GET /api/runs/r/research-map': ({url}) => {
      requests.push(url)
      return data(url.searchParams.has('cursors'))
    }})
    const view = await h.mount(ResearchMap, {runId: 'r', generation: 'a'.repeat(64),readOnly: true})
    try {
      const details = view.container.querySelector('details')
      await React.act(async () => {details.open = true; details.dispatchEvent(new window.Event('toggle'))})
      await until(() => view.container.textContent.includes('First question'), 'first page')
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Next'))
      await until(() => view.container.textContent.includes('Second question'), 'continued page')
      const query = requests.at(-1).searchParams, cursors = JSON.parse(query.get('cursors'))
      assert.equal(query.get('offset'), '0')
      assert.equal(cursors.experiments, tok(1)); assert.equal(cursors.nodes, tok(3))
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Previous'))
      await until(() => view.container.textContent.includes('First question'), 'previous page')
      assert.equal(requests.at(-1).searchParams.has('cursors'), false)
      assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
    } finally {await view.unmount()}
  } finally {await h.close()}
})
