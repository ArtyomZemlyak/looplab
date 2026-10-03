import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, jsonResponse, mountLive, until } from './_mount.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'

let harness, Results
test.before(async () => {
  harness = await mountLive()
  ;({ default: Results } = await harness.load('/src/AssistantResults.jsx'))
})
test.after(async () => { await harness?.close() })
test.beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
const records = () => Array.from({ length: 205 }, (_, id) => ({ ...node, id: `node:${id}:0`,
  node_id: id, attempt: 0, score: id, evidence_token: (id + 1).toString(16).padStart(64, '0'),
  parents: [], score_comparison: { version: 1, parent_count: 0, status: 'no_parent' },
  commentary: `Interpretation ${id}` }))
const scope = 'd'.repeat(64)
const anchor = row => `rn1.${scope}.${row.id}.${row.evidence_token}`
const page = (rows, cursor, gen = generation) => {
  const end = cursor ? rows.findIndex(row => anchor(row) === cursor) : rows.length
  assert.ok(end >= 0, 'fixture cursor names an existing receipt')
  const start = Math.max(0, end - 50)
  const items = rows.slice(start, end)
  return { ...payload(items), generation: gen, total: rows.length, has_more: start > 0,
    next_cursor: start > 0 ? anchor(items[0]) : null }
}
const button = (view, text) => [...view.container.querySelectorAll('button')].find(row => row.textContent === text)

test('all 205 current results and commentary are readable in chat despite a new completion during paging', async () => {
  const rows = records(), readCursors = []
  let added = false, ready = 0
  globalThis.fetch = fetchStub({ 'GET /api/runs/demo/result-notices': ({ url }) => {
    assert.equal(url.searchParams.get('expected_generation'), generation)
    assert.equal(url.searchParams.get('limit'), '50')
    const cursor = url.searchParams.get('cursor')
    readCursors.push(cursor)
    if (cursor && !added) {
      rows.push({ ...rows[204], id: 'node:205:0', node_id: 205, evidence_token: 'e'.repeat(64), commentary: 'Newest completion' })
      added = true
    }
    return page(rows, cursor)
  } })
  const view = await harness.mount(Results, { runId: 'demo', generation, onReady: () => { ready++ } })
  try {
    await until(() => view.container.querySelectorAll('article').length === 50, 'latest page')
    const seen = new Set()
    const capture = () => [...view.container.querySelectorAll('article')].forEach(row => {
      const text = row.textContent.match(/Interpretation (\d+)/)
      assert.ok(text, 'the same chat page includes external commentary')
      seen.add(Number(text[1]))
    })
    capture()
    for (const count of [50, 50, 50, 5]) {
      const calls = readCursors.length
      await click(button(view, 'Earlier'))
      await until(() => readCursors.length > calls && view.container.querySelectorAll('article').length === count,
        'older page loaded')
      assert.equal(view.container.querySelector('details').open, true, 'older entries are expanded for reading')
      capture()
    }
    assert.equal(seen.size, 205)
    assert.equal(Math.min(...seen), 0); assert.equal(Math.max(...seen), 204)
    assert.equal(new Set(readCursors).size, 5, 'each page uses its own evidence anchor')
    assert.equal(button(view, 'Earlier').disabled, true)
    assert.equal(ready, 1, 'paging never asks the transcript to autoscroll')
    assert.doesNotMatch(view.container.textContent, /Full history is in Events/)
    await click(button(view, 'Newer'))
    await until(() => view.container.querySelectorAll('article').length === 50, 'newer page')
    await click(button(view, 'Latest results'))
    await until(() => view.container.textContent.includes('Newest completion'), 'refresh latest after draining')
    assert.match(view.container.textContent, /50 \/ 206/)
    assert.equal(ready, 2)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await view.unmount() }
})

test('an older page refreshes changed commentary, then withdraws an invalidated cursor and recovers explicitly', async t => {
  const rows = records()
  let fault = false
  globalThis.fetch = fetchStub({ 'GET /api/runs/demo/result-notices': ({ url }) => {
    const cursor = url.searchParams.get('cursor')
    if (cursor && fault) return jsonResponse({ detail: { code: 'result_notice_cursor_changed' } }, 409)
    return page(rows, cursor)
  } })
  t.mock.timers.enable({ apis: ['setInterval'] })
  const view = await harness.mount(Results, { runId: 'demo', generation })
  try {
    await until(() => button(view, 'Earlier')?.disabled === false, 'latest page ready')
    await click(button(view, 'Earlier'))
    await until(() => view.container.textContent.includes('Interpretation 120'), 'older interpretation')
    rows[120] = { ...rows[120], evidence_token: 'f'.repeat(64), commentary: null, score: -5 }
    await React.act(async () => { t.mock.timers.tick(5000) })
    await until(() => !view.container.textContent.includes('Interpretation 120'), 'superseded interpretation withdrawn')
    assert.match(view.container.textContent, /Experiment #120/)
    fault = true
    await React.act(async () => { t.mock.timers.tick(5000) })
    await until(() => view.container.textContent.includes('Results changed'), 'cursor change shown')
    assert.equal(view.container.querySelectorAll('article').length, 0, 'last-good evidence is not shown as current')
    assert.equal(button(view, 'Earlier').disabled, true)
    await click(button(view, 'Latest results'))
    await until(() => view.container.textContent.includes('Interpretation 204'), 'explicit latest read')
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await view.unmount() }
})

test('generation change clears cursor navigation and ignores an old page reply; language keeps the current page', async () => {
  const rows = records(), newGen = 'c'.repeat(64)
  let release, olderSignal
  globalThis.fetch = fetchStub({ 'GET /api/runs/demo/result-notices': ({ url, init }) => {
    const gen = url.searchParams.get('expected_generation')
    if (gen === newGen) {
      assert.equal(url.searchParams.has('cursor'), false, 'new incarnation starts with latest')
      return { ...payload([{ ...node, commentary: 'Replacement run' }]), generation: newGen }
    }
    const cursor = url.searchParams.get('cursor')
    if (cursor) {
      olderSignal = init.signal
      return new Promise(resolve => { release = () => resolve(page(rows, cursor)) })
    }
    return page(rows, cursor)
  } })
  const view = await harness.mount(Results, { runId: 'demo', generation })
  try {
    await until(() => button(view, 'Earlier')?.disabled === false, 'latest ready')
    await click(button(view, 'Earlier'))
    await until(() => release, 'older read in flight')
    await React.act(async () => {
      window.dispatchEvent(new window.CustomEvent('looplab:language', { detail: 'ru' }))
    })
    assert.ok(button(view, 'К последним итогам'), 'language does not discard navigation')
    await view.rerender({ runId: 'demo', generation: newGen })
    await until(() => view.container.textContent.includes('Replacement run'), 'new generation ready')
    assert.equal(olderSignal.aborted, true)
    await React.act(async () => { release() })
    assert.doesNotMatch(view.container.textContent, /Interpretation 1|К последним итогам|Latest results/)
    assert.match(view.container.querySelector('article a').getAttribute('href'), new RegExp(newGen))
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await view.unmount() }
})

test('incomplete pagination and an oversized HTTP 200 withdraw evidence until an explicit valid retry', async t => {
  const rows = records()
  let fault = ''
  globalThis.fetch = fetchStub({ 'GET /api/runs/demo/result-notices': () => {
    const latest = page(rows, null)
    if (fault === 'hidden') return { ...latest, has_more: false, next_cursor: null }
    if (fault === 'oversized') {
      const items = rows.slice(-51)
      return { ...latest, items, next_cursor: anchor(items[0]) }
    }
    return latest
  } })
  t.mock.timers.enable({ apis: ['setInterval'] })
  const view = await harness.mount(Results, { runId: 'demo', generation })
  try {
    await until(() => view.container.querySelectorAll('article').length === 50, 'latest page ready')
    fault = 'hidden'
    await React.act(async () => { t.mock.timers.tick(5000) })
    await until(() => view.container.textContent.includes('Could not refresh'), 'inconsistent pagination refused')
    assert.equal(view.container.querySelectorAll('article').length, 0)
    assert.equal(button(view, 'Earlier').disabled, true)
    fault = 'oversized'
    await click(button(view, 'Retry'))
    await until(() => button(view, 'Retry')?.disabled === false, 'oversized page settled')
    assert.equal(view.container.querySelectorAll('article').length, 0, 'server cannot enlarge the requested page')
    fault = ''
    await click(button(view, 'Retry'))
    await until(() => view.container.querySelectorAll('article').length === 50, 'valid page recovered')
    assert.equal(button(view, 'Earlier').disabled, false)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally { await view.unmount() }
})
