import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import React, { act } from 'react'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

import {
  scopeObservationRows, scopeReportAuthority, scopeReportGenerationError, scopeReportKey,
  SCOPE_CONTENT_SCHEMA,
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
    authoritative: true, freshness: 'fresh', fresh: true, inspectable: true,
    verdict: true, narrative: true,
  })
  assert.deepEqual(scopeReportAuthority(record({ stale: true })), {
    authoritative: true, freshness: 'stale', fresh: false, inspectable: true,
    verdict: false, narrative: true,
  })
  for (const stale of [undefined, null, 0, '']) {
    const authority = scopeReportAuthority(record({ stale }))
    assert.equal(authority.authoritative, true)
    assert.equal(authority.freshness, 'unknown')
    assert.equal(authority.fresh, false)
    assert.equal(authority.inspectable, false)
    assert.equal(authority.verdict, false)
    assert.equal(authority.narrative, false)
  }
  for (const authoritative of [undefined, null, 1, 'true', false]) {
    assert.equal(scopeReportAuthority(record({ authoritative })).authoritative, false)
  }
  assert.equal(scopeReportAuthority(record({ exists: false })).authoritative, false)
  assert.equal(scopeReportAuthority(record({
    content: {
      schema: SCOPE_CONTENT_SCHEMA - 1,
      verdict_authority: SCOPE_VERDICT_AUTHORITY,
      narrative_authority: SCOPE_NARRATIVE_AUTHORITY,
    },
  })).authoritative, false)
})

test('verdict and narrative authority are independent and version pinned', () => {
  const legacyVerdict = scopeReportAuthority(record({
    content: { schema: SCOPE_CONTENT_SCHEMA,
      verdict_authority: 'server-derived-v2', narrative_authority: SCOPE_NARRATIVE_AUTHORITY },
  }))
  assert.deepEqual(legacyVerdict, {
    authoritative: true, freshness: 'fresh', fresh: true, inspectable: true,
    verdict: false, narrative: true,
  })

  const legacyNarrative = scopeReportAuthority(record({
    content: { schema: SCOPE_CONTENT_SCHEMA,
      verdict_authority: SCOPE_VERDICT_AUTHORITY, narrative_authority: 'legacy-quarantined' },
  }))
  assert.deepEqual(legacyNarrative, {
    authoritative: true, freshness: 'fresh', fresh: true, inspectable: true,
    verdict: true, narrative: false,
  })
})

test('scope generation failures retain actionable bounded-remediation copy', () => {
  assert.match(
    scopeReportGenerationError({ code: 'SCOPE_REPORT_ACTION_STORAGE_UNAVAILABLE' }),
    /durable paid-action identity.*locked/i,
  )
  assert.match(
    scopeReportGenerationError({ ambiguous: true }),
    /same paid action status.*disabled/i,
  )
  assert.match(
    scopeReportGenerationError({ code: 'scope_report_action_indeterminate' }),
    /cannot be proven complete.*abandon/i,
  )
  assert.match(
    scopeReportGenerationError({ code: 'scope_report_action_unknown' }),
    /No durable claim.*same paid action.*discard/i,
  )
  assert.match(
    scopeReportGenerationError({ code: 'scope_report_action_in_progress' }),
    /already unresolved.*recover/i,
  )
  assert.match(
    scopeReportGenerationError({ code: 'scope_report_action_capacity' }),
    /ledger is full.*new run root.*never delete/i,
  )
  assert.match(
    scopeReportGenerationError({ code: 'scope_report_publication_read_failed' }),
    /completed.*could not be read.*Retry/i,
  )
  assert.equal(
    scopeReportGenerationError({ code: 'scope_report_inputs_changed' }),
    'Scope runs changed during generation. Retry from the current scope snapshot.',
  )
  for (const error of [
    { status: 413 },
    { code: 'scope_report_too_large' },
    { code: 'scope_report_source_too_large' },
  ]) {
    assert.match(scopeReportGenerationError(error), /narrower child scope.*compact oversized/i)
  }
  assert.equal(scopeReportGenerationError({ status: 400 }), 'No runs in this scope yet.')
  assert.equal(scopeReportGenerationError({ message: 'secret provider detail' }), 'Generation failed.')
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
  assert.match(source, /getScopeReport\(scope\.type, scope\.id, \{ signal: controller\.signal \}\)/)
  assert.match(source, /authority\.verdict && verdict/)
  assert.match(source, /authority\.narrative &&/)
  assert.match(source, /groups = authority\.inspectable \? list/)
  assert.match(source, /observations = authority\.inspectable \? list/)
  assert.match(source, /Model-advisory narrative · not a selection decision/)
  assert.match(source, /content is quarantined because its authority is unavailable/)
  assert.match(source, /stale historical snapshot[\s\S]*outcome claims are withheld/)
  assert.match(source, /scopeObservationRows\(group\)/)
  assert.match(source, /Cohort withheld — unverified observation contract/)
  assert.doesNotMatch(source, /Winner:/)
  assert.match(source, /incomplete_runs[\s\S]*run incomplete/)
  assert.match(source, /typeof value\?\.exists === 'boolean'[\s\S]*!Array\.isArray\(value\.content\)/)
  assert.match(source, /scopeReportGenerationError\(error\)/)
  assert.match(source, /persistGeneration\(key, flight\)[\s\S]*driveGeneration\(key, flight, start\)/)
  assert.match(source, /reconcileScopeReportGeneration\([\s\S]*actionId: flight\.actionId/)
  assert.match(source, /Check paid status/)
  assert.match(source, /stale_reason === 'report_format_upgrade'/)
  assert.doesNotMatch(source, /generatedAt|reportAdvanced/)
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

    await render({ type: 'task', id: 'scope-stale', label: 'scope-stale' })
    const stale = payload('STALE')
    stale.stale = true
    await reply(requests[1], stale)
    assert.match(document.body.textContent, /stale historical snapshot/i)
    assert.match(document.body.textContent, /Model-advisory narrative.*HEADLINE STALE.*WORKED STALE/s)
    assert.doesNotMatch(document.body.textContent, /VERDICT STALE/)

    await render({ type: 'task', id: 'scope-a', label: 'scope-a' })
    await reply(requests[2], payload('A'))
    assert.match(document.body.textContent, /VERDICT A.*Model-advisory narrative.*HEADLINE A/s)
    await act(async () => {
      const regenerate = [...document.querySelectorAll('button')].find(button => /Regenerate/.test(button.textContent))
      regenerate.click(); regenerate.click()
      await Promise.resolve()
    })
    assert.match(requests[3].url, /\/scope-a\/generate$/)
    assert.equal(requests.length, 4, 'one scope generation click burst starts one paid request')
    const actionId = requests[3].options.headers['Idempotency-Key']
    assert.match(actionId, /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i)
    assert.equal(requests[3].options.body, '{}')

    await render({ type: 'task', id: 'scope-b', label: 'scope-b' })
    await reply(requests[4], payload('B'))
    await reply(requests[3], { ...payload('LATE A'), ok: true, action_id: actionId })
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

test('ScopeReport reload resumes one stored paid action through job then durable GET', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
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
  const payload = marker => ({
    exists: true, authoritative: true, stale: false, label: marker,
    run_ids: [`run-${marker}`],
    content: {
      schema: SCOPE_CONTENT_SCHEMA,
      verdict_authority: SCOPE_VERDICT_AUTHORITY,
      narrative_authority: SCOPE_NARRATIVE_AUTHORITY,
      verdict: `VERDICT ${marker}`, headline: `HEADLINE ${marker}`,
      comparison_groups: [], metric_observations: [],
      coverage: { prompt_runs: 1, source_runs: 1 },
      what_worked: [], what_didnt: [], learnings: [], next_directions: [], caveats: [],
    },
  })
  const reply = async (request, body) => act(async () => {
    request.resolve({
      ok: true, status: 200, headers: { get: () => null }, json: async () => body,
    })
    for (let index = 0; index < 4; index += 1) await Promise.resolve()
  })
  const replyInvalidJson = async request => act(async () => {
    request.resolve({
      ok: true, status: 200, headers: { get: () => null },
      json: async () => { throw new SyntaxError('truncated terminal') },
    })
    for (let index = 0; index < 4; index += 1) await Promise.resolve()
  })
  const settle = () => act(async () => {
    for (let index = 0; index < 8; index += 1) await Promise.resolve()
  })
  let root = null
  let vite = null
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    const loadComponent = async () => {
      vite = await createServer({
        root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
        server: { middlewareMode: true },
      })
      return (await vite.ssrLoadModule('/src/ScopeReport.jsx')).default
    }
    const { createRoot } = await import('react-dom/client')
    let ScopeReport = await loadComponent()
    root = createRoot(document.getElementById('root'))
    await act(async () => {
      root.render(React.createElement(ScopeReport, {
        scope: { type: 'task', id: 'scope-resume', label: 'scope-resume' },
        onClose: () => {},
      }))
      await Promise.resolve()
    })
    await reply(requests[0], payload('BASELINE'))

    await act(async () => {
      const regenerate = [...document.querySelectorAll('button')]
        .find(button => /Regenerate/.test(button.textContent))
      regenerate.click()
      await Promise.resolve()
    })
    assert.equal(requests[1].options.method, 'POST')
    const actionId = requests[1].options.headers['Idempotency-Key']
    await reply(requests[1], {
      status: 'running', action_id: actionId, job_id: 'known-paid-job',
    })
    assert.match(requests[2].url, /\/api\/jobs\/known-paid-job$/)
    await replyInvalidJson(requests[2])
    assert.match(requests[3].url,
      /\/api\/scope-report-actions\/.+\?scope_type=task&scope_id=scope-resume$/)
    await reply(requests[3], {
      status: 'indeterminate', action_id: actionId, job_id: 'known-paid-job',
    })
    await settle()

    assert.match(document.body.textContent, /cannot be proven complete after a server restart/i)
    assert.match(document.body.textContent, /Abandon recovery lock/)
    const regenerate = [...document.querySelectorAll('button')]
      .find(button => /outcome unknown/i.test(button.textContent))
    assert.equal(regenerate?.disabled, true)
    const storageKey = 'll.scope-report-generation.'
      + encodeURIComponent(JSON.stringify(['task', 'scope-resume']))
    assert.deepEqual(JSON.parse(sessionStorage.getItem(storageKey)), {
      v: 1, action_id: actionId, job_id: 'known-paid-job',
    })
    assert.equal(requests.filter(request => request.options.method === 'POST').length, 1)

    await act(async () => root.unmount())
    root = null
    await vite.close()
    vite = null

    // A fresh module instance proves reload recovery comes from sessionStorage, not module memory.
    ScopeReport = await loadComponent()
    root = createRoot(document.getElementById('root'))
    await act(async () => {
      root.render(React.createElement(ScopeReport, {
        scope: { type: 'task', id: 'scope-resume', label: 'scope-resume' },
        onClose: () => {},
      }))
      await Promise.resolve()
    })
    assert.match(requests[4].url, /\/api\/jobs\/known-paid-job$/)
    assert.notEqual(requests[4].options.method, 'POST')
    await reply(requests[4], { status: 'unknown' })
    assert.match(requests[5].url,
      /\/api\/scope-report-actions\/.+\?scope_type=task&scope_id=scope-resume$/)
    await reply(requests[5], {
      status: 'done', ok: true, authoritative: true, action_id: actionId,
    })
    assert.match(requests[6].url, /\/api\/scope-report\/task\/scope-resume$/)
    await reply(requests[6], payload('RECOVERED'))
    await settle()

    assert.match(document.body.textContent, /VERDICT RECOVERED.*HEADLINE RECOVERED/s)
    assert.equal(sessionStorage.getItem(storageKey), null)
    assert.equal(requests.filter(request => request.options.method === 'POST').length, 1)
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

test('ScopeReport adopts a server-fenced action from another tab before recovery', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
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
  const settle = () => act(async () => {
    for (let index = 0; index < 8; index += 1) await Promise.resolve()
  })
  const reply = async (request, body, ok = true, status = ok ? 200 : 409) => act(async () => {
    request.resolve({ ok, status, headers: { get: () => null }, json: async () => body })
    for (let index = 0; index < 6; index += 1) await Promise.resolve()
  })
  const activeAction = '12345678-1234-4234-9234-123456789abc'
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
    await act(async () => {
      root.render(React.createElement(ScopeReport, {
        scope: { type: 'task', id: 'shared-scope', label: 'shared-scope' },
        onClose: () => {},
      }))
      await Promise.resolve()
    })
    await reply(requests[0], record({ label: 'shared-scope', run_ids: ['r1'] }))

    await act(async () => {
      [...document.querySelectorAll('button')]
        .find(button => /Regenerate/.test(button.textContent)).click()
      await Promise.resolve()
    })
    const rejectedAction = requests[1].options.headers['Idempotency-Key']
    assert.notEqual(rejectedAction, activeAction)
    await reply(requests[1], { detail: {
      code: 'scope_report_action_in_progress', action_id: activeAction,
      error: 'another action is active',
    } }, false, 409)
    await settle()

    const storageKey = 'll.scope-report-generation.'
      + encodeURIComponent(JSON.stringify(['task', 'shared-scope']))
    assert.equal(JSON.parse(sessionStorage.getItem(storageKey)).action_id, activeAction)
    assert.match(document.body.textContent, /already unresolved/i)

    await act(async () => {
      [...document.querySelectorAll('button')]
        .find(button => /Check paid status/.test(button.textContent)).click()
      await Promise.resolve()
    })
    assert.match(requests[2].url,
      new RegExp(`/api/scope-report-actions/${activeAction}\\?scope_type=task&scope_id=shared-scope$`))
    assert.notEqual(requests[2].options.method, 'POST')
    await reply(requests[2], {
      status: 'indeterminate', action_id: activeAction, job_id: 'orphaned-paid-job',
    })
    await settle()
    assert.match(document.body.textContent, /Abandon recovery lock/)
    assert.equal(requests.filter(request => request.options.method === 'POST').length, 1)
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

test('ScopeReport re-probes repaired storage and clears a settled uppercase UUID', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const actionId = '12345678-1234-4234-9234-123456789abc'
  const storageKey = 'll.scope-report-generation.'
    + encodeURIComponent(JSON.stringify(['task', 'storage-recovery']))
  const values = new Map([[storageKey, JSON.stringify({
    v: 1, action_id: actionId.toUpperCase(), job_id: null,
  })]])
  let storageUnavailable = true
  const storage = {
    getItem: key => {
      if (storageUnavailable) throw new DOMException('blocked', 'SecurityError')
      return values.get(key) ?? null
    },
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
  const requests = []
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: storage,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise(resolve => requests.push({
      url: String(url), options, resolve,
    })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const settle = () => act(async () => {
    for (let index = 0; index < 8; index += 1) await Promise.resolve()
  })
  const reply = async (request, body) => act(async () => {
    request.resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => body })
    for (let index = 0; index < 6; index += 1) await Promise.resolve()
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
    await act(async () => {
      root.render(React.createElement(ScopeReport, {
        scope: { type: 'task', id: 'storage-recovery', label: 'storage-recovery' },
        onClose: () => {},
      }))
      await Promise.resolve()
    })
    await settle()
    assert.equal(requests.length, 0)
    assert.match(document.body.textContent, /Durable paid-action identity is unavailable/i)

    storageUnavailable = false
    await act(async () => {
      [...document.querySelectorAll('button')]
        .find(button => /Check paid status/.test(button.textContent)).click()
      await Promise.resolve()
    })
    assert.match(requests[0].url,
      new RegExp(`/api/scope-report-actions/${actionId}\\?scope_type=task&scope_id=storage-recovery$`))
    await reply(requests[0], {
      status: 'done', ok: true, authoritative: true, action_id: actionId,
    })
    assert.match(requests[1].url, /\/api\/scope-report\/task\/storage-recovery$/)
    await reply(requests[1], record({ label: 'RECOVERED', run_ids: ['r1'] }))
    await settle()

    assert.equal(values.get(storageKey), undefined)
    assert.match(document.body.textContent, /Cross-run report.*RECOVERED/s)
    assert.equal(requests.filter(request => request.options.method === 'POST').length, 0)
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
