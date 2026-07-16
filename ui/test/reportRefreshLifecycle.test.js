import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const state = {
  run_id: 'demo', task_id: 'task', goal: 'report receipt', direction: 'min', phase: 'running',
  nodes: {}, best_node_id: null, reward_hacks: [], drifts: [], research: [], report: null,
}
const GENERATION = 'a'.repeat(64)
const SAVED_REQUEST = '12345678-1234-4234-9234-123456789abc'

test('report refresh is receipt-scoped, double-submit safe, and immune to an older safety timer', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/', pretendToBeVisual: true,
  })
  const realSetTimeout = globalThis.setTimeout
  const realClearTimeout = globalThis.clearTimeout
  const safetyTimers = []
  const requests = []
  const queuedResults = []
  let fetches = 0
  let nextReceipt = 10
  let root
  let vite
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    location: dom.window.location,
    sessionStorage: dom.window.sessionStorage,
    requestAnimationFrame: callback => realSetTimeout(callback, 0),
    cancelAnimationFrame: handle => realClearTimeout(handle),
    IS_REACT_ACT_ENVIRONMENT: true,
    fetch: async (url, options) => {
      fetches += 1
      requests.push({ url: String(url), options })
      const seq = nextReceipt++
      return { ok: true, json: async () => ({
        ...(queuedResults.shift() || {
          ok: true, seq, generation: GENERATION, content: { trigger: 'manual' },
        }),
      }) }
    },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    globalThis.setTimeout = (callback, delay, ...args) => {
      if (delay === 30000) {
        const handle = { callback: () => callback(...args), cleared: false }
        safetyTimers.push(handle)
        return handle
      }
      return realSetTimeout(callback, delay, ...args)
    }
    globalThis.clearTimeout = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) handle.cleared = true
      else realClearTimeout(handle)
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, reportModule] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/Report.jsx'),
    ])
    const { default: ReportView, reportRefreshFailure } = reportModule
    assert.deepEqual(reportRefreshFailure({ code: 'report_refresh_uncertain' }).slice(1),
      [true, true], 'an uncertain paid outcome may resume only its retained identity')
    assert.match(reportRefreshFailure({ code: 'report_refresh_uncertain' })[0],
      /outcome unknown.*saved paid request.*never start another.*operator recovery/i)
    assert.deepEqual(reportRefreshFailure({
      status: 409, code: 'report_refresh_in_progress',
    }).slice(1), [false], 'an unused competing identity is cleared and must not offer a retry POST')
    assert.deepEqual(reportRefreshFailure({ code: 'job_unknown' }).slice(1),
      [true, true], 'an expired process receipt must retry only the durable request identity')
    assert.deepEqual(reportRefreshFailure({ status: 422 }, true).slice(1),
      [false], 'an authoritative client error must not be called an ambiguous paid submission')
    assert.deepEqual(reportRefreshFailure({
      status: 400, submissionMayHaveSucceeded: true,
    }, true).slice(1), [true, true], 'a poll error after acceptance must retain the paid identity')
    assert.match(reportRefreshFailure({ error_kind: 'accounting_pending' })[0],
      /durable cost accounting/i)
    root = createRoot(document.querySelector('#root'))
    const render = (observedSeq, runId = 'demo', expectedGeneration = GENERATION) => React.act(async () => {
      root.render(React.createElement(ReportView, {
        state, runId, observedSeq, expectedGeneration,
      }))
      await Promise.resolve()
    })
    const button = () => document.querySelector('button[aria-describedby="paid-report-refresh-status"]')

    await render(9, 'demo', null)
    assert.equal(button().disabled, true)
    assert.match(button().title, /verified generation/i)
    assert.match(document.querySelector('#paid-report-refresh-status')?.textContent || '',
      /disabled.*verified generation/i)

    sessionStorage.setItem('ll.report-refresh.' + encodeURIComponent('demo'),
      GENERATION + ':' + SAVED_REQUEST)

    await render(9)
    assert.equal(fetches, 0, 'mounting a saved paid request must remain read-only')
    assert.match(button().textContent, /Resume paid refresh/)
    assert.match(document.querySelector('#paid-report-refresh-status')?.textContent || '',
      /paid request saved.*same request.*cannot start a second job.*safely leave/i)
    await React.act(async () => {
      button().click()
      button().click()
      await Promise.resolve()
      await Promise.resolve()
    })
    assert.equal(fetches, 1, 'the synchronous request ref must reject a second click before render')
    assert.deepEqual(JSON.parse(requests[0].options.body), { expected_generation: GENERATION })
    assert.equal(requests[0].options.headers['Idempotency-Key'], SAVED_REQUEST,
      'resume must POST the exact saved paid identity')
    assert.match(button().textContent, /Paid refresh running/)
    assert.match(document.querySelector('#paid-report-refresh-status')?.textContent || '',
      /running with the saved request.*safely leave.*resume it/i)
    assert.equal(safetyTimers.length, 1)

    await render(10)
    assert.match(button().textContent, /Refresh report.*paid/)
    assert.match(document.querySelector('#paid-report-refresh-status')?.textContent || '',
      /Paid AI action.*provider charges.*One request identity.*safely leave.*resume/i)
    assert.equal(safetyTimers[0].cleared, true)

    await React.act(async () => {
      button().click()
      await Promise.resolve()
      await Promise.resolve()
    })
    assert.equal(fetches, 2)
    assert.notEqual(requests[1].options.headers['Idempotency-Key'],
      requests[0].options.headers['Idempotency-Key'], 'a completed refresh gets a new identity')
    assert.match(button().textContent, /Paid refresh running/)
    assert.equal(safetyTimers.length, 2)

    await React.act(async () => { safetyTimers[0].callback(); await Promise.resolve() })
    assert.match(button().textContent, /Paid refresh running/,
      'a stale timer from request A must not clear request B')
    await render(11)
    assert.match(button().textContent, /Refresh report.*paid/)

    queuedResults.push({
      ok: false, code: 'report_refresh_uncertain', generation: GENERATION,
    })
    await React.act(async () => { button().click(); await Promise.resolve(); await Promise.resolve() })
    assert.equal(fetches, 3)
    const uncertainKey = requests[2].options.headers['Idempotency-Key']
    assert.match(button().textContent, /Resume paid refresh/)
    assert.equal(button().disabled, false, 'the same saved identity remains resumable')
    assert.match(document.querySelector('[role="alert"]')?.textContent || '',
      /saved paid request.*operator recovery/i)

    queuedResults.push({
      ok: true, seq: 12, generation: GENERATION, content: { trigger: 'manual' },
    })
    await React.act(async () => { button().click(); await Promise.resolve(); await Promise.resolve() })
    assert.equal(fetches, 4)
    assert.equal(requests[3].options.headers['Idempotency-Key'], uncertainKey,
      'Resume must reconcile the uncertain paid request with exactly the same identity')
    await render(12)
    assert.match(button().textContent, /Refresh report.*paid/)

    Object.defineProperty(globalThis, 'sessionStorage', {
      configurable: true, writable: true,
      value: { getItem: () => { throw new Error('storage blocked') } },
    })
    await render(12, 'storage-blocked')
    assert.equal(fetches, 4, 'paid work must not start when its tab identity cannot be stored')
    assert.match(document.querySelector('[role="alert"]')?.textContent || '',
      /needs working session storage.*preserve one request identity/i)
    assert.match(document.querySelector('#paid-report-refresh-status')?.textContent || '',
      /disabled.*tab cannot safely save its request identity/i)
    assert.equal(button().disabled, true)
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    if (vite) await vite.close()
    globalThis.setTimeout = realSetTimeout
    globalThis.clearTimeout = realClearTimeout
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
    dom.window.close()
  }
})
