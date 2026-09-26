// An operator RETARGET (doc 68 68.2): rank every node by a declared extra metric instead of the
// task's own — the Metrics tab offers it where the server would accept it, names the key the ★ row
// is once one is in force, and says what that means for every decision taken before it.
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

import { CONTROL } from '../src/api.js'
import { STORED_ERROR_CODES, commandErrorMessage } from '../src/commandModel.js'
import {
  currentRetarget, objectiveKey, objectiveLabel, retargetBlocked, retargetableKeys,
} from '../src/objectiveModel.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
let dom = null, root = null, vite = null
const previous = {}

const node = (id, extras = {}, channels = {}, directions = {}, status = 'evaluated') => ({
  id, metric: 0.5, status, attempt: 0, extra_metrics: extras,
  extra_metrics_provenance: channels, extra_metrics_direction: directions, metric_provenance: {},
})
const run = (nodes, extra = {}) => ({
  direction: 'max', nodes: Object.fromEntries(nodes.map(n => [n.id, n])), best_node_id: nodes[0]?.id,
  ...extra,
})
const RETARGETED = { objective_key: 'filtered',
  objective_history: [{ seq: 9, key: 'filtered', previous: null }] }

// ------------------------------------------------------------------ the model

test('only a declared, finite key the run is not already ranked by is offered', () => {
  const state = run([
    node(0, { filtered: 0.3, printed: 0.9, nan: NaN }, { filtered: 'declared', printed: 'auto',
      nan: 'declared' }),
    node(1, { latency: 12 }, { latency: 'declared' }, { latency: 'min' }),
    node(2, { later: 0.4 }, { later: 'declared' }, {}, 'pending'),
    node(3, { unoriented: 1 }, { unoriented: 'declared' }, { unoriented: 'max' }),
  ])
  assert.deepEqual(retargetableKeys(state), ['filtered', 'unoriented'],
    'self-reported, non-finite, oriented against the run, and unevaluated keys are never offered')
  assert.deepEqual(retargetableKeys({ ...state, ...RETARGETED }), ['unoriented'])
})

test('a run with a holdout is never offered a retarget', () => {
  const nodes = [node(0, { filtered: 0.3 }, { filtered: 'declared' })]
  assert.equal(retargetBlocked(run(nodes)), null)
  assert.equal(retargetBlocked(run(nodes, { host_grading: { scorer: 'accuracy' } })), 'host-graded')
  assert.equal(retargetBlocked(run(nodes, { holdout_evaluated_ids: [0] })), 'holdout scored')
  assert.deepEqual(retargetableKeys(run(nodes, { holdout_evaluated_ids: [0] })), [])
  assert.equal(retargetBlocked(null), 'no run')
})

test('the objective in force names the star row and its own retarget', () => {
  assert.equal(objectiveKey({}), null)
  assert.equal(objectiveLabel({}), 'objective')
  assert.equal(objectiveLabel(RETARGETED), 'objective · filtered')
  assert.deepEqual(currentRetarget(RETARGETED), { seq: 9, key: 'filtered', previous: null })
  assert.equal(currentRetarget({ objective_key: null,
    objective_history: [{ seq: 9, key: 'filtered' }, { seq: 12, key: null }] }), null,
  'restored to the task metric: no retarget is in force')
})

// ------------------------------------------------------------------ the command and its words

const GEN = 'a'.repeat(64)
const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status, json: async () => body, headers: { get: () => null },
})
const posted = async (action) => {
  const calls = []
  const previous = { location: globalThis.location, fetch: globalThis.fetch,
    sessionStorage: globalThis.sessionStorage }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = { getItem: () => null }
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/lifecycle') && options.method == null) {
      return jsonResponse({ schema: 1, seq: 0, event_count: 1, generation: GEN, engine_running: false })
    }
    calls.push({ url, options })
    return jsonResponse({ id: `cmd_${'d'.repeat(32)}`, status: 'succeeded', event_type: 'metric_retarget' })
  }
  try { await action() } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
  return JSON.parse(calls.find(call => call.options.method === 'POST').options.body)
}

test('the command carries the key, null to restore, and a trimmed goal only when given', async () => {
  assert.deepEqual(await posted(() => CONTROL.retargetMetric('demo', 'filtered')), {
    type: 'metric_retarget', data: { key: 'filtered' }, expected_generation: GEN })
  assert.deepEqual((await posted(() => CONTROL.retargetMetric('demo', null))).data, { key: null })
  assert.deepEqual((await posted(() => CONTROL.retargetMetric('demo', ''))).data, { key: null })
  assert.deepEqual((await posted(() => CONTROL.retargetMetric('demo', 'f', { goal: '  g  ' }))).data,
    { key: 'f', goal: 'g' })
})

test('every refusal is a stored code with its own remedy', () => {
  for (const code of ['retarget_key_not_declared', 'retarget_direction_flip',
    'retarget_with_holdout', 'retarget_unchanged']) {
    assert.ok(STORED_ERROR_CODES.has(code), code)
    const restored = commandErrorMessage({ status: 'rejected', error: { code } })
    assert.doesNotMatch(restored, /Refresh state/, `${code}: ${restored}`)
  }
})

test('the feed says what changed, as an operator action', async () => {
  // Through Vite, as the narration model's own tests load it: the module graph reaches `.jsx`.
  const { eventNarration, kindOf } = await (await server()).ssrLoadModule('/src/narration.js')
  assert.equal(eventNarration({ type: 'metric_retarget', data: { key: 'filtered', goal: 'g' } }),
    'objective → filtered (operator retarget) — g')
  assert.equal(eventNarration({ type: 'metric_retarget', data: { key: null } }),
    "objective → the task's own metric")
  assert.equal(kindOf('metric_retarget').group, 'control')
})

// ------------------------------------------------------------------ the Metrics tab


async function server() {
  if (!vite) {
    dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
      { url: 'https://looplab.test/', pretendToBeVisual: true })
    const installed = {
      window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
      location: dom.window.location, sessionStorage: dom.window.sessionStorage,
      MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
      requestAnimationFrame: cb => setTimeout(cb, 0), cancelAnimationFrame: h => clearTimeout(h),
      IS_REACT_ACT_ENVIRONMENT: true, fetch: () => new Promise(() => {}),
    }
    for (const [k, v] of Object.entries(installed)) {
      previous[k] = Object.getOwnPropertyDescriptor(globalThis, k)
      Object.defineProperty(globalThis, k, { configurable: true, writable: true, value: v })
    }
    vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom',
      logLevel: 'silent', server: { middlewareMode: true } })
    const { createRoot } = await import('react-dom/client')
    root = createRoot(document.getElementById('root'))
  }
  return vite
}

async function render(state, props = {}) {
  const mod = await (await server()).ssrLoadModule('/src/Inspector.jsx')
  await act(async () => root.render(React.createElement(mod.Metrics, {
    n: Object.values(state.nodes)[0], detail: {}, state, runId: 'r', ...props,
  })))
  return document.getElementById('root')
}

after(async () => {
  if (root) await act(async () => root.unmount())
  if (vite) await vite.close()
  for (const [k, d] of Object.entries(previous)) {
    if (d) Object.defineProperty(globalThis, k, d); else delete globalThis[k]
  }
})

const buttons = el => [...el.querySelectorAll('button')].map(b => b.textContent.trim())
const NODES = () => [node(0, { filtered: 0.3, printed: 0.9 }, { filtered: 'declared', printed: 'auto' })]

test('a live tab offers the declared key, and only that one', async () => {
  const el = await render(run(NODES()), { canRetarget: true, onToast: () => {} })
  assert.deepEqual(buttons(el), ['rank by this'])
  const row = [...el.querySelectorAll('tr')].find(tr => tr.textContent.includes('filtered'))
  assert.ok(row.querySelector('button'), 'the button sits on the declared key\'s own row')
})

test('a read-only tab offers nothing, the way back included', async () => {
  for (const props of [{}, { canRetarget: false, onToast: () => {} }, { canRetarget: true }]) {
    const label = JSON.stringify(Object.keys(props))
    assert.deepEqual(buttons(await render(run(NODES()), props)), [], label)
    assert.deepEqual(buttons(await render(run(NODES(), RETARGETED), props)), [], label)
  }
})

test('under a retarget the star row names its key and the way back', async () => {
  const el = await render(run(NODES(), RETARGETED), { canRetarget: true, onToast: () => {} })
  assert.match(el.textContent, /★ objective · filtered/)
  assert.deepEqual(buttons(el), ['rank by the task metric'],
    'the key in force is not offered again; the task metric is')
  assert.match(el.textContent, /ranks by filtered since an operator retarget \(event #9\)/)
  assert.match(el.textContent, /every decision taken earlier was taken on that objective/)
  const plain = await render(run(NODES()), { canRetarget: true, onToast: () => {} })
  assert.doesNotMatch(plain.textContent, /operator retarget/)
  assert.match(plain.textContent, /★ objective/)
})
