// The card-control recovery verdict: which command statuses are failed, which are retryable, and
// what the operator is offered. Two lists that look interchangeable and are not.
import test from 'node:test'
import assert from 'node:assert/strict'

import { cardControlRecovery } from '../src/cardControlModel.js'

test('a rejected command is terminal and never offers a retry', () => {
  // THE ONE THAT MATTERS. The server refused this command; re-sending it refuses again, so a retry
  // button here is an infinite loop over a settled answer. `rejected` is in the FAILED list and not
  // in the RETRYABLE one, and inline that distinction was a one-token edit away from vanishing.
  const verdict = cardControlRecovery({ status: 'rejected' })
  assert.equal(verdict.failed, true)
  assert.equal(verdict.retryable, false)
  assert.equal(verdict.phase, 'failed')
  assert.equal(verdict.tone, 'error')
})

test('failed and timed_out are the states a retry can resolve', () => {
  for (const status of ['failed', 'timed_out']) {
    const verdict = cardControlRecovery({ status })
    assert.equal(verdict.retryable, true, status)
    assert.equal(verdict.failed, true, status)
    assert.equal(verdict.phase, 'retryable', `${status} names the affordance, not just the failure`)
    assert.equal(verdict.tone, 'error', status)
  }
})

test('anything not terminal is still waiting for the fold, in a pending tone', () => {
  for (const status of ['succeeded', 'executing', 'accepted', 'noop', undefined, null, '']) {
    const verdict = cardControlRecovery({ status })
    assert.equal(verdict.failed, false, String(status))
    assert.equal(verdict.retryable, false, String(status))
    assert.equal(verdict.phase, 'waiting-for-fold', String(status))
    assert.equal(verdict.tone, 'pending', `an unfinished command must not read as an error (${status})`)
  }
})

test('a missing record is not a failure', () => {
  for (const record of [null, undefined, {}, 'nope'])
    assert.deepEqual(cardControlRecovery(record),
      { failed: false, retryable: false, phase: 'waiting-for-fold', tone: 'pending' })
})
