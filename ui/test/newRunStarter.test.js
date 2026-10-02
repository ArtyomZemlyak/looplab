import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, mountLive, settle, unanswered, until } from './_mount.js'

let harness, Bar, Starter
test.before(async () => {
  harness = await mountLive()
  ;({ default: Bar } = await harness.load('/src/AssistantBar.jsx'))
  ;({ default: Starter } = await harness.load('/src/NewRunStarter.jsx'))
})
test.after(async () => { await harness?.close() })

test('code and data examples are editable drafts, including disabled and language states', async () => {
  const drafts = []
  const mounted = await harness.mount(Starter, { onDraft: value => drafts.push(value) })
  try {
    for (const button of mounted.container.querySelectorAll('button')) await click(button)
    assert.equal(drafts.length, 2)
    assert.match(drafts[0], /Code on the LoopLab server: \[repository path\]/)
    assert.match(drafts[1], /Data on the LoopLab server: \[data path\]/)
    assert.ok(drafts.every(value => !value.startsWith('/') && /before launch/.test(value)))
    await mounted.rerender({ language: 'ru', disabled: true, onDraft: value => drafts.push(value) })
    assert.match(mounted.container.textContent, /Денежный бюджет задаётся отдельно/)
    for (const button of mounted.container.querySelectorAll('button')) {
      assert.equal(button.disabled, true)
      await click(button)
    }
    assert.equal(drafts.length, 2)
    assert.deepEqual(harness.fetch.calls, [])
  } finally { await mounted.unmount() }
})

test('first-run Assistant drafts examples without sending, keeps edits and follows language changes', async () => {
  localStorage.clear(); sessionStorage.clear()
  const backend = fetchStub({
    'GET /api/assistant/commands': { commands: [] },
    'GET /api/assistant/sessions': { sessions: [] },
    'GET /api/assistant/watches': { watches: [] },
    'GET /api/runs': [],
    'GET /api/settings': { settings: { llm_model: 'local-model', llm_base_url: 'http://localhost/v1' },
      settings_revision: 'one', secret_revision: 'one' },
    'GET /api/assistant/permissions': ({ init }) => unanswered(init),
    'GET /api/assistant/progress': ({ init }) => unanswered(init),
  })
  globalThis.fetch = backend
  const mounted = await harness.mount(Bar)
  const button = label => [...mounted.container.querySelectorAll('button')].find(row => row.textContent === label)
  const input = () => mounted.container.querySelector('textarea')
  const edit = value => React.act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(input(), value)
    input().dispatchEvent(new window.Event('input', { bubbles: true }))
  })
  try {
    await settle()
    const side = mounted.container.querySelector('.cmdbar-drawer-btn')
    if (side) await click(side)
    await until(() => button('Start a new run'), 'new run entry')
    await click(button('Start a new run'))
    await until(() => button('I have code'), 'draft examples')
    await click(button('I have code'))
    assert.match(input().value, /\[repository path\]/)
    assert.equal(document.activeElement, input(), 'example focuses the editable question')
    assert.equal(button('I have data').disabled, true, 'another example cannot overwrite this draft')
    await edit('My edited goal with a real path')
    await click(button('I have data'))
    assert.equal(input().value, 'My edited goal with a real path')
    await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'ru' })))
    assert.equal(input().value, 'My edited goal with a real path', 'language changes preserve authored content')
    await until(() => button('Настроить модель'), 'Russian first-run model controls')
    assert.match(mounted.container.textContent, /Связь с моделью ещё не проверена/)
    assert.equal(button('Есть данные').disabled, true)
    await edit('')
    await click(button('Есть данные'))
    assert.match(input().value, /Данные находятся на сервере LoopLab: \[путь к данным\]/)
    assert.match(mounted.container.textContent, /План нового запуска/)
    await click(button('Вернуться в чат'))
    assert.equal(mounted.container.querySelector('.asst-run-starter'), null)
    assert.match(input().value, /\[путь к данным\]/, 'returning to chat keeps the editable draft')
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false,
      'preparing either example cannot create a session, message, command or run')
  } finally {
    await mounted.unmount()
    globalThis.fetch = harness.fetch
  }
})
