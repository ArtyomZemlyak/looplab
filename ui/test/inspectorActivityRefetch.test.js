// A change of a node's ACTIVITY re-reads its detail, even when its status did not move (doc 69
// 69.12b; critic 2026-09-30).
//
// The detail is re-polled only while the node works (`nodeWorking`), and `pending` is the status
// of every node from `node_created` to its terminal. So a pause that WITHHELD the evaluation
// (`evaluating` -> `queued`, status still `pending`) stopped the poll with the Inspector on the
// `evaluating` of its last fetch, and nothing re-read it: the Overview kept saying the sandbox owned
// a node the engine had given back. Driven, not pinned: the real Inspector is mounted with polling
// OFF (`live: null`), so the only thing that can issue a second read is the summary's activity
// moving. MUTATION: drop `summaryActivityKey` from the detail's `deps` -> one request, red.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React, { act } from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const EVALUATING = { schema: 1, status: 'evaluating', generation: 0, evidence: 'eval_started' }
const WITHHELD = { schema: 1, status: 'queued', generation: 0, evidence: 'eval_attempt_withheld' }

const detail = activity => ({
  id: 0, attempt: 0, status: 'pending', error: '', error_reason: null, metric: null,
  feasible: null, operator: 'draft', parent_ids: [], stages: [], failed_stage: null,
  code: 'print(1)', files: { 'solution.py': 'print(1)' }, trials: [],
  idea: { title: 'seed', rationale: 'seed rationale' }, trace: { nodes: [], projection: {} },
  activity,
})

const state = activity => ({
  run_id: 'demo', direction: 'max', best_node_id: null, drifts: [], node_concepts: {},
  nodes: { 0: { id: 0, attempt: 0, status: 'pending', error: '', error_reason: null,
    metric: null, operator: 'draft', parent_ids: [], activity } },
})

test('a withheld evaluation re-reads the node detail although its status stayed pending',
  async () => {
    const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
      url: 'https://looplab.test/', pretendToBeVisual: true,
    })
    const requests = []
    const installed = {
      window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
      location: dom.window.location, sessionStorage: dom.window.sessionStorage,
      requestAnimationFrame: callback => setTimeout(callback, 0),
      cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
      // Abort-aware, as in `inspectorDetailResource.test.js`: an unanswered request must reject on
      // unmount, or a red assertion here turns into a harness timeout.
      fetch: (url, options = {}) => new Promise((resolve, reject) => {
        requests.push({ url: String(url), resolve })
        options.signal?.addEventListener('abort',
          () => reject(new DOMException('The operation was aborted.', 'AbortError')))
      }),
    }
    const previous = Object.fromEntries(Object.keys(installed)
      .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
    let root
    let vite
    const reply = async (request, payload) => act(async () => {
      request.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => payload })
      await Promise.resolve()
    })
    const props = activity => ({
      runId: 'demo', nodeId: 0, state: state(activity), live: null, tab: 'Overview',
      setTab() {}, onToast() {},
    })
    try {
      for (const [key, value] of Object.entries(installed)) {
        Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
      }
      vite = await createServer({
        root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
        server: { middlewareMode: true },
      })
      const [{ createRoot }, { default: Inspector }] = await Promise.all([
        import('react-dom/client'), vite.ssrLoadModule('/src/Inspector.jsx'),
      ])
      root = createRoot(document.getElementById('root'))
      await act(async () => root.render(React.createElement(Inspector, props(EVALUATING))))
      assert.equal(requests.length, 1, 'the mount reads the detail once')
      await reply(requests[0], detail(EVALUATING))
      await act(async () => new Promise(resolve => setTimeout(resolve, 5)))

      // The same props again: nothing moved, so nothing is re-read — the deps are a CHANGE signal,
      // not a render signal.
      await act(async () => root.render(React.createElement(Inspector, props(EVALUATING))))
      assert.equal(requests.length, 1, 'an unchanged activity must not re-read the detail')

      // The pause withheld the evaluation: the status is still `pending`, only the activity moved.
      await act(async () => root.render(React.createElement(Inspector, props(WITHHELD))))
      assert.equal(requests.length, 2,
        'a withheld evaluation must re-read the detail, or the Inspector keeps `evaluating`')
      assert.equal(requests[1].url, requests[0].url, 'the SAME scope is re-read, never reset')
      await reply(requests[1], detail(WITHHELD))
      await act(async () => new Promise(resolve => setTimeout(resolve, 5)))
      assert.equal(requests.length, 2, 'one change, one re-read')
    } finally {
      if (root) await act(async () => root.unmount())
      if (vite) await vite.close()
      for (const [key, descriptor] of Object.entries(previous)) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor)
        else delete globalThis[key]
      }
      dom.window.close()
    }
  })
