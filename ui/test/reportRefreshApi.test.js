import test from 'node:test'
import assert from 'node:assert/strict'

import {
  abandonScopeReportAction, peekReportRefreshIntent, reportRefreshIntent, CONTROL,
  genScopeReport, getScopeReport, jobAwait, reconcileScopeReportGeneration,
} from '../src/api.js'

const GENERATION = 'c'.repeat(64)
const ACTION_ID = '12345678-1234-4234-9234-123456789abc'

const memoryStorage = () => {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

test('report refresh intent survives an ambiguous remount and clears after authority', () => {
  const storage = memoryStorage()
  const first = reportRefreshIntent('ambiguous/run', GENERATION, '', storage)
  const retry = reportRefreshIntent('ambiguous/run', GENERATION, '', storage)
  assert.deepEqual(retry, first)

  reportRefreshIntent('ambiguous/run', GENERATION, first.idempotencyKey, storage)
  const next = reportRefreshIntent('ambiguous/run', GENERATION, '', storage)
  assert.notEqual(next.idempotencyKey, first.idempotencyKey)
})

test('report refresh intent can be inspected without creating or rewriting paid work', () => {
  const values = new Map()
  let writes = 0
  let removals = 0
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { writes += 1; values.set(key, String(value)) },
    removeItem: key => { removals += 1; values.delete(key) },
  }
  const key = 'll.report-refresh.' + encodeURIComponent('saved/run')
  const idempotencyKey = '12345678-1234-4234-9234-123456789abc'
  values.set(key, GENERATION + ':' + idempotencyKey)

  assert.deepEqual(peekReportRefreshIntent('saved/run', GENERATION, storage), {
    generation: GENERATION, idempotencyKey,
  })
  assert.equal(peekReportRefreshIntent('saved/run', 'd'.repeat(64), storage), null)
  assert.equal(writes, 0)
  assert.equal(removals, 0)
  assert.equal(values.get(key), GENERATION + ':' + idempotencyKey)
})

test('scope report publication conflicts reject message-only failure receipts', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      ok: false,
      action_id: ACTION_ID,
      code: 'scope_report_storage_conflict',
      message: 'The report store changed during generation.',
    }),
  })
  try {
    await assert.rejects(
      genScopeReport('task', 'scope-id', { actionId: ACTION_ID }),
      error => error?.code === 'scope_report_storage_conflict'
        && /changed during generation/.test(error.message))
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('scope generation submits one caller-owned UUID and quarantines lost or invalid receipts', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  try {
    const scenarios = [
      async () => { throw new TypeError('connection reset') },
      async () => ({
        ok: false, status: 409, headers: { get: () => null },
        json: async () => ({ detail: {
          code: 'scope_report_storage_conflict',
          message: 'The paid action store could not confirm its strict claim.',
        } }),
      }),
      async () => ({
        ok: true, status: 200,
        json: async () => ({
          status: 'indeterminate', ok: false, action_id: ACTION_ID,
          code: 'scope_report_action_indeterminate',
        }),
      }),
      async () => ({
        ok: true, status: 200,
        json: async () => { throw new SyntaxError('truncated JSON') },
      }),
      async () => ({
        ok: true, status: 200,
        json: async () => ({ ok: true, action_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' }),
      }),
      async () => ({
        ok: true, status: 200,
        json: async () => ({ status: 'unknown', ok: true, action_id: ACTION_ID }),
      }),
    ]
    for (const response of scenarios) {
      const requests = []
      globalThis.fetch = async (url, options) => {
        requests.push({ url: String(url), options })
        return response()
      }
      await assert.rejects(
        genScopeReport('task', 'paid/scope', { actionId: ACTION_ID }),
        error => error?.ambiguous === true
          && error?.submissionMayHaveSucceeded === true
          && error?.action_id === ACTION_ID,
      )
      assert.equal(requests.length, 1, 'an ambiguous initial submission is never replayed')
      assert.match(requests[0].url, /\/api\/scope-report\/task\/paid%2Fscope\/generate$/)
      assert.equal(requests[0].options.method, 'POST')
      assert.equal(requests[0].options.headers['Idempotency-Key'], ACTION_ID)
      assert.equal(requests[0].options.body, '{}')
    }
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('scope generation reconciliation falls back from the known job to the durable action using GET only', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options })
    if (requests.length === 1) {
      return {
        ok: true, status: 200,
        json: async () => { throw new SyntaxError('consumed terminal body') },
      }
    }
    return {
      ok: true, status: 200,
      json: async () => ({
        status: 'done', ok: true, action_id: ACTION_ID,
        authoritative: true, stale: false, content: {},
      }),
    }
  }
  try {
    const result = await reconcileScopeReportGeneration('task', 'paid/scope', {
      actionId: ACTION_ID, jobId: 'known-paid-job', intervalMs: 0,
    })
    assert.equal(result.action_id, ACTION_ID)
    assert.equal(result.ok, true)
    assert.equal(requests.length, 2)
    assert.match(requests[0].url, /\/api\/jobs\/known-paid-job$/)
    assert.match(requests[1].url,
      /\/api\/scope-report-actions\/12345678-1234-4234-9234-123456789abc\?scope_type=task&scope_id=paid%2Fscope$/)
    assert.ok(requests.every(request => request.options.method !== 'POST'))
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('unknown, indeterminate, and mismatched durable actions stay observation-only', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  try {
    const unknownRequests = []
    globalThis.fetch = async (url, options = {}) => {
      unknownRequests.push({ url: String(url), options })
      return { ok: true, status: 200,
        json: async () => ({ status: 'unknown', action_id: ACTION_ID }) }
    }
    await assert.rejects(
      reconcileScopeReportGeneration('task', 'scope-id', { actionId: ACTION_ID }),
      error => error?.ambiguous === true && error?.action_id === ACTION_ID,
    )
    assert.equal(unknownRequests.length, 1)
    assert.notEqual(unknownRequests[0].options.method, 'POST')

    for (const body of [
      { status: 'indeterminate', action_id: ACTION_ID, job_id: 'lost-paid-job' },
      { status: 'done', ok: true, action_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' },
    ]) {
      const requests = []
      globalThis.fetch = async (url, options = {}) => {
        requests.push({ url: String(url), options })
        return { ok: true, status: 200, json: async () => body }
      }
      await assert.rejects(
        reconcileScopeReportGeneration('task', 'scope-id', { actionId: ACTION_ID }),
        error => error?.ambiguous === true && error?.action_id === ACTION_ID,
      )
      assert.equal(requests.length, 1)
      assert.ok(requests.every(request => request.options.method !== 'POST'))
    }
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('scope action APIs canonicalize UUIDs and accept only exact durable or proven-unknown discard', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  const upper = ACTION_ID.toUpperCase()
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options })
    return { ok: true, status: 200, json: async () => requests.length === 1
      ? ({
          status: 'abandoned', ok: false,
          code: 'scope_report_action_abandoned', action_id: ACTION_ID,
        })
      : ({ status: 'unknown', action_id: ACTION_ID }) }
  }
  try {
    const result = await abandonScopeReportAction('task', 'paid/scope', upper)
    assert.equal(result.action_id, ACTION_ID)
    const unknown = await abandonScopeReportAction('task', 'paid/scope', upper)
    assert.equal(unknown.status, 'unknown')
    assert.equal(requests.length, 2)
    assert.equal(requests[0].options.method, 'POST')
    assert.match(requests[0].url,
      /\/api\/scope-report-actions\/12345678-1234-4234-9234-123456789abc\/abandon\?scope_type=task&scope_id=paid%2Fscope$/)
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('a scope fence conflict adopts the exact existing paid action for recovery', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  const fresh = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options })
    return {
      ok: false, status: 409, headers: { get: () => null },
      json: async () => ({ detail: {
        code: 'scope_report_action_in_progress', action_id: ACTION_ID,
        error: 'another action is active',
      } }),
    }
  }
  try {
    await assert.rejects(
      genScopeReport('task', 'shared-scope', { actionId: fresh }),
      error => error?.code === 'scope_report_action_in_progress'
        && error?.ambiguous === true && error?.action_id === ACTION_ID,
    )
    assert.equal(requests.length, 1)
    assert.equal(requests[0].options.method, 'POST')
    assert.equal(requests[0].options.headers['Idempotency-Key'], fresh)
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('canonical scope-report reads have a response-body deadline', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.fetch = async () => new Promise(() => {})
  try {
    await assert.rejects(
      getScopeReport('task', 'bounded-read', { requestTimeoutMs: 1 }),
      error => error?.code === 'COMMAND_REQUEST_TIMEOUT',
    )
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('an exact action-bound terminal failure is definitive only after durable reconciliation', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  let requests = 0
  globalThis.fetch = async () => {
    requests += 1
    return {
      ok: true, status: 200,
      json: async () => ({
        status: 'done', ok: false, action_id: ACTION_ID,
        code: 'scope_report_inputs_changed', ambiguous: true,
      }),
    }
  }
  try {
    await assert.rejects(
      reconcileScopeReportGeneration('task', 'scope-id', {
        actionId: ACTION_ID, jobId: 'known-paid-job', intervalMs: 0,
      }),
      error => error?.code === 'scope_report_inputs_changed'
        && error?.action_id === ACTION_ID
        && error?.ambiguous !== true
        && error?.submissionMayHaveSucceeded !== true,
    )
    assert.equal(requests, 2, 'the volatile terminal is verified through the durable action ledger')
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('a volatile done wrapper cannot turn an indeterminate paid action into a terminal failure', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options })
    return {
      ok: true, status: 200,
      json: async () => requests.length === 1
        ? ({
            status: 'done', ok: false, action_id: ACTION_ID,
            code: 'scope_report_action_indeterminate',
          })
        : ({ status: 'indeterminate', action_id: ACTION_ID, job_id: 'known-paid-job' }),
    }
  }
  try {
    await assert.rejects(
      reconcileScopeReportGeneration('task', 'scope-id', {
        actionId: ACTION_ID, jobId: 'known-paid-job', intervalMs: 0,
      }),
      error => error?.code === 'scope_report_action_indeterminate'
        && error?.action_id === ACTION_ID
        && error?.ambiguous === true
        && error?.submissionMayHaveSucceeded === true,
    )
    assert.equal(requests.length, 2, 'the durable action ledger remains the terminal authority')
    assert.match(requests[0].url, /\/api\/jobs\/known-paid-job$/)
    assert.match(requests[1].url, /\/api\/scope-report-actions\//)
    assert.ok(requests.every(request => request.options.method !== 'POST'))
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('a mismatched volatile terminal falls through to the exact durable action receipt', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  const requests = []
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options })
    return requests.length === 1
      ? {
          ok: true, status: 200,
          json: async () => ({
            status: 'done', ok: true,
            action_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
          }),
        }
      : {
          ok: true, status: 200,
          json: async () => ({ status: 'done', ok: true, action_id: ACTION_ID, content: {} }),
        }
  }
  try {
    const result = await reconcileScopeReportGeneration('task', 'scope-id', {
      actionId: ACTION_ID, jobId: 'known-paid-job', intervalMs: 0,
    })
    assert.equal(result.action_id, ACTION_ID)
    assert.equal(requests.length, 2)
    assert.ok(requests.every(request => request.options.method !== 'POST'))
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('report refresh API quarantines a mismatched 200 receipt as ambiguous', async () => {
  const previous = {
    fetch: globalThis.fetch,
    location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  let requests = 0
  globalThis.fetch = async (_url, options) => {
    requests += 1
    assert.deepEqual(JSON.parse(options.body), { expected_generation: GENERATION })
    assert.equal(options.headers['Idempotency-Key'], 'same-report')
    return {
      ok: true,
      json: async () => ({ ok: true, seq: 8, generation: 'd'.repeat(64) }),
    }
  }
  try {
    await assert.rejects(
      CONTROL.refreshReport('demo', {
        expectedGeneration: GENERATION, idempotencyKey: 'same-report',
      }), error => error?.code === 'REPORT_REFRESH_PROTOCOL_ERROR'
        && error?.ambiguous === true && error?.submissionMayHaveSucceeded === true)
    assert.equal(requests, 1)
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('report refresh intent fails closed on unavailable or silently broken storage', () => {
  assert.throws(
    () => reportRefreshIntent('no-storage', GENERATION, '', null),
    error => error?.code === 'REPORT_REFRESH_STORAGE_UNAVAILABLE')
  assert.throws(
    () => peekReportRefreshIntent('no-storage', GENERATION, null),
    error => error?.code === 'REPORT_REFRESH_STORAGE_UNAVAILABLE')
  const silent = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
  assert.throws(
    () => reportRefreshIntent('silent-storage', GENERATION, '', silent),
    error => error?.code === 'REPORT_REFRESH_STORAGE_UNAVAILABLE')
})

test('a corrupt same-generation report identity is quarantined, never replaced', () => {
  const storage = memoryStorage()
  const key = 'll.report-refresh.' + encodeURIComponent('corrupt/run')
  const corrupt = GENERATION + ':truncated key?'
  storage.setItem(key, corrupt)

  assert.throws(
    () => peekReportRefreshIntent('corrupt/run', GENERATION, storage),
    error => error?.code === 'REPORT_REFRESH_STORAGE_UNAVAILABLE')
  assert.throws(
    () => reportRefreshIntent('corrupt/run', GENERATION, '', storage),
    error => error?.code === 'REPORT_REFRESH_STORAGE_UNAVAILABLE')
  assert.equal(storage.getItem(key), corrupt)
})

test('a completed report identity is tombstoned even when storage removal fails', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: () => { throw new Error('blocked removal') },
  }
  const first = reportRefreshIntent('remove-failure', GENERATION, '', storage)
  assert.equal(reportRefreshIntent(
    'remove-failure', GENERATION, first.idempotencyKey, storage), true)
  const next = reportRefreshIntent('remove-failure', GENERATION, '', storage)
  assert.notEqual(next.idempotencyKey, first.idempotencyKey)
})

test('low-level report refresh requires a caller-owned identity before fetch', async () => {
  const previousFetch = globalThis.fetch
  let fetches = 0
  globalThis.fetch = async () => { fetches += 1; throw new Error('must not fetch') }
  try {
    await assert.rejects(
      CONTROL.refreshReport('demo', { expectedGeneration: GENERATION }),
      /valid report refresh idempotency key/i)
    assert.equal(fetches, 0)
  } finally {
    globalThis.fetch = previousFetch
  }
})

test('background-job polling aborts locally without another request', async () => {
  const controller = new AbortController()
  controller.abort()
  const previousFetch = globalThis.fetch
  let fetches = 0
  globalThis.fetch = async () => { fetches += 1; throw new Error('must not fetch') }
  try {
    await assert.rejects(
      jobAwait({ status: 'running', job_id: 'paid-job' }, {
        intervalMs: 0, signal: controller.signal,
      }), error => error?.name === 'AbortError')
    assert.equal(fetches, 0)
  } finally {
    globalThis.fetch = previousFetch
  }
})

test('background-job polling aborts an in-flight request body', async () => {
  const previous = {
    fetch: globalThis.fetch, location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  const controller = new AbortController()
  let started
  const requestStarted = new Promise(resolve => { started = resolve })
  globalThis.fetch = async (_url, options) => new Promise((resolve, reject) => {
    started()
    options.signal.addEventListener('abort', () => reject(
      options.signal.reason || new DOMException('Aborted', 'AbortError')), { once: true })
  })
  try {
    const polling = jobAwait({ status: 'running', job_id: 'in-flight' }, {
      intervalMs: 0, requestTimeoutMs: 1000, signal: controller.signal,
    })
    await requestStarted
    controller.abort()
    await assert.rejects(polling, error => error?.name === 'AbortError')
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('report requests do not require AbortSignal.any support', async () => {
  const previous = {
    any: globalThis.AbortSignal?.any, fetch: globalThis.fetch,
    location: globalThis.location, sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  Object.defineProperty(globalThis.AbortSignal, 'any', {
    configurable: true, writable: true, value: undefined,
  })
  globalThis.fetch = async () => ({
    ok: true, json: async () => ({ ok: true, seq: 3, generation: GENERATION }),
  })
  try {
    const result = await CONTROL.refreshReport('demo', {
      expectedGeneration: GENERATION, idempotencyKey: 'portable-signal',
      signal: new AbortController().signal,
    })
    assert.equal(result.seq, 3)
  } finally {
    Object.defineProperty(globalThis.AbortSignal, 'any', {
      configurable: true, writable: true, value: previous.any,
    })
    for (const key of ['fetch', 'location', 'sessionStorage']) {
      if (previous[key] === undefined) delete globalThis[key]
      else globalThis[key] = previous[key]
    }
  }
})

test('background-job polling aborts its interval sleep', async () => {
  const previous = {
    fetch: globalThis.fetch, location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  const controller = new AbortController()
  let observed
  const polled = new Promise(resolve => { observed = resolve })
  globalThis.fetch = async () => {
    observed()
    return { ok: true, json: async () => ({ status: 'running' }) }
  }
  try {
    const polling = jobAwait({ status: 'running', job_id: 'sleeping' }, {
      intervalMs: 60_000, signal: controller.signal,
    })
    await polled
    controller.abort()
    await assert.rejects(polling, error => error?.name === 'AbortError')
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('malformed background-job receipts become same-request ambiguity', async () => {
  const previous = {
    fetch: globalThis.fetch, location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ status: 'mystery' }) })
  try {
    const result = await jobAwait(
      { status: 'running', job_id: 'malformed' }, { intervalMs: 0 })
    assert.equal(result.code, 'job_protocol_error')
    assert.equal(result.ambiguous, true)
    assert.equal(result.job_id, 'malformed')
    assert.equal(result.jobId, 'malformed')
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('background-job receipts cannot redirect an accepted paid identity', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.fetch = async () => ({
    ok: true, json: async () => ({ status: 'done', ok: true, job_id: 'different-job' }),
  })
  try {
    const result = await jobAwait(
      { status: 'running', job_id: 'accepted-job' }, { intervalMs: 0 })
    assert.deepEqual(result, {
      ok: false, code: 'job_identity_mismatch', ambiguous: true,
      job_id: 'accepted-job', jobId: 'accepted-job', error: 'job identity mismatch',
    })
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('poll protocol and client errors retain the accepted paid identity', async () => {
  const previous = {
    fetch: globalThis.fetch, location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  try {
    for (const response of [
      { ok: true, status: 200, json: async () => { throw new SyntaxError('html') } },
      {
        ok: false, status: 400, headers: { get: () => null },
        json: async () => ({ detail: { message: 'bad poll request' } }),
      },
    ]) {
      globalThis.fetch = async () => response
      await assert.rejects(
        jobAwait({ status: 'running', job_id: 'accepted-paid-job' }, { intervalMs: 0 }),
        error => error?.submissionMayHaveSucceeded === true
          && error?.ambiguous === true
          && error?.job_id === 'accepted-paid-job'
          && error?.jobId === 'accepted-paid-job')
    }
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('background-job polling recovers from bounded transient read failures', async () => {
  const previous = {
    fetch: globalThis.fetch, location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  let fetches = 0
  globalThis.fetch = async () => {
    fetches += 1
    if (fetches < 3) {
      return {
        ok: false, status: 503, headers: { get: () => null },
        json: async () => ({ detail: { message: 'temporarily unavailable' } }),
      }
    }
    return { ok: true, json: async () => ({ status: 'done', ok: true }) }
  }
  try {
    const result = await jobAwait({ status: 'running', job_id: 'recovering' }, {
      intervalMs: 0, maxTransientErrors: 3,
    })
    assert.equal(result.ok, true)
    assert.equal(fetches, 3)
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('job polling does not swallow authoritative auth or not-found responses', async () => {
  const previous = {
    fetch: globalThis.fetch,
    location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = memoryStorage()
  const statuses = [401, 403, 404]
  try {
    for (const status of statuses) {
      let fetches = 0
      globalThis.fetch = async (_url, options) => {
        fetches += 1
        assert.notEqual(options?.method, 'POST', 'poll recovery is GET-only')
        return {
          ok: false, status, headers: { get: () => null },
          json: async () => ({ detail: { code: `status_${status}`, message: 'denied' } }),
        }
      }
      await assert.rejects(
        jobAwait({ status: 'running', job_id: `job-${status}` }, { intervalMs: 0 }),
        error => error?.status === status)
      assert.equal(fetches, 1)
    }
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})
