import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, jsonResponse, mountLive, until } from './_mount.js'

let harness, Card, guard
test.before(async () => {
  harness = await mountLive()
  ;({ default: Card } = await harness.load('/src/LaunchCard.jsx'))
  guard = await harness.load('/src/settingsLaunchGuard.js')
})
test.after(async () => { await harness?.close() })
test.beforeEach(() => { localStorage.clear(); sessionStorage.clear(); location.hash = '' })
const spec = { proposal_id: 'guide-demo', run_id: 'guide-demo',
  task: { kind: 'quadratic', goal: 'min (x-3)^2', direction: 'min' },
  settings: { backend: 'toy', max_nodes: 3, max_seconds: 30 } }
const preview = body => ({ ok: true, validation_token: 'checked-proposal', warnings: [], preview: {
  run_id: body.run_id, source: 'inline', source_task_file: null, task: body.task,
  referenced_paths: [], settings: { backend: 'toy', llm_model: 'local-model', max_nodes: 3,
    n_seeds: 1, max_parallel: 1, parallel_build: 0, eval_parallel: 1, llm_parallel: 1,
    max_seconds: 30, max_eval_seconds: 10 },
} })
const button = (mounted, name) => [...mounted.container.querySelectorAll('button')]
  .find(row => row.textContent === name)
const edit = (input, value) => React.act(async () => {
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value)
  input.dispatchEvent(new window.Event('input', { bubbles: true }))
})

test('next step follows authoritative validation, language and later edits without starting', async () => {
  let complete
  const calls = fetchStub({ 'POST /api/start/preflight': ({ init }) => new Promise(resolve => {
    complete = () => resolve(jsonResponse(preview(JSON.parse(init.body))))
  }) })
  globalThis.fetch = calls
  const mounted = await harness.mount(Card, { spec, language: 'ru' })
  try {
    await until(() => mounted.container.textContent.includes('Дальше: проверить план'), 'Russian review guidance')
    assert.equal(button(mounted, 'Start run').disabled, true)
    assert.equal(calls.calls.length, 0)
    await click(button(mounted, 'Validate — free'))
    await until(() => mounted.container.textContent.includes('Проверяем план…'), 'validation in flight')
    assert.equal(button(mounted, 'Start run').disabled, true)
    complete()
    await until(() => mounted.container.textContent.includes('Дальше: проверить условия и запустить'), 'checked plan')
    assert.equal(button(mounted, 'Start run').disabled, false)
    await mounted.rerender({ spec, language: 'en' })
    assert.match(mounted.container.textContent, /Next: review the checked plan and start/)
    assert.equal(button(mounted, 'Start run').disabled, false, 'language does not invalidate the checked plan')
    await click(button(mounted, 'Edit proposal details'))
    await edit(mounted.container.querySelector('input[id$="-run_id"]'), 'edited-demo')
    await until(() => mounted.container.textContent.includes('Next: check the proposal'), 'edited plan')
    assert.equal(button(mounted, 'Start run').disabled, true)
    assert.deepEqual(calls.calls.map(row => `${row.method} ${row.path}`), ['POST /api/start/preflight'])
  } finally { await mounted.unmount() }
})

test('an incomplete validation result explains the problem and cannot advertise a ready launch', async () => {
  const calls = fetchStub({ 'POST /api/start/preflight': { ok: true, validation_token: 'partial' } })
  globalThis.fetch = calls
  const mounted = await harness.mount(Card, { spec, language: 'ru' })
  try {
    await until(() => button(mounted, 'Validate — free')?.disabled === false, 'hydrated card')
    await click(button(mounted, 'Validate — free'))
    await until(() => mounted.container.textContent.includes('Дальше: исправить отмеченную проблему'), 'rejected preview')
    assert.equal(button(mounted, 'Start run').disabled, true)
    assert.doesNotMatch(mounted.container.textContent, /Дальше: проверить условия и запустить/)
    assert.equal(calls.calls.length, 1)
  } finally { await mounted.unmount() }
})

test('an unknown startup is recovered by GET under its original key, including after remount and language change', async () => {
  let receiptHeaders
  const calls = fetchStub({
    'POST /api/start/preflight': ({ init }) => preview(JSON.parse(init.body)),
    'POST /api/start': () => jsonResponse({ message: 'reply lost' }, 503),
    'GET /api/start/guide-demo/status': ({ init }) => {
      receiptHeaders = new Headers(init.headers)
      return { run_id: 'guide-demo', ok: false, status: 'uncertain',
        started: false, can_retry: false, paid_effect_unknown: true }
    },
  })
  globalThis.fetch = calls
  let mounted = await harness.mount(Card, { spec, language: 'ru' })
  try {
    await until(() => button(mounted, 'Validate — free')?.disabled === false, 'hydrated card')
    await click(button(mounted, 'Validate — free'))
    await until(() => button(mounted, 'Start run')?.disabled === false, 'valid plan')
    await click(button(mounted, 'Start run'))
    await until(() => mounted.container.textContent.includes('Дальше: проверить прежний запуск'), 'unknown startup')
    const first = calls.calls.find(row => row.path === '/api/start')
    assert.ok(first)
    await mounted.unmount()
    mounted = await harness.mount(Card, { spec, language: 'en' })
    await until(() => mounted.container.textContent.includes('Next: check the previous startup'), 'saved recovery')
    await mounted.rerender({ spec, language: 'ru' })
    await click(button(mounted, 'Check startup'))
    await until(() => calls.calls.some(row => row.method === 'GET'), 'receipt read')
    assert.equal(calls.calls.filter(row => row.path === '/api/start').length, 1)
    assert.equal(receiptHeaders.get('Idempotency-Key'), JSON.parse(first.body).idempotency_key)
    assert.equal(button(mounted, 'Start run'), undefined)
    assert.doesNotMatch(mounted.container.textContent, /Дальше: проверить условия и запустить/)
  } finally { await mounted.unmount() }
})

test('settings recovery takes priority over a normal plan and cannot call preflight', async () => {
  const publisher = guard.claimPublisher()
  guard.publish(publisher, { active: true, blocked: true, status: 'recovery', reason: 'Saved settings need reconciliation.' })
  const calls = fetchStub({})
  globalThis.fetch = calls
  const mounted = await harness.mount(Card, { spec, language: 'ru', onOpenSettings() {} })
  try {
    await until(() => mounted.container.textContent.includes('Дальше: разобраться с настройками'), 'settings guidance')
    assert.match(mounted.container.textContent, /Saved settings need reconciliation/)
    assert.equal(button(mounted, 'Start run').disabled, true)
    assert.equal(button(mounted, 'Validate — free').disabled, true)
    await click(button(mounted, 'Validate — free'))
    assert.equal(calls.calls.length, 0)
  } finally {
    await mounted.unmount()
    guard.releasePublisher(publisher)
  }
})
