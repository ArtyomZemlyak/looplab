// doc 74 (the entry-barrier inspection): the first screens a new user sees, driven live.
//
//   * EB-24 — an empty installation offers the offline demo as an ordinary launch card over the
//     fixed spec, and opening it makes no write (validation and start stay the user's two clicks);
//   * EB-19 — a small portfolio keeps the run list plain (at most 15 visible buttons), one button
//     brings the tools back, and five runs show them unasked;
//   * EB-18 — the model screen's one line about the API key, as a truth table, and no store
//     vocabulary shown until the details are opened.
import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, mountLive, settle, until } from './_mount.js'

let harness
let RunList
let settings

test.before(async () => {
  harness = await mountLive({ visible: true })
  ;({ default: RunList } = await harness.load('/src/RunList.jsx'))
  settings = await harness.load('/src/Settings.jsx')
})

test.after(async () => { await harness?.close() })

const row = id => ({
  run_id: id, label: id, task_id: 'toy_quadratic', direction: 'min', best_metric: 1.5,
  best_confirmed: null, nodes: 6, finished: true, phase: 'finished', mtime: 1_700_000_000,
  best_metric_caveats: [], best_metric_comparability: null,
})
const buttonNamed = (container, name) => [...container.querySelectorAll('button')]
  .find(button => button.textContent.trim() === name)

async function list(rows) {
  const backend = fetchStub({
    '/api/runs': rows,
    '/api/projects': { projects: [], assignments: {} },
    '/api/supertasks': { supertasks: [], assignments: {} },
  })
  globalThis.fetch = backend
  sessionStorage.clear(); localStorage.clear()
  const view = await harness.mount(RunList, { onOpen() {}, onGlobalNavigate() {} })
  return { view, backend }
}

test('an empty installation offers the offline demo as a launch card, and opening it writes nothing', async () => {
  const { view, backend } = await list([])
  try {
    await until(() => buttonNamed(view.container, 'Try the offline demo — no model needed'), 'demo button')
    await React.act(async () => { buttonNamed(view.container, 'Try the offline demo — no model needed').click() })
    await until(() => view.container.querySelector('.offline-demo form.asst-launch'), 'demo launch card')
    assert.match(view.container.querySelector('.offline-demo').textContent, /offline-demo/)
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false,
      'opening the demo validates and starts nothing on its own')
  } finally { await view.unmount() }
})

test('the demo card outlives the landing: the started run being listed does not unmount it', async () => {
  // The run is listed as soon as its directory exists — possibly before startup is proven — which
  // ends the landing. The card used to live INSIDE the landing and vanished with its receipt.
  let listed = []
  const { view } = await list(() => listed)
  try {
    await until(() => buttonNamed(view.container, 'Try the offline demo — no model needed'), 'demo button')
    await React.act(async () => { buttonNamed(view.container, 'Try the offline demo — no model needed').click() })
    await until(() => view.container.querySelector('.offline-demo form.asst-launch'), 'demo launch card')
    const card = view.container.querySelector('.offline-demo form.asst-launch')
    listed = [row('offline-demo')]
    await until(() => !view.container.textContent.includes('Start with a goal'),
      'the next list poll ends the landing', { ceilingMs: 10_000 })
    assert.equal(view.container.querySelector('.offline-demo form.asst-launch'), card,
      'the same card instance is still mounted after the landing gave way')
  } finally { await view.unmount() }
})

test('a small portfolio keeps the list plain until asked; five runs show the tools unasked', async () => {
  const small = await list([row('a'), row('b')])
  try {
    await until(() => buttonNamed(small.view.container, 'Show filters, views and projects'), 'compact list')
    assert.equal(small.view.container.querySelector('select[aria-label="Saved portfolio view"]'), null)
    assert.equal(small.view.container.querySelector('input[aria-label="Filter runs"]'), null)
    // The acceptance itself (doc 74 EB-19, as corrected in its section 12.4): at 1-4 runs the list
    // workspace shows at most 15 buttons. A button inside a closed <details> or a hidden subtree is
    // not shown; the measured figure when the criterion was set was 6.
    const shown = [...small.view.container.querySelectorAll('button')]
      .filter(button => !button.closest('[hidden], details:not([open]) > :not(summary)'))
    assert.ok(shown.length <= 15, `${shown.length} visible buttons: ${shown.map(b => b.textContent.trim()).join(' | ')}`)
    await React.act(async () => {
      buttonNamed(small.view.container, 'Show filters, views and projects').click()
    })
    await until(() => small.view.container.querySelector('input[aria-label="Filter runs"]'), 'tools shown')
    assert.ok(small.view.container.querySelector('select[aria-label="Saved portfolio view"]'))
  } finally { await small.view.unmount() }

  const five = await list(['a', 'b', 'c', 'd', 'e'].map(row))
  try {
    await until(() => five.view.container.querySelector('input[aria-label="Filter runs"]'), 'full chrome at five runs')
    assert.equal(buttonNamed(five.view.container, 'Show filters, views and projects'), undefined)
  } finally { await five.view.unmount() }
})

test('tools once shown stay shown: the portfolio shrinking below five runs does not hide them', async () => {
  let rows = ['a', 'b', 'c', 'd', 'e'].map(row)
  const { view, backend } = await list(() => rows)
  const listReads = () => backend.calls.filter(call => call.path === '/api/runs').length
  try {
    await until(() => view.container.querySelector('input[aria-label="Filter runs"]'), 'full chrome at five runs')
    rows = rows.slice(0, 4)
    const before = listReads()
    await until(() => listReads() > before, 'the next list poll', { ceilingMs: 10_000 })
    await settle()
    assert.ok(view.container.querySelector('input[aria-label="Filter runs"]'),
      'the filter the operator may be typing in is still mounted')
    assert.equal(buttonNamed(view.container, 'Show filters, views and projects'), undefined)
  } finally { await view.unmount() }
})

test('after the first run the demo is still one button away, under a fresh run id (doc 75 UX-30)', async () => {
  const { view, backend } = await list([row('offline-demo')])
  try {
    await until(() => buttonNamed(view.container, 'Offline demo'), 'the demo button beside New run')
    await React.act(async () => { buttonNamed(view.container, 'Offline demo').click() })
    await until(() => view.container.querySelector('.offline-demo form.asst-launch'), 'demo launch card')
    assert.match(view.container.querySelector('.offline-demo').textContent, /offline-demo-2/)
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false, 'opening it writes nothing')
  } finally { await view.unmount() }
})

test('the model screen says one plain line about the API key', () => {
  const line = settings.credentialKeySummary
  assert.equal(line(null), 'API key: not set. Local endpoints usually need none.')
  assert.equal(line({ effective: false, active: false }), 'API key: not set. Local endpoints usually need none.')
  assert.equal(line({ effective: true, active: true }), 'API key: saved for this base URL.')
  assert.equal(line({ effective: true, active: false }), 'API key: saved, but not for this base URL.')
})

test('the model screen shows no credential-store vocabulary until its details are opened', async () => {
  // The acceptance itself (doc 74 EB-18): by default the model screen does not say "material" or
  // "shared key". The store's Yes/No chips stay in the DOM, inside a closed <details>.
  const shownText = container => {
    const copy = container.cloneNode(true)
    for (const hidden of copy.querySelectorAll('details:not([open]) > :not(summary)')) hidden.remove()
    return copy.textContent
  }
  for (const credential of [
    { stored: false, effective: false, active: false, source: 'none', status: 'missing' },
    { stored: true, effective: true, active: true, source: 'stored', status: 'active' },
  ]) {
    const view = await harness.mount(settings.CredentialState, { credential })
    try {
      const text = shownText(view.container)
      assert.match(text, /^API key: /)
      assert.doesNotMatch(text, /material|shared key/i, text)
      assert.match(view.container.textContent, /Stored material/, 'the details are still there on request')
    } finally { await view.unmount() }
  }
})
