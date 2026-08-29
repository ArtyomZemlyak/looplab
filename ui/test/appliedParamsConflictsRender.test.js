// THE CONFLICTED COORDINATES, RENDERED — the case `bd022b3c` left behind.
//
// `engine/champion_caveats.py::applied_params_diverged` raises the `params_overridden` caveat on
// `diverged` OR `conflicts`, and its docstring says why in as many words: "A CONFLICT COUNTS, and
// that is the half worth stating … Reading only `diverged` would answer 'no caveat' about exactly
// the champion this whole change is about" — `rubertlite-dr-unified-v8` node 3, 0.762048, whose two
// carriers disagree on `batch_size` and `gradient_accumulation_steps`.
//
// The Metrics tab gated its footnote on `appliedParamsDivergences(...).length > 0` alone. So for a
// conflicts-only champion the run row showed the slug, the operator opened the tab that commit
// built as the answer to the slug, and no applied-params section rendered at all.
//
// This file renders the REAL `Metrics` component, because a model test cannot see a gate: the
// helpers `appliedParamsUnsettled` and `appliedParamsConflicts` can both be exported, correct and
// fully tested while no production module imports them — which is precisely the state
// `appliedParamsUnsettled` was in when this defect was found.
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

let dom = null
let root = null
let vite = null
const previous = {}

// v8 node 3's real shape: the config says one number, `train.py` says another, and
// `runtime/applied_params.py` refuses to settle it — so the coordinate is in NEITHER `applied` nor
// `diverged`, which is what made it invisible.
const CONFLICT = {
  param: 'train.training.batch_size',
  declared: 8192,
  readings: [
    { applied: 4096, file: 'vectorsearch/train.py', line: 31 },
    { applied: 8192, file: 'vectorsearch/configs/config.yaml', line: 12 },
  ],
}
const DIVERGENCE = {
  param: 'train.training.n_epochs',
  declared: 15, applied: 8,
  file: 'vectorsearch/configs/config.yaml', line: 263, match: 'exact',
}

const node = (applied_params) => ({
  id: 3, metric: 0.762048, status: 'evaluated', attempt: 0,
  extra_metrics: {},
  metric_provenance: applied_params ? { applied_params } : {},
})

async function metricsHtml(applied_params) {
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
      // MetricCurves fetches on mount; a never-settling promise keeps it pending instead of
      // throwing, which is what we want — this file is about what the panel PRINTS, not about it.
      fetch: () => new Promise(() => {}),
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
  const mod = await vite.ssrLoadModule('/src/Inspector.jsx')
  const n = node(applied_params)
  await act(async () => root.render(React.createElement(mod.Metrics, {
    n, detail: {}, state: { nodes: { 3: n }, best_node_id: 3 }, runId: 'r',
  })))
  return document.getElementById('root').innerHTML
}

after(async () => {
  // UNMOUNT BEFORE RESTORING THE GLOBALS. `MetricCurves` is mounted by this panel and schedules work
  // through the stubbed `requestAnimationFrame`; a callback that fires after `window` is deleted
  // throws `ReferenceError: window is not defined` into an unhandledRejection, which node's runner
  // attributes to the test that already passed. Unmounting cancels those effects while the globals
  // it captured are still there.
  if (root) await act(async () => root.unmount())
  if (vite) await vite.close()
  for (const [name, descriptor] of Object.entries(previous)) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
})

test('a CONFLICTS-ONLY record renders the conflicted coordinate and both readings', async () => {
  const html = await metricsHtml({ checked: 12, conflicts: [CONFLICT],
                                   unresolved: { 'train.training.batch_size': 'conflict' } })
  assert.ok(html.includes('cannot settle'),
    'the conflicts block must render for a record with conflicts and no divergences — this is the '
    + 'case the champion caveat fires on and the tab showed nothing for')
  assert.ok(html.includes('train.training.batch_size'), 'it must NAME the knob, or it is the slug again')
  assert.ok(html.includes('vectorsearch/train.py') && html.includes('vectorsearch/configs/config.yaml'),
    'both disagreeing carriers must be named — "two files disagree" without saying which is not an answer')
  assert.ok(html.includes('4096') && html.includes('8192'),
    'both readings must be shown; the operator cannot judge a disagreement from one side of it')
})

test('a conflicts-only record does NOT claim to know what ran', async () => {
  const html = await metricsHtml({ checked: 12, conflicts: [CONFLICT] })
  assert.ok(!html.includes('did not run'),
    'the divergence heading ("Declared coordinates that did not run") asserts we know what DID run. '
    + 'A conflict is the record saying it cannot tell, and must not borrow that sentence')
})

test('a DIVERGENCES-ONLY record still renders exactly as it did, with no conflicts block', async () => {
  const html = await metricsHtml({ checked: 12, diverged: [DIVERGENCE] })
  assert.ok(html.includes('did not run'), 'the divergence footnote must be untouched by this change')
  assert.ok(!html.includes('cannot settle'),
    'no conflicts block may render for a record with no conflicts — an invented caveat is the '
    + 'defect `objectiveMetricSource` forbids one section up')
})

test('a record with BOTH renders both blocks, counted apart', async () => {
  const html = await metricsHtml({ checked: 12, diverged: [DIVERGENCE], conflicts: [CONFLICT] })
  assert.ok(html.includes('did not run'), 'divergences still render')
  assert.ok(html.includes('cannot settle'), 'conflicts render beside them')
  assert.ok(html.indexOf('did not run') < html.indexOf('cannot settle'),
    'divergences first: they are the settled claim, and the unsettled one reads as a qualification of it')
})

test('a clean record renders NEITHER block', async () => {
  const html = await metricsHtml({ checked: 12, applied: { 'train.training.n_epochs': 15 } })
  assert.ok(!html.includes('did not run') && !html.includes('cannot settle'),
    'a record where everything agreed must print no caveat at all')
})
