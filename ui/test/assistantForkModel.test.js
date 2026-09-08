// The Assistant fork saga's four decisions (doc 25 UI-05), driven over their truth tables. Until the
// extraction these lived as branch ladders inside AssistantBar.jsx and NOTHING in the suite reached
// them: a fork that forgot a recoverable request, or adopted another tab's request over a different
// transcript, would have been found by an operator and not by a test.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  exhaustedForkStatus, forkSettlement, forkStartBlock, forkStatusVerdict, forkSubmitVerdict,
} from '../src/assistantForkModel.js'

const ACTION = '4e7e14ec-1959-4d67-bb55-643a2354c808'
const OTHER = '8b1d5a90-2f43-4c11-9f0e-77c2a6d4b331'

test('a fork may start on an idle chat and is refused, in order, by five facts', () => {
  assert.equal(forkStartBlock(), null)
  assert.equal(forkStartBlock({ turnBusy: false }), null)
  assert.equal(forkStartBlock({ actionActive: true, hasStoredRecovery: true }).code, 'fork_active',
    'a fork already in flight wins over everything, saved request included')
  assert.equal(forkStartBlock({ turnBusy: true }).message,
    'Wait for a complete Assistant reply before forking this chat')
  assert.equal(forkStartBlock({ deletingSession: true }).message, 'This chat is being deleted')
  assert.equal(forkStartBlock({ shareActionActive: true }).code, 'share_active')
  // The load-bearing ordering: a SAVED request beats the busy-turn gate. `check fork` is how an
  // interrupted fork is resolved, and the interruption is exactly what leaves the turn incomplete —
  // refusing here would strand the one request the recovery record exists to finish.
  assert.equal(forkStartBlock({ turnBusy: true, hasStoredRecovery: true }), null)
  assert.equal(forkStartBlock({ turnBusy: true, hasStoredRecovery: true, deletingSession: true }).code,
    'deleting', 'a saved request does not survive the chat being deleted')
})

test('a status poll asks again for its own pending fork and for ambiguous transport only', () => {
  const pending = forkStatusVerdict({ code: 'assistant_fork_in_progress' }, ACTION)
  assert.deepEqual(pending, { kind: 'pending', retry: true }, 'an unnamed in-flight fork may be ours')
  assert.deepEqual(
    forkStatusVerdict({ code: 'assistant_fork_in_progress', detail: { action_id: ACTION } }, ACTION),
    { kind: 'pending', retry: true })
  assert.deepEqual(
    forkStatusVerdict({ code: 'assistant_fork_in_progress', detail: { action_id: OTHER } }, ACTION),
    { kind: 'blocked', retry: false }, 'a NAMED other action is not ours to wait for')

  for (const [code, kind] of [['assistant_fork_deleted', 'deleted'],
                              ['assistant_fork_action_conflict', 'conflict'],
                              ['assistant_fork_deleting', 'deleting']]) {
    assert.deepEqual(forkStatusVerdict({ code }, ACTION), { kind, retry: false })
  }
  assert.deepEqual(forkStatusVerdict({ status: 404 }, ACTION), { kind: 'absent', retry: false })

  // Ambiguity is asked again, never reported as failure.
  for (const error of [{ name: 'TimeoutError' }, { name: 'AbortError' }, {}, { status: 503 },
                       { status: 500 }, { status: 408 }, { status: 425 }, { status: 429 }]) {
    assert.deepEqual(forkStatusVerdict(error, ACTION), { kind: 'transient', retry: true },
      `${JSON.stringify(error)} says nothing about whether the server forked`)
  }
  assert.deepEqual(forkStatusVerdict({ status: 403 }, ACTION), { kind: 'unknown', retry: false })

  // And what the spent attempts settle to: only the server's own "still working" changes it.
  assert.equal(exhaustedForkStatus(true), 'pending')
  assert.equal(exhaustedForkStatus(false), 'unknown')
})

test('the submit forgets a saved request only on an authoritative answer', () => {
  const recovery = { actionId: ACTION, expectedMessages: 4 }
  assert.equal(forkSubmitVerdict({ code: 'assistant_fork_session_deleting' }, recovery).kind, 'forget')
  assert.deepEqual(forkSubmitVerdict({ code: 'assistant_fork_deleted' }, recovery),
    { kind: 'forget', refresh: true, adopted: null,
      message: 'That fork was deleted · fork again to create a new copy' })
  assert.equal(forkSubmitVerdict({ code: 'assistant_fork_source_changed' }, recovery).refresh, true)
  assert.equal(forkSubmitVerdict({ code: 'assistant_fork_turn_active' }, recovery).kind, 'forget')
  assert.equal(forkSubmitVerdict({ status: 404 }, recovery).message,
    'This Assistant chat no longer exists')
  assert.equal(forkSubmitVerdict({ status: 400, code: 'whatever' }, recovery).message,
    'Could not fork this Assistant chat')

  // Every ambiguous shape reconciles instead: the server may already have forked.
  for (const error of [{ code: 'assistant_fork_in_progress' }, { code: 'assistant_fork_deleting' },
                       { code: 'assistant_fork_child_deleting' }, { name: 'TimeoutError' },
                       { name: 'AbortError' }, {}, { status: 502 }, { status: 429 }]) {
    assert.equal(forkSubmitVerdict(error, recovery).kind, 'reconcile',
      `${JSON.stringify(error)} must never forget a request that may have succeeded`)
  }
})

test("another tab's request is adopted only when it is provably the same snapshot", () => {
  const recovery = { actionId: ACTION, expectedMessages: 4 }
  const inProgress = detail => forkSubmitVerdict(
    { code: 'assistant_fork_in_progress', detail }, recovery)

  const adopted = inProgress({ action_id: OTHER.toUpperCase(), expected_messages: 4 })
  assert.equal(adopted.kind, 'adopt')
  assert.deepEqual(adopted.adopted, { actionId: OTHER, expectedMessages: 4 },
    'the adopted identity is normalized, and it is the SERVER\'s, not ours')
  assert.match(adopted.unstorableMessage, /refresh the chat list/)

  // A different message count is a different transcript — adopting it would present a child of a
  // conversation this tab never saw.
  assert.equal(inProgress({ action_id: OTHER, expected_messages: 5 }).kind, 'refuse')
  assert.equal(inProgress({ action_id: OTHER, expected_messages: -1 }).kind, 'refuse')
  assert.equal(inProgress({ action_id: OTHER, expected_messages: '4' }).kind, 'refuse')
  assert.equal(inProgress({ action_id: 'not-a-uuid', expected_messages: 4 }).kind, 'refuse')
  assert.match(inProgress({ action_id: OTHER, expected_messages: 5 }).message,
    /different chat snapshot/)
  // The server naming OUR OWN action is not another tab at all: it is our request, still running.
  assert.equal(inProgress({ action_id: ACTION, expected_messages: 4 }).kind, 'reconcile')
})

test('every outcome settles to one sentence, and only some of them re-read the chat list', () => {
  assert.equal(forkSettlement('created', { presented: true }), null,
    'a presented child needs no notice — the operator is looking at it')
  assert.deepEqual(forkSettlement('created'), { forget: false, refresh: true,
    message: 'Fork result is uncertain · check the chat list before retrying' })
  assert.equal(forkSettlement('deleted').forget, true)
  assert.equal(forkSettlement('conflict').forget, true)
  assert.equal(forkSettlement('deleting').forget, false)
  // The two outcomes that KEEP the request and read nothing: neither says anything new about the
  // session list, and both are resolved by pressing "check fork" again.
  for (const kind of ['absent', 'blocked']) {
    assert.deepEqual(forkSettlement(kind).refresh, false, kind)
    assert.equal(forkSettlement(kind).forget, false, kind)
    assert.match(forkSettlement(kind).message, /check fork/)
  }
  assert.match(forkSettlement('pending').message, /still finishing/)
  assert.match(forkSettlement('unknown').message, /uncertain/)
  // A kind nobody wrote down still gets the honest sentence rather than a crash or silence.
  assert.deepEqual(forkSettlement('a-kind-that-does-not-exist'), forkSettlement('unknown'))
  // No settlement ever forgets a request without also re-reading the list that proves it gone.
  for (const kind of ['created', 'deleted', 'conflict', 'deleting', 'absent', 'blocked', 'pending',
                      'unknown']) {
    const settled = forkSettlement(kind)
    if (settled?.forget) assert.equal(settled.refresh, true, kind)
  }
})
