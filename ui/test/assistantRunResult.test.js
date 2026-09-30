import test from 'node:test'
import assert from 'node:assert/strict'
import { click, fetchStub, mountLive, settle, unanswered, until } from './_mount.js'
import { parseRunRouteState } from '../src/runRouteState.js'

const generation = 'a'.repeat(64)
const receipt = { first: { node_id: 0, attempt: 0, value: 0.5, confirmed: false, seeds: null },
  selected: { node_id: 2, attempt: 1, value: 0.7, confirmed: true, seeds: 3 } }
const row = { run_id: 'demo', generation, finished: true, phase: 'finished', engine_running: false,
  direction: 'max', result_summary: receipt, source_integrity: { complete: true }, best_metric_caveats: [] }
let harness
let Card
test.before(async () => {
  harness = await mountLive()
  ;({ default: Card } = await harness.load('/src/AssistantRunResult.jsx'))
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
    assert.match(container.textContent, /mean from 3 seeds/)
    assert.match(container.textContent, /evaluation conditions, and confirmation/)
    assert.doesNotMatch(container.textContent, /Improvement:|robust|verified/)
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
    assert.match(container.textContent, /no multi-seed confirmation/)
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
    await until(() => mounted.container.querySelector('.asst-run-result button'), 'the finished result card')
    const ask = mounted.container.querySelector('.asst-run-result button')
    assert.equal(ask.disabled, false)
    await click(ask)
    const input = mounted.container.querySelector('[aria-label="Assistant message"]')
    assert.ok(input)
    assert.match(input.value, /Explain this run’s result/)
    assert.match(input.value, /Do not start another experiment/)
    assert.equal(document.activeElement, input)
    assert.equal(ask.disabled, true, 'the next click cannot overwrite a nonempty draft')
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false,
      'preparing the question cannot send a message or run command')
  } finally {
    await mounted.unmount()
    globalThis.fetch = harness.fetch
  }
})
