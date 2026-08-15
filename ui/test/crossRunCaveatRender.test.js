// THE SAME FINDING, RENDERED. `test/bestMetricCaveats.test.js` drives the reading rule and the
// leaderboard MODEL; this file is the other half — that the caveat reaches the cell beside the
// number. It is a separate file because it stands up a vite dev server and a JSDOM client root,
// which no pure-model test should have to pay for.
//
// See `bestMetricCaveats.test.js`'s header for the finding: `/api/runs` publishes `best_metric` and
// nothing else about the champion, while `metric_salvage: "select"` and `trust_gate: "audit"` (the
// DEFAULT) both let a caveated number be crowned. The decision here is CAVEAT, NOT UNRANK.
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

import { CHAMPION_CAVEAT_SALVAGED } from '../src/runIndex.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const run = (run_id, best_metric, extra = {}) => ({
  run_id, task_id: 'repo_task', direction: 'max', best_metric, best_confirmed: null, nodes: 4,
  finished: true, phase: 'finished', label: run_id, best_metric_caveats: [], ...extra,
})

const LEADER_IS_CAVEATED = [
  run('v6', 0.727991),
  run('v9', 0.81, { best_metric_caveats: [CHAMPION_CAVEAT_SALVAGED] }),
  run('v2', 0.224975),
]

let dom = null
let root = null
let vite = null
let requests = []
const previous = {}

async function panel(rows, state = { task_id: 'repo_task', direction: 'max' }, key = undefined) {
  if (!vite) {
    dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
      url: 'https://looplab.test/', pretendToBeVisual: true,
    })
    const installed = {
      window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
      location: dom.window.location, sessionStorage: dom.window.sessionStorage,
      MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
      requestAnimationFrame: cb => setTimeout(cb, 0), cancelAnimationFrame: h => clearTimeout(h),
      IS_REACT_ACT_ENVIRONMENT: true,
      fetch: (url, options = {}) => new Promise(resolve => {
        requests.push({ url: String(url), options, resolve })
      }),
    }
    for (const [name, value] of Object.entries(installed)) {
      previous[name] = Object.getOwnPropertyDescriptor(globalThis, name)
      Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const { createRoot } = await import('react-dom/client')
    root = createRoot(document.getElementById('root'))
  }
  const panels = await vite.ssrLoadModule('/src/panels.jsx')
  await act(async () => root.render(
    React.createElement(panels.CrossRunPanel, { key, state, onClose() {} })))
  const request = requests.at(-1)
  await act(async () => {
    request.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => rows })
    await Promise.resolve()
  })
  return document
}

after(async () => {
  if (root) await act(async () => root.unmount())
  if (vite) await vite.close()
  for (const [name, descriptor] of Object.entries(previous)) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
})

test('the leaderboard prints the caveat in the objective cell, beside the number it qualifies', async () => {
  const doc = await panel(LEADER_IS_CAVEATED)
  const rows = [...doc.querySelectorAll('tbody tr:not(.xr-group)')]
  assert.deepEqual(rows.map(r => r.textContent),
    ['#1v90.81salvaged4finished', '#2v60.7284finished', '#3v20.2254finished'],
    'the caveated run keeps rank #1 and its value, and the word rides in the objective cell')
  const pill = doc.querySelector('tbody .pill.warn')
  assert.ok(pill, 'the caveat is styled as a warning, like the integrity receipt on the run card')
  assert.match(pill.getAttribute('title') || '', /NOT measured/)
  assert.match(doc.body.textContent, /1 run\(s\) in this group publish a best metric their own run/)
  assert.match(doc.body.textContent, /One of them leads this group/)
})

test('NEGATIVE CONTROL, RENDERED: a measured leaderboard is byte-for-byte what it was', async () => {
  const doc = await panel([run('v6', 0.9), run('v2', 0.2)], undefined, 'measured')
  assert.deepEqual([...doc.querySelectorAll('tbody tr:not(.xr-group)')].map(r => r.textContent),
    ['#1v60.94finished', '#2v20.24finished'])
  assert.equal(doc.querySelector('tbody .pill.warn'), null, 'no marker on a measured row')
  assert.doesNotMatch(doc.body.textContent, /recorded a caveat about/)
  assert.doesNotMatch(doc.body.textContent, /salvaged|trust-flagged/)
})
