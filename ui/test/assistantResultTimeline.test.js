import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { resultConversation } from '../src/assistantResultTimeline.js'
import { click, fetchStub, mountLive, until } from './_mount.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'

test('completion stays between recorded conversation turns after reload and commentary changes', () => {
  const messages = [{ role: 'user', ts: 10 }, { role: 'assistant', ts: 12 }, { role: 'user', ts: 30 }]
  const row = { id: 'node:2:1', completed_at: 20 }
  for (const result of [row, { ...row, commentary: 'Explanation arrived later' }]) {
    const items = resultConversation(messages, [result])
    assert.deepEqual(items.map(item => item.result ? 'result' : item.index), [0, 1, 'result', 2])
    assert.equal(items[2].result.id, row.id)
  }
  assert.deepEqual(resultConversation(messages, []).map(item => item.index), [0, 1, 2])
})

test('legacy missing times do not manufacture historical ordering or reorder dialogue', () => {
  const messages = [{ ts: 10 }, {}, { ts: 30 }]
  const rows = [{ id: 'legacy' }, { id: 'measured', completed_at: 20 }]
  assert.deepEqual(resultConversation(messages, rows).map(item => item.result?.id ?? item.index),
    [0, 1, 'measured', 2, 'legacy'])
})

test('interpretation is the assistant message; evidence is collapsed and dialogue remains on failed refresh', async t => {
  const harness = await mountLive()
  const { default: Results } = await harness.load('/src/AssistantResults.jsx')
  let invalid = false
  globalThis.fetch = fetchStub({ 'GET /api/runs/demo/result-notices': () => invalid ? {} : payload([
    { ...node, completed_at: 20, commentary: 'The change did not improve the comparable score. Try a smaller step.' },
  ]) })
  localStorage.clear()
  t.mock.timers.enable({ apis: ['setInterval'] })
  const view = await harness.mount(Results, { runId: 'demo', generation,
    messages: [{ content: 'Please evaluate', ts: 10 }, { content: 'What next?', ts: 30 }],
    renderMessage: (message, index) => React.createElement('div', { key: index, 'data-turn': index }, message.content) })
  try {
    await until(() => view.container.querySelector('article'), 'completion message')
    const article = view.container.querySelector('article')
    assert.ok(article.classList.contains('assistant'))
    assert.match(article.querySelector('.asst-result-commentary').textContent, /did not improve/)
    assert.equal(article.querySelector('.asst-result-evidence').open, false)
    assert.deepEqual([...view.container.querySelectorAll('[data-turn], article')].map(item =>
      item.tagName === 'ARTICLE' ? 'result' : item.textContent), ['Please evaluate', 'result', 'What next?'])
    await click(article.querySelector('summary'))
    assert.match(article.querySelector('.asst-result-evidence').textContent, /Evaluation score|evaluation score/)
    invalid = true
    await React.act(async () => { t.mock.timers.tick(5000) })
    await until(() => !view.container.querySelector('article'), 'stale summary withdrawn')
    assert.match(view.container.textContent, /Please evaluate.*What next\?/)
    assert.equal(view.container.querySelector('.asst-result-history').open, true)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await view.unmount(); await harness.close() }
})
