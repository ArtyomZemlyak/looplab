// A status and an error shown together must come from the SAME snapshot. Reported by the owner
// against runs/rubert-dr-0807: a node that was genuinely training showed a Lightning DDP failure and
// a status that did not match it, and switching away and back cleared it. A stale error beside a
// fresh status is worse than either alone, because it invents a failure on a healthy node.
//
// Driven, not pinned. Source cannot tell a correct scope fence from one that is bypassed by the
// render that happens BETWEEN the prop change and the effect that services it — which is exactly
// where this class of defect lives, and why `resourceModel.js::resourceView` exists. So this mounts
// the real Inspector, changes the selected node, and reads the rendered text on both the immediate
// render and the settled one, for EVERY tab: the previous node's payload reaching the screen is a
// per-tab property (each tab renders different fields of it, and only some of them are keyed).
//
// The markers are deliberately field-diverse — a failure message, a metric, source text, an idea
// rationale — because a resource can be fenced for the field a single-marker test happens to read
// while leaking another that a differently-keyed child holds.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const LIGHTNING = 'RuntimeError: It looks like your LightningModule has parameters'
const ZERO_CODE = 'ZERO_ONLY_SOURCE_MARKER'
const ZERO_METRIC = 4242.4242
const ZERO_RATIONALE = 'zero-only rationale'
// Every field below is node 0's and node 0's only. If any of them is on screen while node 1 is
// selected, the Inspector is describing an experiment the operator is not looking at.
const ZERO_ONLY = {
  'failure text': LIGHTNING,
  'source': ZERO_CODE,
  'metric': String(ZERO_METRIC),
  'rationale': ZERO_RATIONALE,
}

const NODE_DETAIL = {
  0: {
    id: 0, attempt: 0, status: 'failed', error: LIGHTNING, error_reason: 'crash',
    metric: ZERO_METRIC, feasible: false, operator: 'mutate', parent_ids: [],
    stages: [{ name: 'train', status: 'fail' }], failed_stage: 'train',
    code: ZERO_CODE, files: { 'solution.py': ZERO_CODE }, trials: [],
    idea: { title: 'ddp baseline', rationale: ZERO_RATIONALE },
    trace: { nodes: [], projection: {} },
  },
  1: {
    id: 1, attempt: 0, status: 'pending', error: '', error_reason: null,
    metric: null, feasible: null, operator: 'mutate', parent_ids: [0],
    stages: [{ name: 'train', status: 'running' }], failed_stage: null,
    code: 'one source', files: { 'solution.py': 'one source' }, trials: [],
    idea: { title: 'training now', rationale: 'one rationale' },
    trace: { nodes: [], projection: {} },
  },
}

const RUN_STATE = {
  run_id: 'rubert-dr-0807', direction: 'max', best_node_id: null, drifts: [], node_concepts: {},
  nodes: {
    0: { id: 0, attempt: 0, status: 'failed', error: LIGHTNING, error_reason: 'crash',
      metric: ZERO_METRIC, operator: 'mutate', parent_ids: [] },
    1: { id: 1, attempt: 0, status: 'pending', error: '', error_reason: null, metric: null,
      operator: 'mutate', parent_ids: [0] },
  },
}
// A LIVE run: node 1 is pending, so the Inspector's 4 s detail poll and the Trace tab's live
// refresh are both armed. A quiescent fixture would not exercise the path the report came from.
const LIVE = { engine_running: true, finished: false, paused: false, building: null }

const installDom = () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
    { url: 'http://localhost/', pretendToBeVisual: true })
  for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
    'MouseEvent', 'CustomEvent', 'getComputedStyle', 'requestAnimationFrame',
    'cancelAnimationFrame']) {
    // `defineProperty`, not assignment — Node ships `globalThis.navigator` as a getter-only accessor.
    Object.defineProperty(globalThis, name,
      { configurable: true, writable: true, value: dom.window[name] })
  }
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT',
    { configurable: true, writable: true, value: true })
  return dom
}

const installFetch = () => {
  globalThis.fetch = async (url) => {
    const path = String(url)
    const detail = /\/api\/runs\/[^/]+\/nodes\/(\d+)(?:\?|$)/.exec(path)
    if (detail) return { ok: true, status: 200, json: async () => NODE_DETAIL[Number(detail[1])] }
    const metrics = /\/nodes\/(\d+)\/metrics/.exec(path)
    if (metrics) {
      const nid = Number(metrics[1])
      return { ok: true, status: 200, json: async () => ({
        node_id: nid, attempt: 0,
        metrics: { loss: [{ step: 1, value: nid === 0 ? ZERO_METRIC : 1 }] },
      }) }
    }
    const logs = /\/nodes\/(\d+)\/logs/.exec(path)
    if (logs) {
      const nid = Number(logs[1])
      return { ok: true, status: 200, json: async () => ({
        eval: '', setup: '', run_setup: '',
        stages: { train: nid === 0 ? LIGHTNING : 'epoch 5/19, still going' },
      }) }
    }
    return { ok: true, status: 200, json: async () => ({}) }
  }
}

const leaked = container => Object.entries(ZERO_ONLY)
  .filter(([, marker]) => container.textContent.includes(marker))
  .map(([name]) => name)

test('switching the selected node leaves no field of the previous one on any tab', async () => {
  const dom = installDom()
  installFetch()
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const Inspector = (await vite.ssrLoadModule('/src/Inspector.jsx')).default
    const { createRoot } = await import('react-dom/client')
    const { flushSync } = await import('react-dom')
    const { act } = await import('react-dom/test-utils')
    const container = dom.window.document.getElementById('root')
    const settle = async () => {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 60)) })
    }

    for (const tab of ['Overview', 'Trace', 'Code', 'Metrics', 'Trust', 'Cost', 'Trials']) {
      const root = createRoot(container)
      const render = nodeId => React.createElement(Inspector, {
        runId: 'rubert-dr-0807', nodeId, state: RUN_STATE, live: LIVE,
        tab, setTab: () => {}, onToast: () => {},
      })
      await act(async () => { root.render(render(0)) })
      await settle()
      // Non-vacuity: at least one tab must actually PUT node 0's data on screen, or the assertions
      // after the switch are about an empty container. Checked per tab where it applies and
      // asserted globally below, because 'Cost' and 'Trace' legitimately show none of these fields.
      const shown = leaked(container)

      // `flushSync`, NOT `act`, for the switch itself. `act` runs passive effects before it
      // returns, so it can only ever observe the state AFTER `useScopedResource`'s effect has
      // re-keyed the resource — which is precisely the moment this defect is already over. The
      // render that shows the previous node's payload is the one committed BEFORE that effect, and
      // `resourceModel.js::resourceView` is the only thing standing in front of it. Proved by
      // mutation: with `resourceView` reduced to `value => value`, the assertion below goes red and
      // the `act`-based version stays green.
      flushSync(() => { root.render(render(1)) })
      assert.deepEqual(leaked(container), [],
        `${tab}: node 0's ${shown.join(', ') || 'data'} survived the switch (pre-effect render)`)
      await settle()
      assert.deepEqual(leaked(container), [],
        `${tab}: node 0's data came back once node 1's detail landed`)

      await act(async () => { root.unmount() })
      if (shown.length) installFetch.sawNodeZero = true
    }
    assert.equal(installFetch.sawNodeZero, true,
      'no tab rendered any node-0-only field, so nothing above could have detected a leak')
  } finally {
    await vite.close()
    // jsdom's `pretendToBeVisual` rAF loop keeps the process alive; without this the file-level
    // subtest reports a TIMEOUT even though every assertion above passed.
    dom.window.close()
  }
})
