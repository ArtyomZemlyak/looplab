import test from 'node:test'
import assert from 'node:assert/strict'

import {
  abandonUnknownConceptLens, acquireConceptLensIntent, clearConceptLensIntent,
  peekConceptLensIntent, requestConceptLens, updateConceptLensIntent,
} from '../src/conceptLensRecovery.js'

const GENERATION = 'a'.repeat(64)
const REQUEST_ID = 'b'.repeat(64)

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
    state: 'running', jobId: 'job-one', requestId: REQUEST_ID,
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
        status: 'running', job_id: 'job-one', generation: GENERATION, request_id: REQUEST_ID,
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
    assert.match(calls[1].url, /\/api\/jobs\/job-one$/)
    assert.equal(saved.jobId, 'job-one')
    assert.equal(saved.requestId, REQUEST_ID)
    assert.equal(result.status, 'complete',
      'generic job status is restored to the projection frame status before validation')
  } finally { globalThis.fetch = originalFetch }
})

test('reload polls first, then reconciles an expired job with the exact same key', async () => {
  const storage = memoryStorage()
  const base = acquireConceptLensIntent('resume', GENERATION, 'group by usage', storage)
  const intent = updateConceptLensIntent('resume', base.idempotencyKey, {
    state: 'running', jobId: 'expired-job', requestId: REQUEST_ID,
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
    assert.match(calls[0].url, /\/api\/jobs\/expired-job$/)
    assert.match(calls[1].url, /\/api\/runs\/resume\/concepts\/lens$/)
    assert.equal(calls[1].options.headers['Idempotency-Key'], base.idempotencyKey)
    assert.equal(JSON.parse(calls[1].options.body).prompt, base.prompt)
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
    globalThis.fetch = async (url, options = {}) => {
      calls.push({ url: String(url), options })
      return response({
        status: 'complete', complete: true, ok: false,
        code: 'concept_lens_abandoned', reason: 'operator_abandoned', resolved: true,
        provider_outcome: 'unknown', billing_status: 'unknown',
        warning: 'Provider may already have completed and billed the request.',
        generation: GENERATION, request_id: REQUEST_ID, seq: 9,
      })
    }
    const result = await abandonUnknownConceptLens('orphan', intent)
    assert.equal(result.resolved, true)
    assert.match(calls[0].url, /\/api\/runs\/orphan\/concepts\/lens\/abandon$/)
    assert.equal(calls[0].options.headers['Idempotency-Key'], base.idempotencyKey)
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      expected_generation: GENERATION, request_id: REQUEST_ID,
    })
  } finally { globalThis.fetch = originalFetch }
})
