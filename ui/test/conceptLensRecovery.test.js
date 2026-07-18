import test from 'node:test'
import assert from 'node:assert/strict'

import {
  abandonUnknownConceptLens, acquireConceptLensIntent, clearConceptLensIntent,
  createConceptLensResolutionKey, discoverConceptLensRecovery, peekConceptLensIntent,
  pollDiscoveredConceptLens, requestConceptLens, resolveOrphanedConceptLens,
  updateConceptLensIntent, validateConceptLensRecovery,
} from '../src/conceptLensRecovery.js'

const GENERATION = 'a'.repeat(64)
const REQUEST_ID = 'b'.repeat(64)
const JOB_ID = 'c'.repeat(16)
const OTHER_JOB_ID = 'd'.repeat(16)

function memoryStorage() {
  const values = new Map()
  return {
    get length() { return values.size },
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => { values.set(String(key), String(value)) },
    removeItem: key => { values.delete(String(key)) },
    key: index => [...values.keys()][index] ?? null,
  }
}

const response = (payload, status = 200) => ({
  ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
})

test('paid concept-lens intent is prompt/generation bound and storage failure blocks dispatch', () => {
  const storage = memoryStorage()
  const first = acquireConceptLensIntent('run/one', GENERATION, 'group by usage', storage)
  assert.match(first.idempotencyKey,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  assert.equal(acquireConceptLensIntent(
    'run/one', GENERATION, 'group by usage', storage).idempotencyKey, first.idempotencyKey)
  assert.throws(() => acquireConceptLensIntent(
    'run/one', GENERATION, 'different prompt', storage),
  { code: 'CONCEPT_LENS_INTENT_CONFLICT' })

  const submitting = updateConceptLensIntent('run/one', first.idempotencyKey, {
    state: 'submitting',
  }, storage)
  assert.equal(submitting.state, 'submitting')
  const running = updateConceptLensIntent('run/one', first.idempotencyKey, {
    state: 'running', jobId: JOB_ID, requestId: REQUEST_ID,
  }, storage)
  assert.deepEqual(peekConceptLensIntent('run/one', storage), running)
  assert.equal(clearConceptLensIntent('run/one', 'another-key', storage), false)
  assert.deepEqual(peekConceptLensIntent('run/one', storage), running)
  assert.equal(clearConceptLensIntent('run/one', first.idempotencyKey, storage), true)
  assert.equal(peekConceptLensIntent('run/one', storage), null)

  const strict = acquireConceptLensIntent('strict', GENERATION, 'group by usage', storage)
  const strictKey = storage.key(0)
  for (const malformed of [
    { ...strict, updatedAt: 1.5 },
    { ...strict, state: 'ready', jobId: 'job', requestId: REQUEST_ID },
    { ...strict, state: 'submitting', requestId: REQUEST_ID },
    { ...strict, state: 'running', jobId: 'job', requestId: null },
    { ...strict, state: 'running', jobId: 'not-a-16-hex-job', requestId: REQUEST_ID },
    { ...strict, state: 'unknown', jobId: 'job', requestId: null },
  ]) {
    storage.setItem(strictKey, JSON.stringify(malformed))
    assert.throws(() => peekConceptLensIntent('strict', storage),
      { code: 'CONCEPT_LENS_STORAGE_UNAVAILABLE' })
  }
  storage.setItem(strictKey, JSON.stringify(strict))
  assert.equal(clearConceptLensIntent('strict', strict.idempotencyKey, storage), true)

  const blocked = { getItem() { throw new DOMException('blocked', 'SecurityError') } }
  assert.throws(() => acquireConceptLensIntent(
    'blocked', GENERATION, 'group by usage', blocked),
  { code: 'CONCEPT_LENS_STORAGE_UNAVAILABLE' })
})

test('submit persists one running receipt, polls the existing job, and restores frame status', async () => {
  const storage = memoryStorage()
  const intent = acquireConceptLensIntent('demo', GENERATION, 'group by usage', storage)
  const calls = []
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), options })
      if (calls.length === 1) return response({
        status: 'running', job_id: JOB_ID, generation: GENERATION, request_id: REQUEST_ID,
      })
      return response({
        status: 'done', complete: true, ok: false, reason: 'declined',
        generation: GENERATION, request_id: REQUEST_ID,
      })
    }
    let saved
    const result = await requestConceptLens('demo', intent, {
      pollIntervalMs: 0, pollTimeoutMs: 1000,
      onReceipt: receipt => { saved = updateConceptLensIntent(
        'demo', intent.idempotencyKey, receipt, storage) },
    })
    assert.equal(calls.length, 2)
    assert.match(calls[0].url, /\/api\/runs\/demo\/concepts\/lens$/)
    assert.equal(calls[0].options.headers['Idempotency-Key'], intent.idempotencyKey)
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      prompt: 'group by usage', expected_generation: GENERATION,
    })
    assert.match(calls[1].url, new RegExp(`/api/jobs/${JOB_ID}$`))
    assert.equal(saved.jobId, JOB_ID)
    assert.equal(saved.requestId, REQUEST_ID)
    assert.equal(result.status, 'complete',
      'generic job status is restored to the projection frame status before validation')
  } finally { globalThis.fetch = originalFetch }
})

test('reload polls first, then reconciles an expired job with the exact same key', async () => {
  const storage = memoryStorage()
  const base = acquireConceptLensIntent('resume', GENERATION, 'group by usage', storage)
  const intent = updateConceptLensIntent('resume', base.idempotencyKey, {
    state: 'running', jobId: JOB_ID, requestId: REQUEST_ID,
  }, storage)
  const calls = []
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), options })
      if (calls.length === 1) return response({ status: 'unknown' })
      return response({
        status: 'complete', complete: true, ok: false, reason: 'declined',
        generation: GENERATION, request_id: REQUEST_ID,
      })
    }
    const result = await requestConceptLens('resume', intent, {
      pollIntervalMs: 0, pollTimeoutMs: 1000,
    })
    assert.equal(result.reason, 'declined')
    assert.match(calls[0].url, new RegExp(`/api/jobs/${JOB_ID}$`))
    assert.match(calls[1].url, /\/api\/runs\/resume\/concepts\/lens$/)
    assert.equal(calls[1].options.headers['Idempotency-Key'], base.idempotencyKey)
    assert.equal(JSON.parse(calls[1].options.body).prompt, base.prompt)
  } finally { globalThis.fetch = originalFetch }
})

test('ambiguous receipts cannot replace a saved generation, request, or job identity', async () => {
  const storage = memoryStorage()
  const requestId = 'c'.repeat(64)
  const base = acquireConceptLensIntent('bound', GENERATION, 'group by usage', storage)
  const intent = updateConceptLensIntent('bound', base.idempotencyKey, {
    state: 'unknown', requestId,
  }, storage)
  const originalFetch = globalThis.fetch
  try {
    for (const malformed of [
      { generation: 'd'.repeat(64), request_id: requestId },
      { generation: GENERATION, request_id: 'e'.repeat(64) },
      { generation: GENERATION, request_id: null },
      { generation: GENERATION, request_id: requestId, job_id: 42 },
      { generation: GENERATION, request_id: requestId, job_id: 'unexpected-job' },
    ]) {
      globalThis.fetch = async () => response({
        ok: false, code: 'concept_lens_uncertain', ambiguous: true, ...malformed,
      })
      await assert.rejects(requestConceptLens('bound', intent),
        { code: 'CONCEPT_LENS_PROTOCOL_ERROR' })
      assert.deepEqual(peekConceptLensIntent('bound', storage), intent,
        'a malformed reply cannot rewrite the saved recovery envelope')
    }

    const runningBase = acquireConceptLensIntent(
      'bound-job', GENERATION, 'group by composition', storage)
    const running = updateConceptLensIntent('bound-job', runningBase.idempotencyKey, {
      state: 'running', jobId: JOB_ID, requestId,
    }, storage)
    globalThis.fetch = async () => response({
      status: 'done', complete: true, ok: false, ambiguous: true,
      code: 'job_contact_lost', job_id: OTHER_JOB_ID,
      generation: GENERATION, request_id: requestId,
    })
    await assert.rejects(requestConceptLens('bound-job', running, {
      pollIntervalMs: 0, pollTimeoutMs: 1000,
    }), { code: 'CONCEPT_LENS_PROTOCOL_ERROR' })
    assert.deepEqual(peekConceptLensIntent('bound-job', storage), running)
  } finally { globalThis.fetch = originalFetch }
})

test('operator abandon is explicit, generation-fenced, and preserves unknown billing truth', async () => {
  const storage = memoryStorage()
  const base = acquireConceptLensIntent('orphan', GENERATION, 'group by usage', storage)
  const intent = updateConceptLensIntent('orphan', base.idempotencyKey, {
    state: 'unknown', requestId: REQUEST_ID,
  }, storage)
  const calls = []
  const originalFetch = globalThis.fetch
  try {
    const validTerminal = {
      status: 'complete', complete: true, ok: false,
      code: 'concept_lens_abandoned', reason: 'operator_abandoned', abandoned: true,
      resolved: true, provider_outcome: 'unknown', billing_status: 'unknown',
      warning: 'Provider may already have completed and billed the request.',
      generation: GENERATION, request_id: REQUEST_ID, seq: 9,
    }
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), options })
      return response(validTerminal)
    }
    const result = await abandonUnknownConceptLens('orphan', intent)
    assert.equal(result.resolved, true)
    assert.match(calls[0].url, /\/api\/runs\/orphan\/concepts\/lens\/abandon$/)
    assert.equal(calls[0].options.headers['Idempotency-Key'], base.idempotencyKey)
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      expected_generation: GENERATION, request_id: REQUEST_ID,
    })
    for (const malformed of [
      (() => { const value = { ...validTerminal }; delete value.abandoned; return value })(),
      { ...validTerminal, abandoned: false },
      { ...validTerminal, warning: 'x'.repeat(501) },
      { ...validTerminal, warning: 'unsafe\u0000warning' },
    ]) {
      globalThis.fetch = async () => response(malformed)
      await assert.rejects(abandonUnknownConceptLens('orphan', intent),
        { code: 'CONCEPT_LENS_PROTOCOL_ERROR' })
    }
  } finally { globalThis.fetch = originalFetch }
})

test('same-key resume and original-key abandon accept a recovery-won abandon terminal', async () => {
  const storage = memoryStorage()
  const base = acquireConceptLensIntent('recovery-won', GENERATION, 'group by usage', storage)
  const unknown = updateConceptLensIntent('recovery-won', base.idempotencyKey, {
    state: 'unknown', requestId: REQUEST_ID,
  }, storage)
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => response({
      status: 'complete', complete: true, ok: false, code: 'concept_lens_abandoned',
      reason: 'operator_recovered_abandon', abandoned: true, resolved: true,
      provider_outcome: 'unknown', billing_status: 'unknown',
      warning: 'Provider may already have completed and billed the request.',
      generation: GENERATION, request_id: REQUEST_ID, seq: 9,
    })
    assert.equal((await requestConceptLens('recovery-won', unknown)).reason,
      'operator_recovered_abandon')
    assert.equal((await abandonUnknownConceptLens('recovery-won', unknown)).reason,
      'operator_recovered_abandon')
  } finally { globalThis.fetch = originalFetch }
})

test('lost-tab discovery polls a retained job without a paid key and accepts recovered abandon', async () => {
  const calls = []
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), options })
      if (calls.length === 1) return response({
        schema: 1, generation: GENERATION, state: 'running', status: 'done',
        request_id: REQUEST_ID, started_seq: 8, input_seq: 7, job_id: JOB_ID,
      })
      return response({
        status: 'done', complete: true, ok: false, code: 'concept_lens_abandoned',
        reason: 'operator_recovered_abandon', abandoned: true, resolved: true,
        provider_outcome: 'unknown', billing_status: 'unknown',
        warning: 'Provider may already have completed and billed the request.',
        generation: GENERATION, request_id: REQUEST_ID, seq: 9,
      })
    }
    const discovered = await discoverConceptLensRecovery('lost/tab', GENERATION)
    const terminal = await pollDiscoveredConceptLens('lost/tab', discovered, {
      pollIntervalMs: 0, pollTimeoutMs: 1000,
    })
    assert.equal(terminal.status, 'complete')
    assert.equal(terminal.reason, 'operator_recovered_abandon')
    assert.match(calls[0].url,
      /\/api\/runs\/lost%2Ftab\/concepts\/lens\/recovery\?expected_generation=/)
    assert.match(calls[1].url, new RegExp(`/api/jobs/${JOB_ID}$`))
    for (const call of calls) {
      assert.equal(call.options.headers?.['Idempotency-Key'], undefined)
      assert.notEqual(call.options.method, 'POST')
    }
  } finally { globalThis.fetch = originalFetch }
})

test('expired discovered jobs require a fresh recovery GET and can never resubmit provider work', async () => {
  const calls = []
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), options })
      if (calls.length === 1) return response({ status: 'unknown' })
      return response({
        schema: 1, generation: GENERATION, state: 'orphaned',
        request_id: REQUEST_ID, started_seq: 8, input_seq: 7,
      })
    }
    const running = {
      schema: 1, generation: GENERATION, state: 'running', status: 'running',
      request_id: REQUEST_ID, started_seq: 8, input_seq: 7, job_id: JOB_ID,
    }
    const expired = await pollDiscoveredConceptLens('expired', running, {
      pollIntervalMs: 0, pollTimeoutMs: 1000,
    })
    assert.equal(expired.code, 'job_unknown')
    const refreshed = await discoverConceptLensRecovery('expired', GENERATION)
    assert.equal(refreshed.state, 'orphaned')
    assert.equal(calls.length, 2)
    assert.match(calls[0].url, /\/api\/jobs\//)
    assert.match(calls[1].url, /\/concepts\/lens\/recovery\?/)
    assert.ok(calls.every(call => (call.options.method || 'GET') === 'GET'))
  } finally { globalThis.fetch = originalFetch }
})

test('orphan resolution uses a separate key and validates the exact claim receipt', async () => {
  const calls = []
  const originalFetch = globalThis.fetch
  const orphaned = {
    schema: 1, generation: GENERATION, state: 'orphaned',
    request_id: REQUEST_ID, started_seq: 8, input_seq: 7,
  }
  try {
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), options })
      return response({
        status: 'complete', complete: true, ok: false, code: 'concept_lens_abandoned',
        reason: 'operator_recovered_abandon', abandoned: true, resolved: true,
        provider_outcome: 'unknown', billing_status: 'unknown',
        warning: 'Provider may already have completed and billed the request.',
        generation: GENERATION, request_id: REQUEST_ID, seq: 9,
      })
    }
    const key = createConceptLensResolutionKey()
    const terminal = await resolveOrphanedConceptLens('orphan', orphaned, key)
    assert.equal(terminal.reason, 'operator_recovered_abandon')
    assert.equal(calls[0].options.headers['Resolution-Idempotency-Key'], key)
    assert.equal(calls[0].options.headers['Idempotency-Key'], undefined)
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      expected_generation: GENERATION, request_id: REQUEST_ID, expected_started_seq: 8,
    })
  } finally { globalThis.fetch = originalFetch }
})

test('recovery envelopes are exact and conflict projections never carry claim identity', () => {
  assert.equal(validateConceptLensRecovery({
    schema: 1, generation: GENERATION, state: 'none',
  }, GENERATION).state, 'none')
  assert.equal(validateConceptLensRecovery({
    schema: 1, generation: GENERATION, state: 'conflict',
    code: 'concept_lens_recovery_conflict', message: 'repair required',
  }, GENERATION).state, 'conflict')
  for (const malformed of [
    { schema: 1, generation: GENERATION, state: 'none', request_id: REQUEST_ID },
    { schema: 1, generation: GENERATION, state: 'conflict',
      code: 'concept_lens_recovery_conflict', message: 'repair', request_id: REQUEST_ID },
    { schema: 1, generation: GENERATION, state: 'orphaned',
      request_id: REQUEST_ID, started_seq: 8, input_seq: 8 },
    { schema: 1, generation: GENERATION, state: 'running',
      request_id: REQUEST_ID, started_seq: 8, input_seq: 7, job_id: 'not-a-job', status: 'done' },
  ]) assert.throws(() => validateConceptLensRecovery(malformed, GENERATION),
    { code: 'CONCEPT_LENS_RECOVERY_PROTOCOL_ERROR' })
})

test('recovered terminals must be durably ordered after the exact claim', () => {
  const terminal = {
    status: 'complete', complete: true, ok: false, reason: 'declined',
    generation: GENERATION, request_id: REQUEST_ID, seq: 9,
  }
  const envelope = {
    schema: 1, generation: GENERATION, state: 'terminal',
    request_id: REQUEST_ID, started_seq: 8, input_seq: 7, terminal,
  }
  assert.equal(validateConceptLensRecovery(envelope, GENERATION).terminal.seq, 9)
  for (const seq of [undefined, 8, 7, 8.5, Number.MAX_SAFE_INTEGER + 1]) {
    const malformedTerminal = { ...terminal }
    if (seq === undefined) delete malformedTerminal.seq
    else malformedTerminal.seq = seq
    assert.throws(() => validateConceptLensRecovery({
      ...envelope, terminal: malformedTerminal,
    }, GENERATION), { code: 'CONCEPT_LENS_RECOVERY_PROTOCOL_ERROR' })
  }
})

test('polled and orphan-resolution terminals cannot precede or equal their durable claim', async () => {
  const running = {
    schema: 1, generation: GENERATION, state: 'running', status: 'done',
    request_id: REQUEST_ID, started_seq: 8, input_seq: 7, job_id: JOB_ID,
  }
  const orphaned = {
    schema: 1, generation: GENERATION, state: 'orphaned',
    request_id: REQUEST_ID, started_seq: 8, input_seq: 7,
  }
  const originalFetch = globalThis.fetch
  try {
    globalThis.fetch = async () => response({
      status: 'done', complete: true, ok: false, reason: 'declined',
      generation: GENERATION, request_id: REQUEST_ID, seq: 8,
    })
    await assert.rejects(pollDiscoveredConceptLens('ordered-job', running, {
      pollIntervalMs: 0, pollTimeoutMs: 1000,
    }), { code: 'CONCEPT_LENS_RECOVERY_PROTOCOL_ERROR' })

    globalThis.fetch = async () => response({
      status: 'complete', complete: true, ok: false, code: 'concept_lens_abandoned',
      reason: 'operator_recovered_abandon', abandoned: true, resolved: true,
      provider_outcome: 'unknown', billing_status: 'unknown',
      warning: 'Provider may already have completed and billed the request.',
      generation: GENERATION, request_id: REQUEST_ID, seq: 8,
    })
    await assert.rejects(resolveOrphanedConceptLens(
      'ordered-orphan', orphaned, createConceptLensResolutionKey(),
    ), { code: 'CONCEPT_LENS_RECOVERY_PROTOCOL_ERROR' })
  } finally { globalThis.fetch = originalFetch }
})

test('owner-only recovery GET rejects review mode before fetch', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'location')
  const originalFetch = globalThis.fetch
  let calls = 0
  try {
    Object.defineProperty(globalThis, 'location', { configurable: true, value: {
      pathname: '/review', hash: '#valid-review-capability',
    } })
    globalThis.fetch = async () => { calls += 1; throw new Error('must not fetch') }
    await assert.rejects(discoverConceptLensRecovery('reviewed', GENERATION),
      { code: 'REVIEW_READ_ONLY' })
    await assert.rejects(pollDiscoveredConceptLens('reviewed', {
      schema: 1, generation: GENERATION, state: 'running', status: 'running',
      request_id: REQUEST_ID, started_seq: 8, input_seq: 7, job_id: JOB_ID,
    }, { pollIntervalMs: 0, pollTimeoutMs: 1000 }), { code: 'REVIEW_READ_ONLY' })
    assert.equal(calls, 0)
  } finally {
    globalThis.fetch = originalFetch
    if (descriptor) Object.defineProperty(globalThis, 'location', descriptor)
    else delete globalThis.location
  }
})
