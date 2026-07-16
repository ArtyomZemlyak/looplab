import test from 'node:test'
import assert from 'node:assert/strict'

import {
  reportRefreshIntent, CONTROL, genScopeReport, jobAwait,
} from '../src/api.js'

const GENERATION = 'c'.repeat(64)

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

test('scope report publication conflicts reject message-only failure receipts', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      ok: false,
      code: 'scope_report_storage_conflict',
      message: 'The report store changed during generation.',
    }),
  })
  try {
    await assert.rejects(
      genScopeReport('task', 'scope-id'),
      error => error?.code === 'scope_report_storage_conflict'
        && /changed during generation/.test(error.message))
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
        error => error?.submissionMayHaveSucceeded === true)
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
