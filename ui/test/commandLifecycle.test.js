import test from 'node:test'
import assert from 'node:assert/strict'

import {
  CONTROL, clearAssistantRunTransport, clearRunCommandLock, clearRunTransport, commandCanRetry, commandFailureRecord, commandFeedback,
  commandEventForAction, commandRecordMatchesAction, createIdempotencyKey, isTransientCommandReadError,
  getRunCommand, loadAssistantRunTransport, observeRunGeneration,
  loadRunCommandLock, loadRunTransport, retryRunCommand, runCommand,
  saveAssistantRunTransport as saveAssistantRunTransportRaw,
  saveRunCommandLock as saveRunCommandLockRaw, saveRunTransport as saveRunTransportRaw,
  submitRunCommand, subscribeRunCommandLock,
} from '../src/api.js'
import {
  assistantDirectIntent, assistantRunChanged, assistantStorageFailureOwnsLock,
  pollAssistantDirectOnce, presentAssistantCommandResult, submitAssistantDirect,
} from '../src/assistantCommand.js'

const CMD_A = `cmd_${'a'.repeat(32)}`
const CMD_B = `cmd_${'b'.repeat(32)}`
const CMD_C = `cmd_${'c'.repeat(32)}`
const GEN_A = 'a'.repeat(64)
const GEN_B = 'b'.repeat(64)
const saveRunTransport = (runId, state, storage) =>
  saveRunTransportRaw(runId, { expectedGeneration: GEN_A, ...state }, storage)
const saveAssistantRunTransport = (runId, state, storage) =>
  saveAssistantRunTransportRaw(runId, { expectedGeneration: GEN_A, ...state }, storage)
const saveRunCommandLock = (runId, state, storage) =>
  saveRunCommandLockRaw(runId, { expectedGeneration: GEN_A, ...state }, storage)

const jsonResponse = (body, status = 200, headers = {}) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
  headers: { get: name => headers[name] ?? headers[String(name).toLowerCase()] ?? null },
})

const stalledJsonResponse = (status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: () => new Promise(() => {}),
  headers: { get: () => null },
})

const withHttpGlobals = async (fetchImpl, fn) => {
  const previous = { location: globalThis.location, fetch: globalThis.fetch, sessionStorage: globalThis.sessionStorage }
  globalThis.location = { pathname: '/proxy/app/', hash: '' }
  globalThis.sessionStorage = { getItem: () => '' }
  globalThis.fetch = (url, options = {}) => String(url).endsWith('/state') && options.method == null
    ? Promise.resolve(jsonResponse({ state: {}, seq: 0, generation: GEN_A }))
    : fetchImpl(url, options)
  try { return await fn() }
  finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
}

test('run command posts once with an idempotency key and polls by command id', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return calls.length === 1
      ? jsonResponse({ id: 'cmd-1', status: 'accepted', event_type: 'resume' }, 202)
      : jsonResponse({ id: 'cmd-1', status: 'succeeded', event_type: 'resume' })
  }, async () => {
    const result = await runCommand('demo run', 'resume', {}, { waitMs: 50, pollMs: 0, submitRetries: 0 })
    assert.equal(result.status, 'succeeded')
  })

  assert.equal(calls[0].url, '/proxy/app/api/runs/demo%20run/commands')
  assert.equal(calls[0].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    type: 'resume', data: {}, expected_generation: GEN_A,
  })
  assert.match(calls[0].options.headers['Idempotency-Key'], /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  assert.equal(calls[1].url, '/proxy/app/api/runs/demo%20run/commands/cmd-1')
  assert.equal(calls[1].options.headers['Idempotency-Key'], undefined)
  assert.equal(calls[1].options.cache, 'no-store')
})

test('a command binds to the displayed generation and never substitutes a newer server generation', async () => {
  const calls = []
  observeRunGeneration('stale-visible-run', GEN_A)
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return jsonResponse({ detail: {
      code: 'run_generation_changed',
      message: 'the run was reset',
      remediation: 'refresh before acting',
      expected_generation: GEN_A,
      current_generation: GEN_B,
    } }, 409)
  }, async () => {
    await assert.rejects(
      runCommand('stale-visible-run', 'resume', {}, { submitRetries: 0 }),
      error => error.status === 409 && error.code === 'run_generation_changed'
        && error.remediation === 'refresh before acting',
    )
  })
  assert.equal(calls.length, 1, 'the displayed token prevents a fresh state read from rebinding intent')
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(JSON.parse(calls[0].options.body).expected_generation, GEN_A)
})

test('durable command envelopes require and preserve an exact lowercase run generation', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const state = { action: 'resume', idempotencyKey: 'generation-envelope',
    record: { status: 'submitting' } }
  assert.equal(saveRunTransportRaw('demo', state, storage), false)
  assert.equal(saveRunTransportRaw('demo', { ...state, expectedGeneration: GEN_A.toUpperCase() }, storage), false)
  assert.equal(saveRunTransportRaw('demo', {
    ...state, expectedGeneration: { toString: () => GEN_A },
  }, storage), false)
  assert.equal(saveRunTransportRaw('demo', { ...state, expectedGeneration: GEN_A }, storage), true)
  assert.equal(loadRunTransport('demo', storage).expectedGeneration, GEN_A)
  assert.equal(loadRunCommandLock('demo', storage).expectedGeneration, GEN_A)
  assert.equal(saveRunCommandLockRaw('demo', {
    source: 'dock', action: 'resume', idempotencyKey: 'generation-envelope',
    expectedGeneration: GEN_B, record: { status: 'submitting' },
  }, storage), false, 'a lock cannot be rebound to another generation')
})

test('command status errors distinguish transient reads from authoritative 4xx responses', () => {
  assert.equal(isTransientCommandReadError(new TypeError('connection reset')), true)
  assert.equal(isTransientCommandReadError({ status: 500 }), true)
  assert.equal(isTransientCommandReadError({ status: 503 }), true)
  for (const status of [408, 425, 429]) assert.equal(isTransientCommandReadError({ status }), true, `HTTP ${status}`)
  for (const status of [400, 401, 403, 404, 409]) {
    assert.equal(isTransientCommandReadError({ status }), false, `HTTP ${status}`)
  }
  assert.equal(isTransientCommandReadError({ code: 'COMMAND_PROTOCOL_ERROR' }), false)
  assert.equal(isTransientCommandReadError({ name: 'AbortError' }), false)
})

test('an authoritative command-status 404 stops polling and retains the command record', async () => {
  let calls = 0
  await withHttpGlobals(async () => {
    calls++
    return calls === 1
      ? jsonResponse({ id: 'cmd-gone', status: 'accepted', event_type: 'resume' }, 202)
      : jsonResponse({ detail: 'no such command' }, 404)
  }, async () => {
    await assert.rejects(
      runCommand('demo', 'resume', {}, { waitMs: 100, pollMs: 0, submitRetries: 0 }),
      error => error.status === 404 && error.commandRecord?.id === 'cmd-gone',
    )
  })
  assert.equal(calls, 2)
})

test('a transient command-status 5xx is retried without replaying the command', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    if (calls.length === 1) return jsonResponse({ id: 'cmd-read-retry', status: 'accepted' }, 202)
    if (calls.length === 2) return jsonResponse({ detail: 'temporary status outage' }, 503)
    return jsonResponse({ id: 'cmd-read-retry', status: 'succeeded' })
  }, async () => {
    const result = await runCommand('demo', 'resume', {}, { waitMs: 100, pollMs: 0, submitRetries: 0 })
    assert.equal(result.status, 'succeeded')
  })
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 1)
  assert.equal(calls[1].options.cache, 'no-store')
  assert.equal(calls[2].options.cache, 'no-store')
})

test('rate-limited command observation respects transient semantics without replaying intent', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    if (calls.length === 1) return jsonResponse({ id: 'cmd-rate', status: 'accepted' }, 202)
    if (calls.length === 2) return jsonResponse({ detail: 'slow down' }, 429, { 'Retry-After': '0' })
    return jsonResponse({ id: 'cmd-rate', status: 'succeeded' })
  }, async () => {
    const result = await runCommand('demo', 'resume', {}, {
      waitMs: 150, pollMs: 0, submitRetries: 0,
    })
    assert.equal(result.status, 'succeeded')
  })
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 1)
  assert.equal(calls.length, 3)
})

test('a mismatched command-status record is an authoritative protocol failure', async () => {
  let calls = 0
  await withHttpGlobals(async () => {
    calls++
    return calls === 1
      ? jsonResponse({ id: 'cmd-expected', status: 'accepted' }, 202)
      : jsonResponse({ id: 'cmd-other', status: 'succeeded' })
  }, async () => {
    await assert.rejects(
      runCommand('demo', 'resume', {}, { waitMs: 100, pollMs: 0, submitRetries: 0 }),
      error => error.code === 'COMMAND_PROTOCOL_ERROR' && error.commandRecord?.id === 'cmd-expected',
    )
  })
  assert.equal(calls, 2)
})

test('strict command parsing rejects terminal success without a durable id', async () => {
  await withHttpGlobals(async () => jsonResponse({ status: 'succeeded' }), async () => {
    await assert.rejects(
      runCommand('demo', 'resume', {}, { submitRetries: 0 }),
      error => error.code === 'COMMAND_PROTOCOL_ERROR'
        && error.submissionMayHaveSucceeded === true
        && error.commandUnknown === true,
    )
  })
})

test('malformed JSON on status observation is a bounded protocol error, not eternal pending', async () => {
  let calls = 0
  await withHttpGlobals(async () => {
    calls++
    if (calls === 1) return jsonResponse({ id: 'cmd-json', status: 'accepted' }, 202)
    return { ok: true, status: 200, headers: { get: () => null },
      json: async () => { throw new SyntaxError('truncated JSON') } }
  }, async () => {
    await assert.rejects(
      runCommand('demo', 'resume', {}, { waitMs: 100, pollMs: 0, submitRetries: 0 }),
      error => error.code === 'COMMAND_PROTOCOL_ERROR' && error.commandRecord?.id === 'cmd-json',
    )
  })
  assert.equal(calls, 2)
})

test('submit deadline covers a stalled response body and preserves ambiguous recovery identity', async () => {
  const idempotencyKey = '11111111-aaaa-4bbb-8ccc-222222222222'
  await withHttpGlobals(async () => stalledJsonResponse(202), async () => {
    await assert.rejects(
      submitRunCommand('demo', 'resume', {}, {
        idempotencyKey, expectedGeneration: GEN_A, requestTimeoutMs: 5,
      }),
      error => error.code === 'COMMAND_REQUEST_TIMEOUT'
        && error.submissionMayHaveSucceeded === true,
    )
  })
})

test('command observation deadline covers a stalled response body', async () => {
  await withHttpGlobals(async () => stalledJsonResponse(), async () => {
    await assert.rejects(
      getRunCommand('demo', CMD_A, { requestTimeoutMs: 5 }),
      error => error.code === 'COMMAND_REQUEST_TIMEOUT'
        && isTransientCommandReadError(error),
    )
  })
})

test('retry body timeout observes the same durable id instead of replaying recovery', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return calls.length === 1
      ? stalledJsonResponse(202)
      : jsonResponse({ id: CMD_A, status: 'succeeded', event_type: 'resume' })
  }, async () => {
    const result = await retryRunCommand('demo', CMD_A, { waitMs: 0, requestTimeoutMs: 5 })
    assert.equal(result.status, 'succeeded')
  })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].options.method, 'POST')
  assert.ok(calls[1].url.endsWith(`/commands/${CMD_A}`))
  assert.equal(calls[1].options.cache, 'no-store')
})

test('unknown server command status is rejected instead of being rendered as completion', async () => {
  await withHttpGlobals(async () => jsonResponse({ id: 'cmd-unknown', status: 'almost_done' }), async () => {
    await assert.rejects(
      runCommand('demo', 'resume', {}, { submitRetries: 0 }),
      error => error.code === 'COMMAND_PROTOCOL_ERROR' && error.commandRecord?.id === 'cmd-unknown',
    )
  })
})

test('retry helper re-arms and polls the same durable command id', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return calls.length === 1
      ? jsonResponse({ id: 'cmd_same', status: 'accepted', event_type: 'run_abort' }, 202)
      : jsonResponse({ id: 'cmd_same', status: 'succeeded', event_type: 'run_abort' })
  }, async () => {
    const result = await retryRunCommand('demo run', 'cmd_same', { waitMs: 100, pollMs: 0 })
    assert.equal(result.id, 'cmd_same')
    assert.equal(result.status, 'succeeded')
  })
  assert.equal(calls[0].url, '/proxy/app/api/runs/demo%20run/commands/cmd_same/retry')
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(calls[0].options.body, undefined)
  assert.equal(calls[1].url, '/proxy/app/api/runs/demo%20run/commands/cmd_same')
  assert.equal(calls[1].options.cache, 'no-store')
})

test('an ambiguous retry POST observes the same record instead of replaying recovery', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return calls.length === 1
      ? jsonResponse({ detail: 'response lost after acceptance' }, 503)
      : jsonResponse({ id: 'cmd_same', status: 'succeeded', event_type: 'run_abort' })
  }, async () => {
    const result = await retryRunCommand('demo', 'cmd_same', { waitMs: 0 })
    assert.equal(result.status, 'succeeded')
  })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(calls[1].url, '/proxy/app/api/runs/demo/commands/cmd_same')
  assert.equal(calls[1].options.cache, 'no-store')
})

test('a cross-tab retry 409 observes the same id before deciding its outcome', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return calls.length === 1
      ? jsonResponse({ detail: 'command is already executing' }, 409)
      : jsonResponse({ id: 'cmd_same', status: 'executing' })
  }, async () => {
    const result = await retryRunCommand('demo', 'cmd_same', { waitMs: 0 })
    assert.equal(result.id, 'cmd_same')
    assert.equal(result.status, 'executing')
  })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(calls[1].url, '/proxy/app/api/runs/demo/commands/cmd_same')
})

test('only retryable failed/timed-out records expose same-command retry', () => {
  assert.equal(commandCanRetry({ id: 'cmd-1', status: 'failed', error: { retryable: true } }), true)
  assert.equal(commandCanRetry({ id: 'cmd-1', status: 'timed_out', error: { retryable: true } }), true)
  assert.equal(commandCanRetry({ id: 'cmd-1', status: 'rejected', error: { retryable: true } }), false)
  assert.equal(commandCanRetry({ id: 'cmd-1', status: 'failed', error: { retryable: false } }), false)
  assert.equal(commandCanRetry({ status: 'failed', error: { retryable: true } }), false)
})

test('a transient POST retry reuses the same idempotency key', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    if (calls.length === 1) throw new TypeError('connection reset')
    return jsonResponse({ id: 'cmd-retry', status: 'noop', event_type: 'pause' })
  }, async () => {
    const result = await runCommand('demo', 'pause', {}, { retryMs: 0, submitRetries: 1 })
    assert.equal(result.status, 'noop')
  })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].options.headers['Idempotency-Key'], calls[1].options.headers['Idempotency-Key'])
})

test('a 5xx POST response retries with the same key, while preserving one logical command', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    if (calls.length === 1) return jsonResponse({ detail: 'worker unavailable' }, 503)
    return jsonResponse({ id: 'cmd-after-503', status: 'succeeded', event_type: 'resume' })
  }, async () => {
    const result = await runCommand('demo', 'resume', {}, { retryMs: 0, submitRetries: 1 })
    assert.equal(result.status, 'succeeded')
  })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].options.headers['Idempotency-Key'], calls[1].options.headers['Idempotency-Key'])
})

test('a 4xx command response is authoritative and is not retried', async () => {
  let calls = 0
  await withHttpGlobals(async () => {
    calls++
    return jsonResponse({ detail: 'bad command payload' }, 400)
  }, async () => {
    await assert.rejects(
      runCommand('demo', 'unknown', {}, { submitRetries: 3, retryMs: 0 }),
      error => error.status === 400 && /bad command payload/.test(error.message),
    )
  })
  assert.equal(calls, 1)
})

test('structured HTTP detail preserves code, remediation, and command id', async () => {
  await withHttpGlobals(async () => jsonResponse({ detail: {
    code: 'invalid_command', message: 'payload is invalid', remediation: 'fix the node id',
  } }, 400), async () => {
    await assert.rejects(
      runCommand('demo', 'resume', {}, { submitRetries: 0 }),
      error => error.status === 400 && error.code === 'invalid_command'
        && error.remediation === 'fix the node id' && error.message === 'payload is invalid',
    )
  })
})

test('fresh-key unresolved 409 attaches to the server-named command instead of creating intent', async () => {
  const existing = 'cmd_0123456789abcdef0123456789abcdef'
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return calls.length === 1
      ? jsonResponse({ detail: {
          code: 'retry_existing_command', message: 'an identical command already exists',
          existing_command_id: existing,
        } }, 409)
      : jsonResponse({ id: existing, status: 'failed', error: {
          message: 'engine failed to start', retryable: true,
        } })
  }, async () => {
    const result = await runCommand('demo', 'resume', {}, { submitRetries: 0 })
    assert.equal(result.id, existing)
    assert.equal(result.status, 'failed')
  })
  assert.equal(calls.length, 2)
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 1)
  assert.ok(calls[1].url.endsWith(`/commands/${existing}`))
})

test('different command-in-progress 409 never masquerades as success for the requested action', async () => {
  const existing = 'cmd_fedcba9876543210fedcba9876543210'
  let calls = 0
  await withHttpGlobals(async () => {
    calls++
    return jsonResponse({ detail: {
      code: 'command_in_progress', message: 'resume is still executing',
      remediation: 'wait for the existing command to finish', existing_command_id: existing,
    } }, 409)
  }, async () => {
    await assert.rejects(
      runCommand('demo', 'pause', {}, { submitRetries: 0 }),
      error => error.status === 409 && error.code === 'command_in_progress'
        && error.commandId === undefined && error.existingCommandId === existing
        && /resume is still executing/.test(error.message),
    )
  })
  assert.equal(calls, 1)
})

test('cross-action conflict reference is remediation only, never the requested failure record id', () => {
  const error = Object.assign(new Error('resume is still executing'), {
    status: 409, code: 'command_in_progress', existingCommandId: 'cmd_active_pause',
    remediation: 'wait for the active pause command',
  })
  const record = commandFailureRecord(error)
  assert.equal(record.id, undefined)
  assert.equal(record.status, 'failed')
  assert.equal(record.error.existing_command_id, 'cmd_active_pause')
  assert.equal(record.error.remediation, 'wait for the active pause command')
})

test('bounded wait preserves the observed accepted state and structured failures stay actionable', async () => {
  await withHttpGlobals(async () => jsonResponse({ id: 'cmd-slow', status: 'accepted', event_type: 'run_abort' }, 202), async () => {
    const result = await runCommand('demo', 'run_abort', {}, { waitMs: 0, submitRetries: 0 })
    assert.deepEqual(result, { id: 'cmd-slow', status: 'accepted', event_type: 'run_abort' })
    assert.equal(commandFeedback(result, { executing: 'Finalize requested — waiting' }).message,
      'Finalize requested — waiting')
  })

  const failed = { id: 'cmd-fail', status: 'rejected', error: { code: 'invalid_transition',
    message: 'run is already finalizing', remediation: 'wait for finalization to finish' } }
  const feedback = commandFeedback(failed, { failure: 'Resume failed' })
  assert.equal(feedback.kind, 'error')
  assert.equal(feedback.message, 'Resume failed: run is already finalizing — wait for finalization to finish')
})

test('feedback reserves success for succeeded/noop and keeps executing non-terminal', () => {
  assert.deepEqual(commandFeedback({ status: 'succeeded' }, { success: 'done' }),
    { kind: 'success', terminal: true, status: 'succeeded', message: 'done' })
  assert.equal(commandFeedback({ status: 'noop' }, { noop: 'already done' }).kind, 'success')
  assert.deepEqual(commandFeedback({ status: 'executing' }, { executing: 'waiting' }),
    { kind: 'pending', terminal: false, status: 'executing', message: 'waiting' })
  assert.deepEqual(commandFeedback({ status: 'accepted' }, { executing: 'accepted' }),
    { kind: 'pending', terminal: false, status: 'accepted', message: 'accepted' })
})

test('CONTROL wrappers use only the command service, never legacy control/resume chains', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return jsonResponse({ id: `cmd-${calls.length}`, status: 'succeeded' })
  }, async () => {
    await CONTROL.finalize('demo')
    await CONTROL.resume('demo')
    await CONTROL.fork('demo', 7)
  })
  assert.equal(calls.length, 3)
  assert.ok(calls.every(call => call.url === '/proxy/app/api/runs/demo/commands'))
  assert.ok(calls.every(call => !call.url.includes('/control') && !call.url.endsWith('/resume')))
  assert.deepEqual(calls.map(call => JSON.parse(call.options.body).type), ['run_abort', 'resume', 'fork'])
})

test('idempotency fallback is UUID v4 shaped', () => {
  let value = 0
  const key = createIdempotencyKey({ getRandomValues(bytes) {
    for (let i = 0; i < bytes.length; i++) bytes[i] = value++
    return bytes
  } })
  assert.match(key, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
})

test('transport recovery persists the same key before command id and restores it after reload', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const idempotencyKey = '11111111-2222-4333-8444-555555555555'
  assert.equal(saveRunTransport('demo run', {
    action: 'finalize', idempotencyKey, record: { status: 'submitting' }, statusUnavailable: true,
  }, storage), true)
  assert.deepEqual(loadRunTransport('demo run', storage), {
    runId: 'demo run', action: 'finalize', expectedGeneration: GEN_A,
    idempotencyKey, commandId: '',
    record: { status: 'submitting' }, statusUnavailable: true, observationKind: null,
    retrying: false, checking: false, lastError: '', updatedAt: loadRunTransport('demo run', storage).updatedAt,
  })

  saveRunTransport('demo run', {
    action: 'finalize', idempotencyKey,
    record: { id: CMD_A, status: 'accepted', event_type: 'run_abort' }, statusUnavailable: false,
  }, storage)
  assert.equal(loadRunTransport('demo run', storage).commandId, CMD_A)
  assert.equal(clearRunTransport('demo run', storage), true)
  assert.equal(loadRunTransport('demo run', storage), null)
})

test('assistant direct recovery stores the key before POST, a safe numeric arg, and no payload or token', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const idempotencyKey = '12345678-1234-4123-8123-123456789abc'
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'approve', arg: 12, idempotencyKey, record: { status: 'submitting' },
  }, storage), true)
  const saved = loadAssistantRunTransport('demo', storage)
  assert.equal(saved.action, 'approve')
  assert.equal(saved.arg, 12)
  assert.equal(saved.idempotencyKey, idempotencyKey)
  assert.equal(saved.commandId, '')
  assert.equal('data' in saved, false)
  assert.equal('token' in saved, false)
  assert.deepEqual(loadRunCommandLock('demo', storage), {
    runId: 'demo', source: 'assistant', action: 'approve', expectedGeneration: GEN_A,
    idempotencyKey,
    commandId: '', status: 'submitting', statusUnavailable: false,
    updatedAt: loadRunCommandLock('demo', storage).updatedAt,
  })
})

test('accepted and executing assistant records both continue observation; terminal records do not poll', async () => {
  const calls = []
  const observe = async (runId, commandId) => {
    calls.push([runId, commandId])
    return { id: commandId, status: 'executing' }
  }
  assert.equal((await pollAssistantDirectOnce('demo', { id: 'cmd-a', status: 'accepted' }, { observe })).status, 'executing')
  await pollAssistantDirectOnce('demo', { id: 'cmd-b', status: 'executing' }, { observe })
  const terminal = { id: 'cmd-c', status: 'succeeded' }
  assert.equal(await pollAssistantDirectOnce('demo', terminal, { observe }), terminal)
  assert.deepEqual(calls, [['demo', 'cmd-a'], ['demo', 'cmd-b']])
})

test('lost-response remount recovers one logical assistant intent with the stored key and no double submit', async () => {
  const calls = []
  let release
  const response = new Promise(resolve => { release = resolve })
  const execute = (...args) => { calls.push(args); return response }
  const key = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
  const first = submitAssistantDirect('demo', 'approve', 7, key, {
    expectedGeneration: GEN_A, execute,
  })
  const remount = submitAssistantDirect('demo', 'approve', 7, key, {
    expectedGeneration: GEN_A, execute,
  })
  assert.equal(calls.length, 0, 'submission starts in a microtask so simultaneous remounts can join it')
  await Promise.resolve()
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0].slice(0, 3), ['demo', 'approval_granted', { node_id: 7 }])
  assert.equal(calls[0][3].idempotencyKey, key)
  assert.equal(calls[0][3].expectedGeneration, GEN_A)
  assert.equal(calls[0][3].waitMs, 0)
  assert.equal(calls[0][3].submitRetries, 0)
  release({ id: 'cmd-same', status: 'accepted' })
  assert.deepEqual(await first, await remount)
})

test('Assistant id-less recovery keeps its persisted generation after a newer one is observed', async () => {
  const calls = []
  observeRunGeneration('assistant-recovery-generation', GEN_B)
  const result = await submitAssistantDirect(
    'assistant-recovery-generation', 'resume', null, 'persisted-generation-key', {
      expectedGeneration: GEN_A,
      execute: async (...args) => {
        calls.push(args)
        return { id: CMD_A, status: 'accepted', event_type: 'resume' }
      },
    },
  )
  assert.equal(result.id, CMD_A)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][3].expectedGeneration, GEN_A)
})

test('terminal assistant result cleanup removes both recovery record and shared pending lock', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const key = 'fedcba98-7654-4321-8765-0123456789ab'
  saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: key,
    record: { id: CMD_A, status: 'succeeded', event_type: 'resume' },
  }, storage)
  assert.equal(loadRunCommandLock('demo', storage), null)
  assert.ok(loadAssistantRunTransport('demo', storage), 'terminal record can be cleaned after rendering feedback')
  clearAssistantRunTransport('demo', storage)
  assert.equal(loadAssistantRunTransport('demo', storage), null)
  assert.equal(loadRunCommandLock('demo', storage), null)
})

test('same-tab lock events synchronize Dock and Assistant without exposing command identity in the event', () => {
  const previous = { window: globalThis.window, sessionStorage: globalThis.sessionStorage }
  const values = new Map()
  globalThis.window = new EventTarget()
  globalThis.sessionStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const observed = []
  const stop = subscribeRunCommandLock('demo', lock => observed.push(lock))
  try {
    saveAssistantRunTransport('demo', {
      action: 'resume', idempotencyKey: 'event-key',
      record: { id: CMD_A, status: 'accepted', event_type: 'resume' },
    })
    assert.equal(observed.length, 1)
    assert.equal(observed[0].source, 'assistant')
    assert.equal(observed[0].commandId, CMD_A)
    clearAssistantRunTransport('demo')
    assert.equal(observed.length, 2)
    assert.equal(observed[1], null)
  } finally {
    stop()
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
})

test('a pending lock from the other surface cannot be overwritten by stale recovery state', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  saveRunTransport('demo', {
    action: 'stop', idempotencyKey: 'dock-key',
    record: { id: CMD_A, status: 'executing', event_type: 'pause' },
  }, storage)
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'assistant-key', record: { status: 'submitting' },
  }, storage), false)
  assert.equal(loadRunCommandLock('demo', storage).source, 'dock')
  assert.equal(loadAssistantRunTransport('demo', storage), null)
})

test('assistant direct intent registry only reconstructs allow-listed deterministic data', () => {
  assert.deepEqual(assistantDirectIntent('finalize'), { type: 'run_abort', data: { reason: 'finalized' } })
  assert.deepEqual(assistantDirectIntent('approve', 9), { type: 'approval_granted', data: { node_id: 9 } })
  assert.throws(() => assistantDirectIntent('approve', 'not-a-node'), /valid node id/)
  assert.throws(() => assistantDirectIntent('inject', null), /Unsupported direct command/)
})

test('strict storage persists no server free text, raw lastError, payload, or JSON-like secret', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'sanitize-key', commandId: CMD_A,
    record: {
      id: CMD_A, status: 'failed', event_type: 'resume',
      data: { nested_marker: 'MUST_NOT_PERSIST' }, token: 'MUST_NOT_PERSIST',
      error: {
        code: 'engine_failed', message: String.raw`engine failed: {\"password\":\"MARKER\"}`,
        remediation: 'api_key=another-secret retry later', retryable: true,
        unknown: { nested_marker: 'MUST_NOT_PERSIST' },
      },
    },
    lastError: 'password=raw-last-error MUST_NOT_PERSIST',
  }, storage), true)
  const raw = [...values.entries()].find(([key]) => key.startsWith('ll.assistant-command-transport.'))?.[1]
  assert.ok(raw)
  assert.equal(raw.includes('MUST_NOT_PERSIST'), false)
  assert.equal(raw.includes('MARKER'), false)
  assert.equal(raw.includes('password'), false)
  assert.equal(raw.includes('another-secret'), false)
  assert.equal(raw.includes('remediation'), false)
  assert.equal(raw.includes('message'), false)
  assert.equal(raw.includes('lastError'), false)
  const payload = JSON.parse(raw)
  assert.deepEqual(Object.keys(payload.record).sort(), ['error', 'event_type', 'id', 'status'])
  assert.deepEqual(Object.keys(payload.record.error).sort(), ['code', 'retryable'])
  assert.deepEqual(payload.record.error, { code: 'engine_failed', retryable: true })
  assert.equal(commandCanRetry(loadAssistantRunTransport('demo', storage).record), true)
  assert.match(commandFeedback(loadAssistantRunTransport('demo', storage).record).message,
    /The run engine reported a failure/)
})

test('malformed, unknown, extra-field, cross-wired, and mismatched stored envelopes fail closed', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'seed-key', record: { status: 'submitting' },
  }, storage)
  const envelopeKey = [...values.keys()].find(key => key.startsWith('ll.assistant-command-transport.'))
  const base = {
    runId: 'demo', action: 'resume', arg: null, idempotencyKey: 'seed-key', commandId: CMD_A,
    expectedGeneration: GEN_A,
    record: { id: CMD_A, status: 'accepted', event_type: 'resume' },
    statusUnavailable: false, observationKind: null, retrying: false, checking: false,
    updatedAt: 1, committed: true,
  }
  const cases = [
    '{not-json',
    JSON.stringify({ ...base, record: { ...base.record, status: 'mystery' } }),
    JSON.stringify({ ...base, record: { ...base.record, event_type: 'pause' } }),
    JSON.stringify({ ...base, commandId: CMD_B }),
    JSON.stringify({ ...base, record: { ...base.record, unknown_nested: 'marker' } }),
    JSON.stringify({ ...base, unknown_top_level: 'marker' }),
  ]
  for (const raw of cases) {
    values.set(envelopeKey, raw)
    const recovered = loadAssistantRunTransport('demo', storage)
    assert.equal(recovered.protocolInvalid, true, raw)
    assert.equal(recovered.statusUnavailable, true, raw)
    assert.equal(recovered.observationKind, 'protocol', raw)
    assert.equal(recovered.canResubmit, false, raw)
  }
})

test('Dock uses the same fail-closed envelope contract', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  saveRunTransport('demo', {
    action: 'stop', idempotencyKey: 'dock-envelope', record: { status: 'submitting' },
  }, storage)
  const key = [...values.keys()].find(value => value.startsWith('ll.command-transport.'))
  values.set(key, JSON.stringify({
    runId: 'demo', action: 'stop', idempotencyKey: 'dock-envelope', commandId: CMD_A,
    expectedGeneration: GEN_A,
    record: { id: CMD_A, status: 'accepted', event_type: 'resume' },
    statusUnavailable: false, observationKind: null, retrying: false, checking: false,
    updatedAt: 1, committed: true,
  }))
  const recovered = loadRunTransport('demo', storage)
  assert.equal(recovered.protocolInvalid, true)
  assert.equal(recovered.canResubmit, false)
  assert.equal(recovered.commandId, CMD_A)
})

test('action aliases have exact event identities and cross-wired records are rejected', () => {
  assert.equal(commandEventForAction('stop'), 'pause')
  assert.equal(commandEventForAction('pause'), 'pause')
  assert.equal(commandEventForAction('finalize'), 'run_abort')
  assert.equal(commandEventForAction('abort'), 'run_abort')
  assert.equal(commandRecordMatchesAction({ id: CMD_A, status: 'accepted', event_type: 'pause' }, 'pause'), true)
  assert.equal(commandRecordMatchesAction({ id: CMD_A, status: 'accepted', event_type: 'run_abort' }, 'pause'), false)
})

test('shared lock ownership requires exact source, key, action, and known command id', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const initial = { source: 'dock', action: 'stop', idempotencyKey: 'lock-key',
    record: { status: 'submitting' } }
  assert.equal(saveRunCommandLock('demo', initial, storage), true)
  assert.equal(saveRunCommandLock('demo', { ...initial, idempotencyKey: 'other-key' }, storage), false)
  assert.equal(saveRunCommandLock('demo', { ...initial, source: 'assistant' }, storage), false)
  assert.equal(saveRunCommandLock('demo', { ...initial, action: 'resume' }, storage), false)
  assert.equal(saveRunCommandLock('demo', { ...initial, record: {
    id: CMD_A, status: 'accepted', event_type: 'pause',
  } }, storage), true, 'same identity may learn its durable id')
  assert.equal(saveRunCommandLock('demo', { ...initial, record: {
    id: CMD_B, status: 'accepted', event_type: 'pause',
  } }, storage), false)
  assert.equal(loadRunCommandLock('demo', storage).commandId, CMD_A)
  assert.equal(saveRunCommandLock('demo', initial, storage), true)
  assert.equal(loadRunCommandLock('demo', storage).commandId, CMD_A, 'known identity cannot be downgraded')
  assert.equal(clearRunCommandLock('demo', {
    source: 'dock', action: 'stop', idempotencyKey: 'lock-key',
  }, storage), false, 'id-less cleanup cannot remove a lock with a known durable id')
  assert.equal(clearRunCommandLock('demo', {
    source: 'dock', action: 'stop', idempotencyKey: 'lock-key', commandId: '',
  }, storage), false)
  assert.equal(clearRunCommandLock('demo', {
    source: 'dock', action: 'stop', idempotencyKey: 'lock-key', commandId: CMD_B,
  }, storage), false)
  assert.equal(clearRunCommandLock('demo', {
    source: 'dock', action: 'stop', idempotencyKey: 'lock-key', commandId: CMD_A,
  }, storage), true)
  assert.equal(loadRunCommandLock('demo', storage), null)
})

test('a durable envelope cannot downgrade a command id already learned by its lock', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const start = { action: 'resume', idempotencyKey: 'learned-id', record: { status: 'submitting' } }
  assert.equal(saveAssistantRunTransport('demo', start, storage), true)
  assert.equal(saveRunCommandLock('demo', {
    source: 'assistant', action: 'resume', idempotencyKey: 'learned-id',
    record: { id: CMD_A, status: 'accepted', event_type: 'resume' },
  }, storage), true)
  assert.equal(saveAssistantRunTransport('demo', start, storage), false)
  assert.equal(loadRunCommandLock('demo', storage).commandId, CMD_A)
})

test('retry conflict with a different active command is propagated without reading the old failed id', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return jsonResponse({ detail: {
      code: 'command_in_progress', message: 'pause is executing', existing_command_id: CMD_B,
      remediation: 'wait for the active pause',
    } }, 409)
  }, async () => {
    await assert.rejects(
      retryRunCommand('demo', CMD_A, { waitMs: 0 }),
      error => error.code === 'command_in_progress' && error.existingCommandId === CMD_B,
    )
  })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].options.method, 'POST')
})

test('structured retry conflict naming the same id remains authoritative and never reads the old record', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return jsonResponse({ detail: {
      code: 'command_in_progress', message: 'retry already executing', existing_command_id: CMD_A,
    } }, 409)
  }, async () => {
    await assert.rejects(retryRunCommand('demo', CMD_A, { waitMs: 0 }),
      error => error.status === 409 && error.code === 'command_in_progress')
  })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].options.method, 'POST')
})

test('structured retry conflict without a valid existing id never falls back to the old failed record', async () => {
  for (const existing_command_id of [undefined, 'malformed-id']) {
    const calls = []
    await withHttpGlobals(async (url, options = {}) => {
      calls.push({ url, options })
      return jsonResponse({ detail: {
        code: 'command_in_progress', message: 'another command is active',
        ...(existing_command_id === undefined ? {} : { existing_command_id }),
      } }, 409)
    }, async () => {
      await assert.rejects(retryRunCommand('demo', CMD_A, { waitMs: 0 }),
        error => error.status === 409 && error.code === 'command_in_progress')
    })
    assert.equal(calls.length, 1, String(existing_command_id))
  }
})

test('deferred command A cleans up without presenting its result after navigation to B', async () => {
  let currentRun = 'A', presented = 0, cleaned = 0, release
  const deferred = new Promise(resolve => { release = resolve })
  const result = deferred.then(() => {
    cleaned++
    presentAssistantCommandResult(currentRun, 'A', () => { presented++ })
  })
  currentRun = 'B'
  release()
  await result
  assert.equal(cleaned, 1)
  assert.equal(presented, 0)
  assert.equal(assistantRunChanged('A', 'B'), true)
  assert.equal(assistantRunChanged('B', 'B'), false)
})

test('only an exact id-less own lock is treated as an unsent Assistant storage quarantine', () => {
  const failure = { runId: 'demo', name: 'resume', idempotencyKey: 'storage-key',
    expectedGeneration: GEN_A,
    record: { status: 'rejected', error: { code: 'command_storage_unavailable', retryable: false } } }
  const lock = { runId: 'demo', source: 'assistant', action: 'resume',
    expectedGeneration: GEN_A, idempotencyKey: 'storage-key', commandId: '' }
  assert.equal(assistantStorageFailureOwnsLock(failure, lock), true)
  assert.equal(assistantStorageFailureOwnsLock(failure, { ...lock, source: 'dock' }), false)
  assert.equal(assistantStorageFailureOwnsLock(failure, { ...lock, idempotencyKey: 'other-key' }), false)
  assert.equal(assistantStorageFailureOwnsLock(failure, { ...lock, expectedGeneration: GEN_B }), false)
  assert.equal(assistantStorageFailureOwnsLock(failure, { ...lock, commandId: CMD_A }), false,
    'a durable accepted command must never be ignored')
  assert.equal(assistantStorageFailureOwnsLock({ ...failure, record: { status: 'failed', error: {
    code: 'engine_failed', retryable: true,
  } } }, lock), false)
})

test('throwing storage prevents both Assistant and Dock command submission', async () => {
  const throwingStorage = {
    getItem: () => null,
    setItem: () => { throw new DOMException('quota exceeded', 'QuotaExceededError') },
    removeItem: () => {},
  }
  let posts = 0
  const submit = async () => { posts++ }
  if (saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'assistant-storage-fail', record: { status: 'submitting' },
  }, throwingStorage)) await submit()
  if (saveRunTransport('demo', {
    action: 'resume', idempotencyKey: 'dock-storage-fail', record: { status: 'submitting' },
  }, throwingStorage)) await submit()
  assert.equal(posts, 0)
})

test('partial storage commit is quarantined and cannot be auto-resubmitted after reload', () => {
  const partialStorage = () => {
    const values = new Map()
    return {
      values,
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => {
        if (key.startsWith('ll.command-lock.')) throw new DOMException('lock write failed', 'QuotaExceededError')
        values.set(key, value)
      },
      // Simulate the rollback failing after the staged envelope was written.
      removeItem: () => { throw new DOMException('rollback failed', 'QuotaExceededError') },
    }
  }

  const assistantStorage = partialStorage()
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'assistant-partial', record: { status: 'submitting' },
  }, assistantStorage), false)
  const assistantRaw = [...assistantStorage.values.values()][0]
  assert.equal(JSON.parse(assistantRaw).committed, false)
  const assistantRecovery = loadAssistantRunTransport('demo', assistantStorage)
  assert.equal(assistantRecovery.protocolInvalid, true)
  assert.equal(assistantRecovery.canResubmit, false)
  assert.equal(assistantRecovery.record.status, 'submitting')

  const dockStorage = partialStorage()
  assert.equal(saveRunTransport('demo', {
    action: 'resume', idempotencyKey: 'dock-partial', record: { status: 'submitting' },
  }, dockStorage), false)
  const dockRaw = [...dockStorage.values.values()][0]
  assert.equal(JSON.parse(dockRaw).committed, false)
  const dockRecovery = loadRunTransport('demo', dockStorage)
  assert.equal(dockRecovery.protocolInvalid, true)
  assert.equal(dockRecovery.canResubmit, false)
  assert.equal(dockRecovery.record.status, 'submitting')

  const values = new Map()
  let envelopeWrites = 0
  const commitFailureStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => {
      if (key.startsWith('ll.assistant-command-transport.')) {
        envelopeWrites++
        if (envelopeWrites === 2) throw new DOMException('commit write failed', 'QuotaExceededError')
      }
      values.set(key, value)
    },
    removeItem: key => values.delete(key),
  }
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'assistant-commit-fail', record: { status: 'submitting' },
  }, commitFailureStorage), false)
  const staged = [...values.entries()].find(([key]) => key.startsWith('ll.assistant-command-transport.'))?.[1]
  assert.equal(JSON.parse(staged).committed, false)
  assert.ok(loadRunCommandLock('demo', commitFailureStorage), 'the matching lock remains conservative')
  const commitRecovery = loadAssistantRunTransport('demo', commitFailureStorage)
  assert.equal(commitRecovery.protocolInvalid, true)
  assert.equal(commitRecovery.canResubmit, false)
})

test('a SecurityError while obtaining sessionStorage is a safe persistence failure', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true, get() { throw new DOMException('blocked', 'SecurityError') },
  })
  try {
    assert.equal(saveAssistantRunTransport('demo', {
      action: 'resume', idempotencyKey: 'security-error', record: { status: 'submitting' },
    }), false)
    assert.equal(saveRunTransport('demo', {
      action: 'resume', idempotencyKey: 'security-error-dock', record: { status: 'submitting' },
    }), false)
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor)
    else delete globalThis.sessionStorage
  }
})

test('retryable Assistant failure remains durable across reload and retrying state owns the lock', () => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const failure = { id: CMD_A, status: 'timed_out', event_type: 'resume', error: {
    code: 'command_timeout', message: 'engine did not acknowledge', remediation: 'retry this command', retryable: true,
  } }
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'failure-key', record: failure,
  }, storage), true)
  assert.equal(commandCanRetry(loadAssistantRunTransport('demo', storage).record), true)
  assert.equal(loadRunCommandLock('demo', storage), null, 'settled failure does not block a different action')
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'failure-key', record: failure, retrying: true,
  }, storage), true)
  assert.equal(loadRunCommandLock('demo', storage).commandId, CMD_A)
  assert.equal(loadAssistantRunTransport('demo', storage).retrying, true)
  assert.equal(saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'failure-key', record: failure,
    statusUnavailable: true, observationKind: 'transport',
  }, storage), true)
  assert.equal(loadAssistantRunTransport('demo', storage).statusUnavailable, true)
})

test('a bounded submit timeout retains the key as ambiguous recovery state', async () => {
  await withHttpGlobals((_url, options = {}) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      const error = new Error('aborted'); error.name = 'AbortError'; reject(error)
    }, { once: true })
  }), async () => {
    const idempotencyKey = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    await assert.rejects(
      runCommand('demo', 'resume', {}, {
        idempotencyKey, requestTimeoutMs: 5, submitRetries: 0,
      }),
      error => error.code === 'COMMAND_REQUEST_TIMEOUT' && error.commandUnknown === true
        && error.idempotencyKey === idempotencyKey,
    )
  })
})
