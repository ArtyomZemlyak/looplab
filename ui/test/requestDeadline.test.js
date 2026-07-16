import test from 'node:test'
import assert from 'node:assert/strict'

import { deadlineRequest } from '../src/requestDeadline.js'

test('deadlineRequest preserves successful values and transport failures', async () => {
  const success = deadlineRequest(async signal => {
    assert.equal(signal.aborted, false)
    return { ok: true }
  }, 100)
  assert.deepEqual(await success.promise, { ok: true })
  assert.equal(success.controller.signal.aborted, false)

  const failure = new Error('transport failed')
  const rejected = deadlineRequest(() => Promise.reject(failure), 100)
  await assert.rejects(rejected.promise, error => error === failure)
  assert.equal(rejected.controller.signal.aborted, false)
})

test('deadlineRequest times out an ignored AbortSignal exactly once', async () => {
  let release
  let signal
  const request = deadlineRequest(currentSignal => {
    signal = currentSignal
    return new Promise(resolve => { release = resolve })
  }, 5)

  await assert.rejects(request.promise, error => error?.name === 'TimeoutError')
  assert.equal(request.timedOut(), true)
  assert.equal(signal.aborted, true)

  release('late transport result')
  await Promise.resolve()
  assert.equal(request.timedOut(), true)
})

test('deadlineRequest distinguishes caller cancellation from timeout', async () => {
  const request = deadlineRequest(() => new Promise(() => {}), 100)
  request.controller.abort()

  await assert.rejects(request.promise, error => error?.name === 'AbortError')
  assert.equal(request.timedOut(), false)
})
