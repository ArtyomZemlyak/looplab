import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { readFileSync } from 'node:fs'
import { fetchStub, jsonResponse, mountLive, until } from './_mount.js'

const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))

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
    await until(() => view.container.textContent.includes('Agent last contacted the API 5s ago'), 'observed contact')
    assert.match(view.container.querySelector('.chip').title, /Browser polling does not count/)
    assert.match(view.container.querySelector('.chip').title, /does not prove agent death/)
    payload = { ...payload, agent_activity: { ...activity, status: 'quiet', age_seconds: 121 } }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Agent last contacted the API 2m 1s ago'), 'quiet contact without event')
    // A server without `attention` keeps the historical rule: quiet is a warning.
    assert.ok(view.container.querySelector('.chip.warn'))
    payload = { ...payload, agent_activity: { ...activity, status: 'quiet', age_seconds: 400,
      attention: 'nothing_required', waiting_for: null } }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Agent last contacted the API 6m 40s ago · nothing required'), 'neutral silence')
    assert.ok(!view.container.querySelector('.chip.warn'))
    payload = { ...payload, agent_activity: { ...activity, status: 'quiet', age_seconds: 400,
      attention: 'waiting_on_agent', waiting_for: { reason: 'evaluation_question', count: 1 } } }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Waiting for the agent: 1 evaluation question · last contact 6m 40s ago'), 'real wait')
    assert.ok(view.container.querySelector('.chip.warn'))
    assert.match(view.container.textContent, /Choose the next experiment/)
    payload = { ...payload, agent_activity: { ...activity, age_seconds: 'false' } }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Agent activity unavailable'), 'malformed contact')
    assert.match(view.container.textContent, /Choose the next experiment/)
    assert.ok(!view.container.textContent.includes('Agent last contacted the API 5s ago'))
    payload = { ...payload, agent_activity: activity }
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Agent last contacted the API 5s ago'), 'new successful observation')
    failed = true
    await React.act(async () => { t.mock.timers.tick(10_000) })
    await until(() => view.container.textContent.includes('Next step unavailable'), 'failed refresh')
    assert.match(view.container.textContent, /Agent activity unavailable/)
    assert.ok(!view.container.textContent.includes('Agent last contacted the API 5s ago'))
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await harness.close() }
})

test('activity supports Russian and never treats quiet or unknown as agent death', async () => {
  const harness = await mountLive({ visible: true })
  try {
    localStorage.clear(); localStorage.setItem('looplab.language', 'en')
    globalThis.fetch = (...args) => String(args[0]).endsWith('/locales/ru.json')
      ? Promise.resolve(jsonResponse(catalogue)) : Promise.reject(new Error('unexpected fetch'))
    const locale = await harness.load('/src/uiLanguage.js')
    const { default: AgentActivity, validAgentActivity } = await harness.load('/src/AgentActivity.jsx')
    await React.act(async () => { locale.setUILanguage('ru'); await locale.loadUILanguage() })
    const view = await harness.mount(AgentActivity, { activity, fresh: true })
    assert.match(view.container.textContent, /Последнее обращение агента к API: 5 с назад/)
    await view.rerender({ activity: { ...activity, age_seconds: 0 }, fresh: true })
    assert.match(view.container.textContent, /Последнее обращение агента к API: только что/)
    await view.rerender({ activity: { ...activity, status: 'quiet', age_seconds: 400, attention: 'nothing_required' }, fresh: true })
    assert.match(view.container.textContent, /Последнее обращение агента к API: 6 мин 40 с назад · ничего не требуется/)
    assert.ok(!view.container.querySelector('.chip.warn'))
    await view.rerender({ activity: { ...activity, status: 'quiet', age_seconds: 123, attention: 'waiting_on_agent',
      waiting_for: { reason: 'evaluation_question', count: 2 } }, fresh: true })
    assert.match(view.container.textContent, /Ждём агента: 2 вопроса оценки · последнее обращение 2 мин 3 с назад/)
    assert.ok(view.container.querySelector('.chip.warn'))
    assert.match(view.container.querySelector('.chip').title, /не доказывает, что агент остановился/)
    await view.rerender({ activity: { ...activity, status: 'not_observed', age_seconds: null,
      last_seen_at: null }, fresh: true })
    assert.match(view.container.textContent, /Обращений агента ещё не было/)
    assert.ok(!validAgentActivity({ ...activity, attention: 'panic' }))
    assert.ok(!validAgentActivity({ ...activity, status: 'quiet' }))
    assert.ok(!validAgentActivity({ ...activity, last_seen_at: 'yesterday' }))
    assert.ok(!validAgentActivity({ ...activity, source: 'browser' }))
    await view.rerender({ activity: null, fresh: true })
    assert.equal(view.container.textContent, '') // old servers remain compatible
  } finally { await harness.close() }
})
