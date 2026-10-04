import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, mountLive, settle, unanswered, until } from './_mount.js'
import { parseRunRouteState } from '../src/runRouteState.js'
import { node, payload } from './_resultNoticesFixtures.js'

const generation = 'a'.repeat(64)
const receipt = { first: { node_id: 0, attempt: 0, value: 0.5, confirmed: false, seeds: null },
  selected: { node_id: 2, attempt: 1, value: 0.7, confirmed: true, seeds: 3 } }
const row = { run_id: 'demo', generation, finished: true, phase: 'finished', engine_running: false,
  direction: 'max', result_summary: receipt, source_integrity: { complete: true }, best_metric_caveats: [] }
let harness
let Card
test.before(async () => {
  harness = await mountLive({ visible: true })
  ;({ default: Card } = await harness.load('/src/AssistantRunResult.jsx'))
})

test('result disclosure closes on a new run/generation and on read-only navigation', async () => {
  const { default: AssistantBar } = await harness.load('/src/AssistantBar.jsx')
  const { setRunAccess, clearRunAccess } = await harness.load('/src/runMode.js')
  let current = row
  const other = { ...row, run_id: 'other' }
  const backend = fetchStub({
    'GET /api/assistant/commands': { commands: [] },
    'GET /api/assistant/sessions': { sessions: [] },
    'GET /api/assistant/watches': { watches: [] },
    'GET /api/runs': () => [current, other],
    'GET /api/runs/demo/result-notices': payload([node]),
    'GET /api/assistant/permissions': ({ init }) => unanswered(init),
    'GET /api/assistant/progress': ({ init }) => unanswered(init),
  })
  globalThis.fetch = backend
  localStorage.clear()
  sessionStorage.clear()
  const mounted = await harness.mount(AssistantBar, { runId: 'demo' })
  const details = () => mounted.container.querySelector('.asst-result-details')
  const open = async () => {
    await React.act(async () => {
      details().open = true
      details().dispatchEvent(new Event('toggle'))
    })
    await until(() => details()?.querySelector('.asst-run-result'), 'visible current result')
  }
  const refresh = () => React.act(async () => document.dispatchEvent(new Event('visibilitychange')))
  try {
    const side = mounted.container.querySelector('button.cmdbar-drawer-btn')
    if (side) await click(side)
    await until(details, 'result disclosure')
    await open()
    const first = details()
    current = { ...row, generation: 'b'.repeat(64), result_summary: {
      ...receipt, selected: { ...receipt.selected, value: 0.9 } } }
    await refresh()
    await until(() => details() && details() !== first, 'new generation disclosure')
    assert.equal(details().open, false)
    assert.equal(details().querySelector('.asst-run-result'), null)
    await open()
    assert.match(details().textContent, /0\.9/)
    // A refresh within the same generation keeps the operator's disclosure open
    // and renders the current selected receipt rather than retaining old props.
    current = { ...current, result_summary: { ...receipt,
      selected: { ...receipt.selected, value: 0.8 } } }
    await refresh()
    await until(() => details()?.textContent.includes('0.8'), 'current receipt refresh')
    assert.equal(details().open, true)
    await mounted.rerender({ runId: 'other' })
    await until(details, 'other run disclosure')
    assert.equal(details().open, false)
    assert.equal(details().querySelector('.asst-run-result'), null)
    await open()
    await React.act(async () => setRunAccess('other', { readOnly: true, seq: 10 }))
    assert.equal(details(), null, 'history cannot retain a live result reader')
    await React.act(async () => clearRunAccess('other'))
    await until(details, 'restored live disclosure')
    assert.equal(details().open, false)
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
  } finally {
    await mounted.unmount()
    clearRunAccess('other')
    globalThis.fetch = harness.fetch
  }
})
test.after(async () => { await harness?.close() })

test('result is free to read, and navigation retains generation and exact node attempt', async () => {
  const opened = []
  let asked = 0
  const mounted = await harness.mount(Card, { run: row,
    onOpen: (event, href) => { event.preventDefault(); opened.push(href) }, onAsk: () => { asked += 1 } })
  try {
    const { container } = mounted
    assert.match(container.textContent, /higher is better/)
    assert.match(container.textContent, /confirmation mean.*3 repeat checks/)
    assert.match(container.textContent, /Different measurement types.*improvement is not established/)
    assert.match(container.textContent, /scores and repeat checks separately/)
    assert.match(container.textContent, /detector coverage is not fully verified/)
    assert.doesNotMatch(container.textContent, /Improvement:|robust/)
    const code = [...container.querySelectorAll('a')].find(a => a.textContent === 'Open selected code')
    const target = parseRunRouteState(code.getAttribute('href')).state
    assert.equal(target.generation, generation)
    assert.equal(target.nodeId, 2)
    assert.equal(target.nodeGeneration, 1)
    assert.equal(target.inspectTab, 'Code')
    await click(code)
    assert.equal(opened.length, 1)
    await click(container.querySelector('button'))
    assert.equal(asked, 1)
    assert.equal(harness.fetch.calls.length, 0, 'no model or detail request on reading or drafting')
    await mounted.rerender({ run: { ...row, best_metric_caveats: ['salvaged', 'new_flag'] } })
    assert.match(container.textContent, /salvaged.*new_flag/)
    await mounted.rerender({ run: { ...row, result_summary: {
      ...receipt, selected: { ...receipt.selected, confirmed: false, seeds: null } } } })
    assert.match(container.textContent, /No multi-seed confirmation/)
    assert.doesNotMatch(container.textContent, /Different measurement types/)
    await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'ru' })))
    await mounted.rerender({ run: { ...row, result_summary: { first: receipt.first, selected: receipt.first } } })
    assert.match(container.textContent, /Первый допустимый эксперимент/)
    assert.doesNotMatch(container.textContent, /пригодный для сравнения/)
    assert.match(container.textContent, /улучшение не установлено/)
    assert.match(container.textContent, /не обязательно является базовым решением задачи/)
  } finally { await mounted.unmount() }
})

test('partial sources, missing receipts, and finalization cannot become a completed result', async () => {
  const mounted = await harness.mount(Card, { run: row })
  try {
    for (const changed of [{ finished: false }, { engine_running: true }, { phase: 'finalizing' },
      { finalization_incomplete: true }]) {
      await mounted.rerender({ run: { ...row, ...changed } })
      assert.equal(mounted.container.textContent, '')
    }
    for (const changed of [{ source_integrity: { complete: false } }, { generation: null },
      { result_summary: null }, { result_summary: { ...receipt, selected: { ...receipt.selected, value: NaN } } }]) {
      await mounted.rerender({ run: { ...row, ...changed } })
      assert.equal(mounted.container.querySelectorAll('dd').length, 0)
      assert.equal([...mounted.container.querySelectorAll('a')].some(a => a.textContent === 'Open selected code'), false)
    }
  } finally { await mounted.unmount() }
})

test('Assistant drafts the result question, preserves an existing draft, and sends no command', async () => {
  const { default: AssistantBar } = await harness.load('/src/AssistantBar.jsx')
  const backend = fetchStub({
    'GET /api/assistant/commands': { commands: [] },
    'GET /api/assistant/sessions': { sessions: [] },
    'GET /api/assistant/watches': { watches: [] },
    'GET /api/runs': [row],
    'GET /api/runs/demo/result-notices': payload([node]),
    'GET /api/assistant/permissions': ({ init }) => unanswered(init),
    'GET /api/assistant/progress': ({ init }) => unanswered(init),
  })
  globalThis.fetch = backend
  localStorage.clear()
  sessionStorage.clear()
  const mounted = await harness.mount(AssistantBar, { runId: 'demo' })
  try {
    await settle()
    const side = mounted.container.querySelector('button.cmdbar-drawer-btn')
    if (side) await click(side)
    await until(() => mounted.container.querySelector('.asst-result-details'), 'the finished result disclosure')
    const disclosure = mounted.container.querySelector('.asst-result-details')
    assert.equal(disclosure.open, false)
    assert.equal(disclosure.querySelector('.asst-run-result'), null,
      'a hidden result must not mount or publish a ready/scroll effect')
    await React.act(async () => {
      disclosure.open = true
      disclosure.dispatchEvent(new Event('toggle'))
    })
    await until(() => mounted.container.querySelector('.asst-run-result button'), 'the opened result card')
    const ask = mounted.container.querySelector('.asst-run-result button')
    assert.equal(ask.disabled, false)
    await click(ask)
    const input = mounted.container.querySelector('[aria-label="Assistant message"]')
    assert.ok(input)
    assert.match(input.value, /Explain this run’s result/)
    assert.match(input.value, /Do not start another experiment/)
    assert.equal(document.activeElement, input)
    assert.equal(ask.disabled, true, 'the next click cannot overwrite a nonempty draft')
    await until(() => mounted.container.querySelector('.asst-result-notice button'), 'node result question')
    assert.equal(mounted.container.querySelector('.asst-result-notice button').disabled, true)
    const preserved = input.value
    await React.act(async () => window.dispatchEvent(new CustomEvent('ll:focus-assistant', {
      detail: { text: 'Review useful code changes' },
    })))
    assert.equal(input.value, preserved)
    assert.match(mounted.container.textContent, /Draft preserved — send or clear it first/)
    await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'ru' })))
    await React.act(async () => window.dispatchEvent(new CustomEvent('ll:focus-assistant', {
      detail: { text: 'Помоги выбрать изменение' },
    })))
    assert.equal(input.value, preserved)
    assert.match(mounted.container.textContent, /Черновик сохранён\. Сначала отправьте или очистите его/)
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false,
      'preparing the question cannot send a message or run command')
    await React.act(async () => {
      disclosure.open = false
      disclosure.dispatchEvent(new Event('toggle'))
    })
    assert.equal(disclosure.querySelector('.asst-run-result'), null)
    assert.equal(input.value, preserved, 'closing a result cannot clear its drafted question')
  } finally {
    await mounted.unmount()
    globalThis.fetch = harness.fetch
  }
})
