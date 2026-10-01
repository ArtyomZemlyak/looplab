import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, jsonResponse, mountLive, until } from './_mount.js'

const activity = { status: 'recent_request', age_seconds: 5,
  last_seen_at: '2026-10-01T10:00:00+00:00', quiet_after_s: 120,
  source: 'authenticated_harness_progress_read', scope: 'this_ui_process' }

test('owner poll renders quiet contact without events and withdraws failed or malformed activity', async t => {
  const harness = await mountLive({ visible: true })
  try {
    const { HarnessProgressPanel } = await harness.load('/src/HarnessProgressPanel.jsx')
    localStorage.clear()
    const generation = 'a'.repeat(64)
    const next_step = { code: 'choose_direction', owner: 'external_agent', title: 'Choose the next experiment',
      detail: 'Agent connection: not measured.', reads: [], action: null, phase_id: null }
    let payload = { generation, event_seq: 12, execution: { engine_running: true }, next_step,
      agent_activity: activity }
    let failed = false
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-progress': () => jsonResponse(payload, failed ? 503 : 200) })
    t.mock.timers.enable({ apis: ['setInterval'] })
    const view = await harness.mount(HarnessProgressPanel, { compact: true, runId: 'demo',
      expectedGeneration: generation, seq: 12, externalMode: true, engineRunning: true })
    await until(() => view.container.textContent.includes('Agent read 5s ago'), 'observed contact')
    assert.match(view.container.querySelector('.chip').title, /Browser polling does not count/)
    assert.match(view.container.querySelector('.chip').title, /does not prove agent death/)
    payload = { ...payload, agent_activity: { ...activity, status: 'quiet', age_seconds: 121 } }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('No recent agent reads · 2m 1s'), 'quiet contact without event')
    assert.ok(view.container.querySelector('.chip.warn'))
    assert.match(view.container.textContent, /Choose the next experiment/)
    payload = { ...payload, agent_activity: { ...activity, age_seconds: 'false' } }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Agent activity unavailable'), 'malformed contact')
    assert.match(view.container.textContent, /Choose the next experiment/)
    assert.ok(!view.container.textContent.includes('Agent read 5s ago'))
    payload = { ...payload, agent_activity: activity }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Agent read 5s ago'), 'new successful observation')
    failed = true
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Next step unavailable'), 'failed refresh')
    assert.match(view.container.textContent, /Agent activity unavailable/)
    assert.ok(!view.container.textContent.includes('Agent read 5s ago'))
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await harness.close() }
})

test('activity supports Russian and never treats quiet or unknown as agent death', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { default: AgentActivity, validAgentActivity } = await harness.load('/src/AgentActivity.jsx')
    localStorage.clear(); localStorage.setItem('looplab.language', 'ru')
    const view = await harness.mount(AgentActivity, { activity, fresh: true })
    assert.match(view.container.textContent, /Агент обращался 5 с назад/)
    await view.rerender({ activity: { ...activity, status: 'quiet', age_seconds: 123 }, fresh: true })
    assert.match(view.container.textContent, /Нет новых обращений агента · 2 мин 3 с/)
    assert.match(view.container.querySelector('.chip').title, /не доказывает гибель агента/)
    await view.rerender({ activity: { ...activity, status: 'not_observed', age_seconds: null,
      last_seen_at: null }, fresh: true })
    assert.match(view.container.textContent, /Обращений агента ещё не наблюдалось/)
    assert.ok(!validAgentActivity({ ...activity, status: 'quiet' }))
    assert.ok(!validAgentActivity({ ...activity, last_seen_at: 'yesterday' }))
    assert.ok(!validAgentActivity({ ...activity, source: 'browser' }))
    await view.rerender({ activity: null, fresh: true })
    assert.equal(view.container.textContent, '') // old servers remain compatible
  } finally { await harness.close() }
})
