import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import React, { act } from 'react'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

import {
  scopeObservationRows, scopeReportAuthority, scopeReportKey, SCOPE_CONTENT_SCHEMA,
  SCOPE_NARRATIVE_AUTHORITY, SCOPE_VERDICT_AUTHORITY,
} from '../src/scopeReportModel.js'

const source = readFileSync(new URL('../src/ScopeReport.jsx', import.meta.url), 'utf8')
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const record = overrides => ({
  exists: true,
  authoritative: true,
  stale: false,
  content: {
    schema: SCOPE_CONTENT_SCHEMA,
    verdict_authority: SCOPE_VERDICT_AUTHORITY,
    narrative_authority: SCOPE_NARRATIVE_AUTHORITY,
  },
  ...overrides,
})

test('scope report authority requires exact current protocol markers', () => {
  assert.deepEqual(scopeReportAuthority(record()), {
    current: true, verdict: true, narrative: true,
  })
  for (const stale of [undefined, null, 0, '', true]) {
    assert.equal(scopeReportAuthority(record({ stale })).current, false)
  }
  for (const authoritative of [undefined, null, 1, 'true', false]) {
    assert.equal(scopeReportAuthority(record({ authoritative })).current, false)
  }
  assert.equal(scopeReportAuthority(record({ exists: false })).current, false)
  assert.equal(scopeReportAuthority(record({
    content: {
      schema: SCOPE_CONTENT_SCHEMA - 1,
      verdict_authority: SCOPE_VERDICT_AUTHORITY,
      narrative_authority: SCOPE_NARRATIVE_AUTHORITY,
    },
  })).current, false)
})

test('verdict and narrative authority are independent and version pinned', () => {
  const legacyVerdict = scopeReportAuthority(record({
    content: { schema: SCOPE_CONTENT_SCHEMA,
      verdict_authority: 'server-derived-v2', narrative_authority: SCOPE_NARRATIVE_AUTHORITY },
  }))
  assert.deepEqual(legacyVerdict, { current: true, verdict: false, narrative: true })

  const legacyNarrative = scopeReportAuthority(record({
    content: { schema: SCOPE_CONTENT_SCHEMA,
      verdict_authority: SCOPE_VERDICT_AUTHORITY, narrative_authority: 'legacy-quarantined' },
  }))
  assert.deepEqual(legacyNarrative, { current: true, verdict: true, narrative: false })
})

test('scope identity is collision-safe for separator-bearing opaque ids', () => {
  assert.notEqual(
    scopeReportKey({ type: 'task:a', id: 'b' }),
    scopeReportKey({ type: 'task', id: 'a:b' }),
  )
})

test('schema 5 cohorts remain observations-only even with current top-level markers', () => {
  const group = {
    contract_authority: 'declared', outcome_policy: 'observations-only-v1',
    winner: null, tied_winners: [], measurements: [{ authority: 'declared', run_id: 'a' }],
  }
  assert.deepEqual(scopeObservationRows(group), group.measurements)
  assert.equal(scopeObservationRows({ ...group, winner: { run_id: 'a' } }), null)
  assert.equal(scopeObservationRows({ ...group, tied_winners: [{ run_id: 'a' }] }), null)
  assert.equal(scopeObservationRows({ ...group, outcome_policy: 'rank-point-estimates' }), null)
})

test('ScopeReport fences late requests and quarantines outcome-bearing legacy content', () => {
  assert.match(source, /requestEpoch\.current !== epoch \|\| keyRef\.current !== key/)
  assert.match(source, /requestEpoch\.current === epoch && keyRef\.current === key/)
  assert.match(source, /getScopeReport\(scope\.type, scope\.id, \{ signal: controller\.signal \}\)/)
  assert.match(source, /authority\.verdict && verdict/)
  assert.match(source, /authority\.narrative &&/)
  assert.match(source, /groups = authority\.current \? list/)
  assert.match(source, /observations = authority\.current \? list/)
  assert.match(source, /Model-advisory narrative · not a selection decision/)
  assert.match(source, /content is quarantined because its authority or freshness is not current/)
  assert.match(source, /scopeObservationRows\(group\)/)
  assert.match(source, /Cohort withheld — unverified observation contract/)
  assert.doesNotMatch(source, /Winner:/)
  assert.match(source, /incomplete_runs[\s\S]*run incomplete/)
  assert.match(source, /typeof value\?\.exists === 'boolean'[\s\S]*!Array\.isArray\(value\.content\)/)
  assert.match(source, /error\?\.status === 400/)
  assert.doesNotMatch(source, /\/400\/\.test\(error\.message\)|'Generation failed: ' \+ error\.message/)
})

test('ScopeReport renders only current authority and ignores an old generation after navigation', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    // Deliberately ignore AbortSignal here: a transport can still complete after local cancellation,
    // and the component's identity fence must remain the final line of defence.
    fetch: (url, options) => new Promise(resolve => requests.push({
      url: String(url), options, resolve,
    })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const payload = marker => ({
    exists: true, authoritative: true, stale: false, label: marker, run_ids: [`run-${marker}`],
    content: {
      schema: SCOPE_CONTENT_SCHEMA,
      verdict_authority: SCOPE_VERDICT_AUTHORITY,
      narrative_authority: SCOPE_NARRATIVE_AUTHORITY,
      verdict: `VERDICT ${marker}`, headline: `HEADLINE ${marker}`,
      comparison_groups: [], metric_observations: [],
      coverage: { prompt_runs: 1, source_runs: 1 },
      what_worked: [`WORKED ${marker}`], what_didnt: [], learnings: [], next_directions: [], caveats: [],
    },
  })
  const reply = async (request, body) => act(async () => {
    request.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => body })
    await Promise.resolve()
  })
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
    const [{ createRoot }, { default: ScopeReport }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ScopeReport.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    const render = scope => act(async () => {
      root.render(React.createElement(ScopeReport, { scope, onClose: () => {} }))
      await Promise.resolve()
    })

    await render({ type: 'task', id: 'legacy', label: 'legacy' })
    const legacy = payload('SECRET LEGACY')
    legacy.authoritative = false
    await reply(requests[0], legacy)
    assert.match(document.body.textContent, /content is quarantined/i)
    assert.doesNotMatch(document.body.textContent, /VERDICT SECRET|HEADLINE SECRET|WORKED SECRET/)

    await render({ type: 'task', id: 'scope-a', label: 'scope-a' })
    await reply(requests[1], payload('A'))
    assert.match(document.body.textContent, /VERDICT A.*Model-advisory narrative.*HEADLINE A/s)
    await act(async () => {
      const regenerate = [...document.querySelectorAll('button')].find(button => /Regenerate/.test(button.textContent))
      regenerate.click(); regenerate.click()
      await Promise.resolve()
    })
    assert.match(requests[2].url, /\/scope-a\/generate$/)
    assert.equal(requests.length, 3, 'one scope generation click burst starts one paid request')

    await render({ type: 'task', id: 'scope-b', label: 'scope-b' })
    await reply(requests[3], payload('B'))
    await reply(requests[2], payload('LATE A'))
    assert.match(document.body.textContent, /VERDICT B.*HEADLINE B/s)
    assert.doesNotMatch(document.body.textContent, /LATE A|VERDICT A|HEADLINE A/)
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
