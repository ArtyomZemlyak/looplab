// Guard for the ONE shared load / last-good / stale / retry resource machine (doc 25 UI-06).
// It drives the machine rather than reading it: the pure transitions get a truth table, the hook
// gets a real React tree with a controllable read, and each converted call site is mounted and taken
// through the sequence that used to be re-derived per component — load → success, refresh → failure
// → last-good retained → retry, abort on unmount, and a superseded response that must not settle the
// request that replaced it.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

import {
  resourceBegin, resourceCancel, resourceGated, resourceInitial, resourceSettle, resourceView,
} from '../src/resourceModel.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('the resource transitions keep last-good data, keep a verdict, and name every intent', () => {
  const ready = { scope: 'a', status: 'ready', data: { v: 1 }, error: '', pending: null }

  // A background refresh over healthy data is invisible — the SAME object comes back, so nothing
  // re-renders and no busy label flickers on a poll tick.
  assert.equal(resourceBegin(ready, { scope: 'a', intent: 'refresh' }), ready)
  // Everything the operator is waiting on announces itself, WITHOUT discarding what they can see.
  assert.deepEqual(resourceBegin(ready, { scope: 'a', intent: 'retry' }),
    { scope: 'a', status: 'ready', data: { v: 1 }, error: '', pending: 'retry' })
  const stale = { scope: 'a', status: 'stale', data: { v: 1 }, error: 'boom', pending: null }
  assert.deepEqual(resourceBegin(stale, { scope: 'a', intent: 'refresh' }),
    { scope: 'a', status: 'stale', data: { v: 1 }, error: 'boom', pending: 'refresh' },
    'a refresh must not quietly re-present data the operator was told not to trust')
  const failed = { scope: 'a', status: 'error', data: null, error: 'boom', pending: null }
  assert.deepEqual(resourceBegin(failed, { scope: 'a', intent: 'retry' }),
    { scope: 'a', status: 'error', data: null, error: 'boom', pending: 'retry' },
    'a retry must not blank the error the operator is reading back to a spinner')
  // A scope change drops the previous scope's payload AND its message before anything is requested.
  assert.deepEqual(resourceBegin(ready, { scope: 'b', intent: 'load' }),
    { scope: 'b', status: 'loading', data: null, error: '', pending: 'load' })
  assert.deepEqual(resourceBegin(ready, { scope: 'a', intent: 'reconcile', mapLastGood: () => ({ v: 2 }) }),
    { scope: 'a', status: 'ready', data: { v: 2 }, error: '', pending: 'reconcile' })
  // mapLastGood may reject the retained payload outright, which drops the resource back to loading.
  assert.deepEqual(resourceBegin(ready, { scope: 'a', intent: 'reconcile', mapLastGood: () => null }),
    { scope: 'a', status: 'loading', data: null, error: '', pending: 'reconcile' })

  assert.deepEqual(resourceSettle(stale, { scope: 'a', ok: true, data: { v: 9 } }),
    { scope: 'a', status: 'ready', data: { v: 9 }, error: '', pending: null })
  assert.deepEqual(resourceSettle(ready, { scope: 'a', ok: false }),
    { scope: 'a', status: 'stale', data: { v: 1 }, error: '', pending: null },
    'a failure with something to fall back on is stale, and keeps it')
  assert.deepEqual(resourceSettle(resourceInitial('a', 'loading'), { scope: 'a', ok: false }),
    { scope: 'a', status: 'error', data: null, error: '', pending: null })
  // The classifier sees the last-good payload, so a failure can read differently depending on
  // whether anything survives it; a TERMINAL verdict says so by handing back its own data.
  const seen = []
  assert.deepEqual(resourceSettle(ready, {
    scope: 'a', ok: false, failure: { transport: true, message: '', intent: 'refresh' },
    classify: failure => { seen.push(failure); return { status: 'gone', error: 'revoked', data: null } },
  }), { scope: 'a', status: 'gone', data: null, error: 'revoked', pending: null })
  assert.deepEqual(seen, [{ transport: true, message: '', intent: 'refresh', lastGood: { v: 1 } }])

  // A late settlement for a scope that is no longer current can never become the current scope's
  // data: it commits against its OWN scope, and the view then refuses to read it.
  const late = resourceSettle({ scope: 'b', status: 'loading', data: null, error: '', pending: 'load' },
    { scope: 'a', ok: true, data: { v: 'stale-a' } })
  assert.equal(late.scope, 'a')
  assert.deepEqual(resourceView(late, 'b', 'loading'),
    { scope: 'b', status: 'loading', data: null, error: '', pending: null })
  assert.equal(resourceView(late, 'a', 'loading'), late)
  assert.deepEqual(resourceView(late, 'b', 'restricted').status, 'restricted',
    'the gate status is what a scope with no committed state shows, not a bare loading')

  // An abort teaches nothing about the resource, so only the busy label goes.
  const pendingRetry = { scope: 'a', status: 'stale', data: { v: 1 }, error: 'boom', pending: 'retry' }
  assert.deepEqual(resourceCancel(pendingRetry, 'a'),
    { scope: 'a', status: 'stale', data: { v: 1 }, error: 'boom', pending: null })
  assert.equal(resourceCancel(pendingRetry, 'b'), pendingRetry)
  assert.deepEqual(resourceGated('a', 'restricted'),
    { scope: 'a', status: 'restricted', data: null, error: '', pending: null })
})

// One jsdom + vite harness for every mounted case below.
const withDom = async (body) => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const nativeSetTimeout = globalThis.setTimeout
  const nativeClearTimeout = globalThis.clearTimeout
  const requests = []
  const timers = new Map()
  let timerId = 0
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => nativeSetTimeout(callback, 0),
    cancelAnimationFrame: handle => nativeClearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    // Abort-aware on purpose: a real fetch REJECTS when its signal aborts, and the unmount path
    // below is only meaningful if an aborted read actually settles.
    fetch: (url, options = {}) => new Promise((resolve, reject) => {
      const entry = { url: String(url), options, resolve }
      requests.push(entry)
      options.signal?.addEventListener('abort',
        () => { entry.aborted = true; reject(new DOMException('aborted', 'AbortError')) })
    }),
    setInterval: callback => { const id = ++timerId; timers.set(id, callback); return id },
    clearInterval: id => timers.delete(id),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const { createRoot } = await import('react-dom/client')
    root = createRoot(document.getElementById('root'))
    const render = async element => act(async () => root.render(element))
    const reply = async (request, payload, status = 200) => act(async () => {
      request.resolve({
        ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
      })
      await Promise.resolve()
    })
    const click = async button => act(async () => button.dispatchEvent(
      new dom.window.MouseEvent('click', { bubbles: true })))
    const button = text => [...document.querySelectorAll('button')]
      .find(node => node.textContent.trim().startsWith(text))
    await body({ dom, vite, root, render, reply, click, button, requests, timers })
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
}

test('useScopedResource fences one flight per scope, keeps last-good, and refuses a superseded settle',
  async () => {
    await withDom(async ({ vite, render }) => {
      const hooks = await vite.ssrLoadModule('/src/hooks.js')
      const reads = []
      const renders = []
      let latest = null
      let controls = { scope: 'a', gate: null }
      const Probe = ({ scope, gate, pollMs }) => {
        latest = hooks.useScopedResource(signal => new Promise((resolve, reject) => {
          const entry = { scope, signal, resolve, reject }
          reads.push(entry)
          signal.addEventListener('abort', () => { entry.aborted = true })
        }), {
          scope, gate, pollMs, timeout: 60_000,
          validate: value => (value?.ok === false ? 'rejected payload' : null),
          classifyFailure: ({ transport, message, lastGood }) => ({
            error: transport ? 'transport' : message,
            ...(message === 'terminal' ? { status: 'gone', data: null } : {}),
          }),
        })
        // Recorded from the render body on purpose: the scope fence is about the ONE commit between
        // a scope change and the passive effect that services it, which `act` would otherwise hide.
        renders.push({ scope: latest.scope, status: latest.status, data: latest.data })
        return null
      }
      const show = async next => {
        controls = { ...controls, ...next }
        await render(React.createElement(Probe, controls))
      }

      await show({ scope: 'a' })
      assert.deepEqual([latest.status, latest.pending, latest.data], ['loading', 'load', null])
      assert.equal(reads.length, 1)
      await act(async () => { reads[0].resolve({ v: 1 }) })
      assert.deepEqual([latest.status, latest.pending, latest.data], ['ready', null, { v: 1 }])

      // A background refresh over healthy data does not announce itself; a second start while one is
      // in flight is REFUSED rather than queued, so poll ticks cannot stack up behind a slow read.
      await act(async () => { latest.request('refresh') })
      assert.deepEqual([latest.status, latest.pending], ['ready', null])
      assert.equal(reads.length, 2)
      assert.equal(latest.request('refresh'), false)
      assert.equal(reads.length, 2)

      // A failed refresh keeps the last good payload and says so.
      await act(async () => { reads[1].reject(new Error('network')) })
      assert.deepEqual([latest.status, latest.pending, latest.data, latest.error],
        ['stale', null, { v: 1 }, 'transport'])

      // Retry keeps the stale verdict AND the data while it runs, then replaces both on success.
      await act(async () => { latest.retry() })
      assert.deepEqual([latest.status, latest.pending, latest.data], ['stale', 'retry', { v: 1 }])
      await act(async () => { reads[2].resolve({ v: 2 }) })
      assert.deepEqual([latest.status, latest.pending, latest.data], ['ready', null, { v: 2 }])

      // HTTP success is not resource truth: a rejected payload is a failure, and last-good survives.
      await act(async () => { latest.request('refresh') })
      await act(async () => { reads[3].resolve({ ok: false }) })
      assert.deepEqual([latest.status, latest.data, latest.error],
        ['stale', { v: 2 }, 'rejected payload'])

      // Supersede: an explicit retry aborts the in-flight read instead of being refused by it, and
      // the aborted read's own rejection must not settle — or de-busy — the request that replaced it.
      await act(async () => { latest.request('refresh') })
      const superseded = reads.at(-1)
      await act(async () => { latest.retry({ supersede: true }) })
      assert.equal(superseded.aborted, true)
      assert.equal(reads.length, 6)
      await act(async () => { await Promise.resolve() })
      assert.equal(latest.pending, 'retry',
        'the superseded read must not clear the pending state of the retry that replaced it')
      await act(async () => { reads[5].resolve({ v: 3 }) })
      assert.deepEqual([latest.status, latest.pending, latest.data], ['ready', null, { v: 3 }])

      // A scope change hides the previous scope's payload in the SAME render — before any effect —
      // and the read that lands late for the old scope can never become the new scope's data.
      const before = reads.length
      const mark = renders.length
      await show({ scope: 'b' })
      assert.deepEqual(renders[mark], { scope: 'b', status: 'loading', data: null },
        'the very first render under a new scope must not show the previous scope\'s payload')
      assert.deepEqual([latest.scope, latest.status, latest.data], ['b', 'loading', null])
      assert.equal(reads.length, before + 1)
      assert.equal(reads.at(-1).scope, 'b')
      await act(async () => { reads[before - 1].resolve({ v: 'stale-a' }) })
      assert.deepEqual([latest.scope, latest.status, latest.data], ['b', 'loading', null])

      // A gate means "do not read at all": no request, and the resource sits at the gate status.
      const gated = reads.length
      await show({ gate: 'restricted' })
      assert.deepEqual([latest.status, latest.data], ['restricted', null])
      assert.equal(reads.length, gated)
      assert.equal(latest.request('refresh'), false)
      assert.equal(reads.length, gated)

      // A terminal verdict drops last-good: it is no longer something the operator may act on.
      await show({ scope: 'c', gate: null })
      await act(async () => { reads.at(-1).resolve({ v: 4 }) })
      assert.deepEqual([latest.status, latest.data], ['ready', { v: 4 }])
      await act(async () => { latest.request('refresh') })
      await act(async () => { reads.at(-1).resolve({ ok: false, message: 'x' }) })
      assert.equal(latest.status, 'stale')
      await show({ scope: 'd' })
      await act(async () => { reads.at(-1).resolve({ v: 5 }) })
      await act(async () => { latest.request('refresh') })
      const terminalRead = reads.at(-1)
      await act(async () => { terminalRead.reject(Object.assign(new Error('x'), { name: 'Terminal' })) })
      assert.equal(latest.status, 'stale', 'a transport failure stays stale unless classified terminal')
    })
  })

test('useScopedResource aborts its in-flight read on unmount and polls without stacking ticks',
  async () => {
    await withDom(async ({ vite, render, root, timers }) => {
      const hooks = await vite.ssrLoadModule('/src/hooks.js')
      const reads = []
      let latest = null
      const Probe = () => {
        latest = hooks.useScopedResource(signal => new Promise(resolve => {
          const entry = { signal, resolve }
          reads.push(entry)
          signal.addEventListener('abort', () => { entry.aborted = true })
        }), { scope: 'a', pollMs: 2000, timeout: 60_000 })
        return null
      }
      await render(React.createElement(Probe))
      assert.equal(reads.length, 1)
      const tick = [...timers.values()].at(-1)
      await act(async () => { tick(); tick() })
      assert.equal(reads.length, 1, 'poll ticks must skip a read that is already in flight')
      await act(async () => { reads[0].resolve({ v: 1 }) })
      await act(async () => { tick() })
      assert.equal(reads.length, 2)
      assert.equal(latest.data.v, 1, 'a poll over healthy data keeps showing it while it refreshes')

      const live = reads.at(-1)
      await act(async () => root.render(null))
      assert.equal(live.aborted, true, 'unmount must abort the in-flight read, not merely ignore it')
      assert.equal(timers.size, 0, 'unmount must stop the poll')
    })
  })

test('TrustPanel keeps its detector-config states: error, retry-in-flight, then recovered',
  async () => {
    await withDom(async ({ vite, render, reply, click, button, requests }) => {
      const panels = await vite.ssrLoadModule('/src/panels.jsx')
      const state = { nodes: {}, drifts: [], reward_hacks: [], direction: 'max', leakage: null }
      await render(React.createElement(panels.TrustPanel, {
        state, runId: 'demo', onClose() {}, onSelect() {}, onToast() {},
      }))
      assert.match(document.body.textContent, /Loading detector configuration/)
      assert.match(document.body.textContent, /Loading…/)
      assert.match(requests.at(-1).url, /\/api\/runs\/demo\/config$/)

      await reply(requests.at(-1), { detail: 'upstream refused' }, 503)
      assert.match(document.body.textContent, /Detector configuration unavailable/)
      // This is the ONE converted site that renders the failure text, which is why it is also the one
      // that passes a `classifyFailure`: trust coverage is not verifiable without saying what stopped
      // the check. Everything else leaves `resource.error` at '' and renders fixed copy.
      assert.match(document.body.textContent, /Coverage cannot be verified: .*upstream refused/)
      assert.match(document.body.textContent, /Unknown/)

      const before = requests.length
      await click(button('Retry'))
      assert.equal(requests.length, before + 1)
      // Exactly the old presentation: while the retry runs the panel is back on its loading state,
      // and the unavailable notice (with its Retry) is gone until the read settles again.
      assert.match(document.body.textContent, /Loading detector configuration/)
      assert.doesNotMatch(document.body.textContent, /Detector configuration unavailable/)
      assert.equal(button('Retry'), undefined)

      await reply(requests.at(-1), { trust_mode: 'docker', eval_trust_mode: 'host' })
      assert.doesNotMatch(document.body.textContent, /Loading detector configuration/)
      assert.match(document.body.textContent, /docker/)
      assert.match(document.body.textContent, /host/)
    })
  })

test('Inspector keeps last-good node detail across a failed reload and re-reads on a scope change',
  async () => {
    await withDom(async ({ vite, render, reply, requests }) => {
      const { default: Inspector } = await vite.ssrLoadModule('/src/Inspector.jsx')
      const summary = (status) => ({
        nodes: { 5: { id: 5, attempt: 0, status } }, drifts: [], direction: 'max',
      })
      const show = (status, nodeId = 5) => render(React.createElement(Inspector, {
        runId: 'demo', nodeId, state: summary(status), live: null, tab: 'Overview',
        setTab() {}, onToast() {},
      }))
      await show('pending')
      await reply(requests.at(-1), {
        id: 5, attempt: 0, status: 'pending', operator: 'seed',
        idea: { params: {}, rationale: 'LAST-GOOD-DETAIL-MARKER' },
      })
      assert.match(document.body.textContent, /LAST-GOOD-DETAIL-MARKER/)

      // The node's own status is deliberately outside the detail scope: a status change RE-READS the
      // same scope instead of resetting it, so the detail on screen never blanks mid-experiment.
      const before = requests.length
      await show('ok')
      assert.equal(requests.length, before + 1)
      assert.match(document.body.textContent, /LAST-GOOD-DETAIL-MARKER/)
      await reply(requests.at(-1), { detail: 'api-token=must-not-render' }, 503)
      assert.match(document.body.textContent,
        /Experiment details could not be refreshed\. Last loaded details remain visible/)
      assert.match(document.body.textContent, /LAST-GOOD-DETAIL-MARKER/,
        'a failed reload must not discard the detail the operator is reading')
      assert.doesNotMatch(document.body.textContent, /must-not-render/)

      // A different node IS a different scope: the previous node's detail must be gone from the
      // very first render, before any effect runs.
      await render(React.createElement(Inspector, {
        runId: 'demo', nodeId: 6, state: { nodes: { 6: { id: 6, attempt: 0, status: 'ok' } }, drifts: [], direction: 'max' },
        live: null, tab: 'Overview', setTab() {}, onToast() {},
      }))
      assert.doesNotMatch(document.body.textContent, /LAST-GOOD-DETAIL-MARKER/)
    })
  })

test('the review route gates a missing token, treats a revoked link as terminal, and retries the rest',
  async () => {
    await withDom(async ({ vite, render, reply, click, button, requests }) => {
      const { ReviewRoute } = await vite.ssrLoadModule('/src/App.jsx')

      // No token is a gate, not a failed read: nothing is requested and no Retry is offered.
      await render(React.createElement(ReviewRoute, { token: '' }))
      assert.equal(requests.length, 0)
      assert.match(document.body.textContent, /Review link incomplete or invalid/)
      assert.equal(button('Retry'), undefined)

      await render(React.createElement(ReviewRoute, { token: 'rv_one' }))
      assert.equal(requests.length, 1)
      assert.match(document.body.textContent, /Opening review…/)
      await reply(requests.at(-1), { detail: 'revoked' }, 410)
      assert.match(document.body.textContent, /Review link invalid, expired, or revoked/)
      assert.equal(button('Retry'), undefined, 'a revoked capability can only repeat itself')

      await render(React.createElement(ReviewRoute, { token: 'rv_two' }))
      await reply(requests.at(-1), { detail: 'upstream' }, 503)
      assert.match(document.body.textContent, /Could not open review/)
      assert.match(document.body.textContent, /Review link unavailable/)
      const before = requests.length
      await click(button('Retry'))
      assert.equal(requests.length, before + 1)
      // A retry that fails again must leave the operator the control they just used. The success arm
      // is deliberately not driven here: it hands the route to a lazily imported RunView, which is a
      // different subject with its own coverage.
      await reply(requests.at(-1), { detail: 'upstream' }, 503)
      assert.match(document.body.textContent, /Review link unavailable/)
      assert.ok(button('Retry'))
    })
  })

test('every converted site reads the shared machine, and none keeps a private copy of it', async () => {
  const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')
  const [panels, runView, inspector, app, hooks] = await Promise.all([
    source('panels.jsx'), source('RunView.jsx'), source('Inspector.jsx'), source('App.jsx'),
    source('hooks.js'),
  ])
  for (const [name, text] of [['panels.jsx', panels], ['RunView.jsx', runView],
    ['Inspector.jsx', inspector], ['App.jsx', app]]) {
    assert.match(text, /useScopedResource/, `${name} must read the shared resource machine`)
    // Negative pins: what must not come back is the TEXT of a second, drifting copy — a commented-out
    // re-derivation is as much of a drift risk as a live one.
    assert.doesNotMatch(text, /state: lastGood == null \? 'error' : 'stale'/,
      `${name} must not re-derive the stale/error rule`)
    assert.doesNotMatch(text, /status: lastGood == null \? 'error' : 'stale'/,
      `${name} must not re-derive the stale/error rule`)
  }
  // RunView's notice is pure presentation over the shared state: the separate `retrying` flag it used
  // to carry IS `pending === 'retry'`, and the budget it shows is the machine's last-good payload,
  // which is what keeps the limit on screen through a failed refresh. RunView itself is not mounted
  // anywhere in this suite (it owns the SSE stream and the React Flow canvas), so the sequence this
  // maps is covered by the hook test above and the mapping is pinned here.
  assert.match(runView, /const maxEval = configResource\.data\?\.max_eval_seconds/)
  assert.match(runView,
    /const configNoticeStatus = configResource\.pending === 'retry'\n\s*\? 'retrying'\n\s*: \['error', 'stale'\]\.includes\(configResource\.status\) \? configResource\.status : null/)
  assert.doesNotMatch(runView, /retrying: sameResource/,
    'the retrying flag must not come back as a second spelling of pending')
  // The one-flight rule and the supersede escape hatch belong to the hook, not to any call site.
  assert.match(hooks, /if \(flight\.current\) return false/)
  assert.match(hooks, /if \(supersede && flight\.current\)/)
})
