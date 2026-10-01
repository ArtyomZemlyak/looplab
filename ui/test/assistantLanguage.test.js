import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, mountLive, settle, unanswered, until } from './_mount.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'

test('Russian is available before any run, persists across views, and controls the streamed request and briefs', async () => {
  const harness = await mountLive()
  const { default: Bar } = await harness.load('/src/AssistantBar.jsx')
  const backend = fetchStub({
    'GET /api/assistant/commands': { commands: [] },
    'GET /api/assistant/sessions': { sessions: [] },
    'GET /api/assistant/watches': { watches: [] },
    'GET /api/runs': [{ run_id: 'demo', generation, nodes: 1, finished: false, phase: 'active' }],
    'GET /api/runs/demo/result-notices': payload([node]),
    'GET /api/assistant/permissions': ({ init }) => unanswered(init),
    'GET /api/assistant/progress': ({ init }) => unanswered(init),
    'POST /api/assistant/sessions': { id: 'lang-chat', mode: 'plan', title: '' },
    'POST /api/assistant/sessions/lang-chat/message_stream': () => new Response(
      'event: done\ndata: {"ok":true,"reply":"Русский итог."}\n\n',
      { headers: { 'Content-Type': 'text/event-stream' } }),
  })
  globalThis.fetch = backend
  localStorage.clear(); sessionStorage.clear()
  const mounted = await harness.mount(Bar, {})
  try {
    await settle()
    const picker = mounted.container.querySelector('.asst-language select')
    assert.ok(picker, 'language is available before the first completed experiment')
    await React.act(async () => {
      picker.value = 'ru'; picker.dispatchEvent(new window.Event('change', { bubbles: true }))
    })
    assert.equal(localStorage.getItem('looplab.language'), 'ru')
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
    await mounted.rerender({ runId: 'demo' })
    const side = mounted.container.querySelector('.cmdbar-drawer-btn')
    if (side) await click(side)
    await until(() => mounted.container.querySelector('.asst-result-notice button'), 'Russian completion brief')
    assert.match(mounted.container.textContent, /Эксперимент #2/)
    assert.equal(mounted.container.querySelector('.asst-language select').value, 'ru')
    await click(mounted.container.querySelector('.asst-result-notice button'))
    const input = mounted.container.querySelector('textarea')
    assert.match(input.value, /Разбери эксперимент #2, попытку 1/)
    await click([...mounted.container.querySelectorAll('button')].find(button => button.textContent === 'Отправить'))
    await until(() => backend.calls.some(call => /message_stream/.test(call.path)), 'language on stream request')
    const request = backend.calls.find(call => /message_stream/.test(call.path))
    const body = JSON.parse(request.body)
    assert.equal(body.response_language, 'ru')
    assert.equal(body.mode, 'plan', 'changing language cannot change permissions')
    await until(() => /Русский итог/.test(mounted.container.textContent), 'stream reply')
    await mounted.unmount()
    const reopened = await harness.mount(Bar, {})
    try { assert.equal(reopened.container.querySelector('.asst-language select').value, 'ru') }
    finally { await reopened.unmount() }
  } finally { await harness.close() }
})
