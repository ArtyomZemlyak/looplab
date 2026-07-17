import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import React, { act } from 'react'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

import {
  scopeReportPublicationUnconfirmed, SCOPE_CONTENT_SCHEMA,
  SCOPE_NARRATIVE_AUTHORITY, SCOPE_VERDICT_AUTHORITY,
} from '../src/scopeReportModel.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const report = overrides => ({
  exists: true,
  authoritative: true,
  stale: false,
  content: {
    schema: SCOPE_CONTENT_SCHEMA,
    verdict_authority: SCOPE_VERDICT_AUTHORITY,
    narrative_authority: SCOPE_NARRATIVE_AUTHORITY,
    verdict: 'No winner — observations only.',
    headline: 'Replacement published',
    comparison_groups: [],
    metric_observations: [],
    what_worked: [],
    what_didnt: [],
    learnings: [],
    next_directions: [],
    caveats: [],
  },
  ...overrides,
})

test('publication quarantine requires the exact typed safe-projection marker', () => {
  const projection = {
    exists: false, quarantined: true, code: 'scope_report_publication_unconfirmed',
    label: 'bounded label', run_count: 2,
  }
  assert.equal(scopeReportPublicationUnconfirmed(projection), true)
  assert.equal(scopeReportPublicationUnconfirmed({ ...projection, exists: 0 }), false)
  assert.equal(scopeReportPublicationUnconfirmed({ ...projection, quarantined: 1 }), false)
  assert.equal(scopeReportPublicationUnconfirmed({ ...projection, code: 'job_failed' }), false)
  assert.equal(scopeReportPublicationUnconfirmed(null), false)
  assert.equal(scopeReportPublicationUnconfirmed({
    ...projection, content: { headline: 'must remain quarantined' },
  }), true, 'unexpected extra content must not downgrade the response to an ordinary empty state')
})

test('ScopeReport confirms a paid replacement and preserves the ordinary empty state', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const confirmations = []
  let confirmReplacement = false
  dom.window.confirm = message => {
    confirmations.push(String(message))
    return confirmReplacement
  }
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise(resolve => requests.push({
      url: String(url), options, resolve,
    })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const reply = async (request, body) => act(async () => {
    request.resolve({
      ok: true, status: 200, headers: { get: () => null }, json: async () => body,
    })
    for (let index = 0; index < 6; index += 1) await Promise.resolve()
  })
  const render = (root, ScopeReport, scope) => act(async () => {
    root.render(React.createElement(ScopeReport, { scope, onClose: () => {} }))
    await Promise.resolve()
  })
  let root = null
  let vite = null
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: ScopeReport }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ScopeReport.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    await render(root, ScopeReport, {
      type: 'task', id: 'publication-quarantine', label: 'publication-quarantine',
    })
    await reply(requests[0], {
      exists: false, quarantined: true, code: 'scope_report_publication_unconfirmed',
      label: 'publication-quarantine', run_count: 2,
      error: 'SECRET SERVER DETAIL', content: { headline: 'SECRET MODEL CONTENT' },
    })

    assert.match(document.body.textContent, /previous paid generation may have completed/i)
    assert.match(document.body.textContent, /may incur additional provider cost/i)
    assert.doesNotMatch(document.body.textContent, /No report|SECRET SERVER|SECRET MODEL/)
    const replacement = [...document.querySelectorAll('button')]
      .find(button => /Generate replacement \(may incur cost\)/.test(button.textContent))
    assert.equal(replacement?.disabled, false)

    await act(async () => {
      replacement.click()
      await Promise.resolve()
    })
    assert.equal(confirmations.length, 1)
    assert.match(confirmations[0], /new paid action.*additional provider cost/i)
    assert.doesNotMatch(confirmations[0], /SECRET SERVER|SECRET MODEL/)
    assert.equal(requests.filter(request => request.options.method === 'POST').length, 0)

    confirmReplacement = true
    await act(async () => {
      replacement.click()
      await Promise.resolve()
    })
    const posts = requests.filter(request => request.options.method === 'POST')
    assert.equal(posts.length, 1)
    const actionId = posts[0].options.headers['Idempotency-Key']
    assert.match(actionId, /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i)
    assert.equal(posts[0].options.body, '{}')
    const storageKey = 'll.scope-report-generation.'
      + encodeURIComponent(JSON.stringify(['task', 'publication-quarantine']))
    assert.equal(JSON.parse(sessionStorage.getItem(storageKey)).action_id, actionId)

    await reply(posts[0], {
      status: 'done', ok: true, authoritative: true, action_id: actionId,
    })
    assert.match(requests[2].url, /\/api\/scope-report\/task\/publication-quarantine$/)
    await reply(requests[2], report({
      label: 'replacement published', run_ids: ['replacement-run'],
    }))
    assert.match(document.body.textContent, /Cross-run report.*replacement published/s)
    assert.equal(sessionStorage.getItem(storageKey), null)
    assert.equal(requests.filter(request => request.options.method === 'POST').length, 1)

    await render(root, ScopeReport, { type: 'task', id: 'ordinary-empty', label: 'ordinary-empty' })
    await reply(requests[3], { exists: false, label: 'ordinary-empty', run_count: 2 })
    assert.match(document.body.textContent, /No report.*2 runs.*Generate report/s)
    assert.doesNotMatch(document.body.textContent, /Generate replacement|additional provider cost/)
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
