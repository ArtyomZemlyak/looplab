import test from 'node:test'
import assert from 'node:assert/strict'
import { click, mountLive, until } from './_mount.js'

test('mounted portfolio distinguishes the external engine using only summary reads', async () => {
  const harness = await mountLive({ visible: true, routes: {
    '/api/runs': [
      { run_id: 'external', label: 'External SGD', external_harness: true, engine_running: true },
      { run_id: 'internal', label: 'Internal search', external_harness: false, engine_running: true },
      { run_id: 'legacy', label: 'Legacy', engine_running: true },
    ],
    '/api/projects': { projects: [], assignments: {} },
    '/api/supertasks': { supertasks: [], assignments: {} },
    '/api/campaign-runs': { folders: [] },
  } })
  try {
    const { default: RunList } = await harness.load('/src/RunList.jsx')
    const view = await harness.mount(RunList, { onOpen() {}, onGlobalNavigate() {} })
    await until(() => view.container.querySelectorAll('.run-card').length === 3)
    const cards = [...view.container.querySelectorAll('.run-card')]
    const external = cards.find(card => card.textContent.includes('External SGD'))
    assert.match(external.textContent, /External agent/)
    assert.match(external.textContent, /engine active/)
    assert.doesNotMatch(external.textContent, /agent connected|agent running/i)
    for (const card of cards.filter(card => card !== external)) {
      assert.match(card.textContent, /running/)
      assert.doesNotMatch(card.textContent, /External agent/)
    }
    assert.ok(harness.fetch.calls.every(call => call.method === 'GET'))
    assert.ok(!harness.fetch.calls.some(call => /\/runs\//.test(call.path)),
      'listing must not fetch per-run config, progress or contract')
  } finally { await harness.close() }
})

test('mounted attention routes an external question to the fenced cycle without answering it', async () => {
  const generation = 'b'.repeat(64)
  const harness = await mountLive({ visible: true, routes: {
    '/api/attention': { snapshot_id: 'c'.repeat(64), stale: false,
      generated_at: 1700000000, partial: false, active_action_count: 1,
      items: [{ id: 'a'.repeat(64), kind: 'external_checkpoint', severity: 'action',
        run_id: 'external', generation, seq: 5, created: 1700000000,
        active: true, browser: false, derived: false, node_id: 0, node_generation: 0 }],
      next_cursor: null, total: 1, truncated: false },
    '/api/assistant/permissions': { pending: [] },
  } })
  try {
    const { default: AttentionCenter } = await harness.load('/src/AttentionCenter.jsx')
    const view = await harness.mount(AttentionCenter)
    await until(() => view.container.querySelector('.attention-trigger')?.textContent.includes('1'))
    await click(view.container.querySelector('.attention-trigger'))
    const link = [...document.querySelectorAll('a')].find(a => a.textContent === 'Open Agent cycle')
    assert.ok(link)
    assert.match(link.getAttribute('href'), /panel=agent/)
    assert.ok(link.getAttribute('href').includes(generation))
    assert.ok(harness.fetch.calls.every(call => call.method === 'GET'))
    assert.doesNotMatch(document.body.textContent, /agent connected|training continues/i)
  } finally { await harness.close() }
})
