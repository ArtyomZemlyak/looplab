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
    assert.equal(button(mounted, 'Начать запуск').disabled, true)
    assert.equal(calls.calls.length, 0)
    await click(button(mounted, 'Проверить — бесплатно'))
    await until(() => mounted.container.textContent.includes('Проверяем план…'), 'validation in flight')
    assert.equal(button(mounted, 'Начать запуск').disabled, true)
    complete()
    await until(() => mounted.container.textContent.includes('Дальше: проверить условия и запустить'), 'checked plan')
    assert.equal(button(mounted, 'Начать запуск').disabled, false)
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
    await until(() => button(mounted, 'Проверить — бесплатно')?.disabled === false, 'hydrated card')
    await click(button(mounted, 'Проверить — бесплатно'))
    await until(() => mounted.container.textContent.includes('Дальше: исправить отмеченную проблему'), 'rejected preview')
    assert.equal(button(mounted, 'Начать запуск').disabled, true)
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
    await until(() => button(mounted, 'Проверить — бесплатно')?.disabled === false, 'hydrated card')
    await click(button(mounted, 'Проверить — бесплатно'))
    await until(() => button(mounted, 'Начать запуск')?.disabled === false, 'valid plan')
    await click(button(mounted, 'Начать запуск'))
    await until(() => mounted.container.textContent.includes('Дальше: проверить прежний запуск'), 'unknown startup')
    const first = calls.calls.find(row => row.path === '/api/start')
    assert.ok(first)
    await mounted.unmount()
    mounted = await harness.mount(Card, { spec, language: 'en' })
    await until(() => mounted.container.textContent.includes('Next: check the previous startup'), 'saved recovery')
    await mounted.rerender({ spec, language: 'ru' })
    await until(() => button(mounted, 'Проверить запуск'), 'translated recovery action')
    await click(button(mounted, 'Проверить запуск'))
    await until(() => calls.calls.some(row => row.method === 'GET'), 'receipt read')
    assert.equal(calls.calls.filter(row => row.path === '/api/start').length, 1)
    assert.equal(receiptHeaders.get('Idempotency-Key'), JSON.parse(first.body).idempotency_key)
    assert.equal(button(mounted, 'Начать запуск'), undefined)
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
    assert.equal(button(mounted, 'Начать запуск').disabled, true)
    assert.equal(button(mounted, 'Проверить — бесплатно').disabled, true)
    await click(button(mounted, 'Проверить — бесплатно'))
    assert.equal(calls.calls.length, 0)
  } finally {
    await mounted.unmount()
    guard.releasePublisher(publisher)
  }
})

test('Russian controls preserve raw contracts, boolean values, edits and LLM cost across language changes', async () => {
  const proposal = { ...spec,
    task: { kind: 'repo', goal: 'Start run', direction: 'max', editable_path: 'Backend',
      edit_surface: ['Model'], protect: ['Protected'], cmd: { command: ['python', 'score.py'], metric: { key: 'Score' } },
      data: { Goal: { path: 'Code or data', edit: true } } },
    settings: { ...spec.settings, backend: 'llm', llm_model: 'Start run',
      card_driven_selection: true, custom: { untouched: 'Use inherited value' } },
  }
  const calls = fetchStub({ 'POST /api/start/preflight': ({ init }) => {
    const body = JSON.parse(init.body)
    const checked = preview(body)
    checked.preview.settings.backend = 'llm'
    checked.preview.settings.llm_model = body.settings.llm_model
    return checked
  } })
  globalThis.fetch = calls
  const mounted = await harness.mount(Card, { spec: proposal, language: 'ru' })
  try {
    await until(() => button(mounted, 'Изменить параметры плана'), 'Russian controls')
    const form = mounted.container.querySelector('form')
    const facts = [...form.querySelectorAll('.asst-launch-decision dd')].map(row => row.textContent)
    assert.equal(facts[0], 'Start run', 'authored goal matching a UI string is not translated')
    assert.equal(facts[1], 'Score · больше — лучше', 'only the generated metric direction is translated')
    assert.ok(facts.includes('Backend, Code or data'))
    assert.ok(facts.includes('Protected'))
    assert.ok(facts.includes('Goal можно менять; остальные — только читать.'))
    await click(button(mounted, 'Изменить параметры плана'))
    const taskJson = form.querySelector('textarea[id$="-task"]')
    const rawTask = taskJson.value
    const boolean = form.querySelector('select[id$="-settings-card_driven_selection"]')
    assert.equal(boolean.value, 'enabled')
    assert.equal(boolean.selectedOptions[0].textContent, 'Включить')
    await React.act(async () => {
      boolean.value = 'disabled'
      boolean.dispatchEvent(new window.Event('change', { bubbles: true }))
    })
    await edit(form.querySelector('input[id$="-run_id"]'), 'edited-russian-plan')
    const settingsJson = form.querySelector('textarea[id$="-advanced-settings"]')
    const rawSettings = settingsJson.value
    assert.equal(JSON.parse(rawSettings).card_driven_selection, false)
    assert.deepEqual(JSON.parse(rawSettings).custom, proposal.settings.custom)
    await click(button(mounted, 'Проверить — бесплатно'))
    await until(() => button(mounted, 'Начать запуск')?.disabled === false, 'checked Russian plan')
    assert.match(form.textContent, /Лимит расходов в деньгах не задан/)
    const sent = JSON.parse(calls.calls[0].body)
    assert.deepEqual(sent.task, proposal.task)
    assert.equal(sent.settings.backend, 'llm')
    assert.equal(sent.settings.llm_model, 'Start run')
    assert.equal(sent.settings.card_driven_selection, false)
    await mounted.rerender({ spec: proposal, language: 'en' })
    assert.equal(mounted.container.querySelector('form'), form)
    assert.equal(form.querySelector('textarea[id$="-task"]'), taskJson)
    assert.equal(taskJson.value, rawTask)
    assert.equal(settingsJson.value, rawSettings)
    assert.equal(form.querySelector('input[id$="-run_id"]').value, 'edited-russian-plan')
    assert.equal(button(mounted, 'Start run').disabled, false)
    assert.match(form.textContent, /No monetary cap is configured/)
    assert.equal(calls.calls.length, 1, 'language switch sends no request or new startup')
  } finally { await mounted.unmount() }
})

test('a Russian task-file card keeps its path and uses the authoritative resolved task after validation', async () => {
  const fileSpec = { ...spec, task: undefined, task_file: 'C:/tasks/Start run.yaml' }
  const calls = fetchStub({ 'POST /api/start/preflight': ({ init }) => {
    const body = JSON.parse(init.body)
    const checked = preview(body)
    Object.assign(checked.preview, { source: 'task_file', source_task_file: body.task_file,
      task: { kind: 'quadratic', goal: 'resolved file goal', direction: 'min' } })
    return checked
  } })
  globalThis.fetch = calls
  const mounted = await harness.mount(Card, { spec: fileSpec, language: 'ru' })
  try {
    await until(() => button(mounted, 'Проверить — бесплатно')?.disabled === false, 'Russian file card')
    assert.match(mounted.container.textContent, /Start run.yaml/)
    assert.match(mounted.container.textContent, /Проверьте план, чтобы увидеть цель, метрику/)
    await click(button(mounted, 'Проверить — бесплатно'))
    await until(() => button(mounted, 'Начать запуск')?.disabled === false, 'resolved file preview')
    assert.match(mounted.container.textContent, /resolved file goal/)
    await click(button(mounted, 'Изменить параметры плана'))
    const path = mounted.container.querySelector('input[id$="-task_file"]')
    assert.equal(path.value, fileSpec.task_file)
    await mounted.rerender({ spec: fileSpec, language: 'en' })
    assert.equal(mounted.container.querySelector('input[id$="-task_file"]'), path)
    assert.equal(path.value, fileSpec.task_file)
    assert.equal(button(mounted, 'Start run').disabled, false)
    const sent = JSON.parse(calls.calls[0].body)
    assert.equal(sent.task_file, fileSpec.task_file)
    assert.equal(Object.hasOwn(sent, 'task'), false)
    assert.equal(calls.calls.length, 1)
  } finally { await mounted.unmount() }
})
