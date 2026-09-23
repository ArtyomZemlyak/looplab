// What the browser accepts as a chat's public-link metadata, driven directly (review 2026-09-22,
// UI-06). Module-private in `AssistantBar.jsx` until the split, so the consistency rules below — which
// decide whether a session's "shared" badge and link count are shown at all — had no direct test.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ASSISTANT_SHARE_IDS_MAX, assistantLiveShareAckRequired, assistantLiveShareIds,
  assistantLiveShareRecoveryFailure, assistantShareIds, boundedAssistantShareIds,
  shareRecoveryScope, validAssistantShareFallback, validAssistantShareId, validAssistantShareMeta,
} from '../src/assistantShareMetaModel.js'
import { assistantForkTurnInProgress } from '../src/assistantForkModel.js'

const A = 'a'.repeat(32)
const B = 'b'.repeat(32)

test('a share id is 32 lowercase hex, and a list of them is bounded and duplicate-free', () => {
  assert.equal(validAssistantShareId(A), true)
  for (const bad of ['A'.repeat(32), 'a'.repeat(31), 'g'.repeat(32), 7, null]) {
    assert.equal(validAssistantShareId(bad), false, String(bad))
  }
  assert.deepEqual(boundedAssistantShareIds([A, B]), [A, B])
  assert.equal(boundedAssistantShareIds([A, A]), null, 'a duplicate poisons the whole list')
  assert.equal(boundedAssistantShareIds([A, 'nope']), null)
  assert.equal(boundedAssistantShareIds('not a list'), null)
  assert.equal(boundedAssistantShareIds(Array(ASSISTANT_SHARE_IDS_MAX + 1).fill(A)), null)
  assert.deepEqual(assistantShareIds({ share_ids: [A] }), [A])
  assert.deepEqual(assistantLiveShareIds({ live_share_ids: [] }), [])
  assert.equal(assistantShareIds({}), null)
})

test('share metadata is shown only when every field agrees with every other', () => {
  const unshared = { share_ids: [], live_share_ids: [], shared: false, share_count: 0,
    share_live: false, share_expires_at: null }
  const shared = { share_ids: [A, B], live_share_ids: [A], shared: true, share_count: 2,
    share_live: true, share_expires_at: 1_900_000_000 }
  assert.equal(validAssistantShareMeta(unshared), true)
  assert.equal(validAssistantShareMeta(shared), true)
  for (const [label, patch] of [
    ['a live id that is not a share id', { live_share_ids: ['c'.repeat(32)] }],
    ['shared disagrees with the ids', { shared: false }],
    ['the count disagrees with the ids', { share_count: 3 }],
    ['share_live disagrees with the live ids', { share_live: false }],
    ['a shared chat with no expiry', { share_expires_at: null }],
    ['a non-integer count', { share_count: 2.5 }],
  ]) {
    assert.equal(validAssistantShareMeta({ ...shared, ...patch }), false, label)
  }
  assert.equal(validAssistantShareMeta({ ...unshared, share_expires_at: 5 }), false,
    'an unshared chat cannot carry an expiry')
  assert.equal(validAssistantShareMeta(null), false)
})

test('the two conflict codes and the paused-turn record', () => {
  assert.equal(assistantLiveShareAckRequired({ status: 409, code: 'assistant_live_share_ack_required' }), true)
  assert.equal(assistantLiveShareAckRequired({ status: 400, code: 'assistant_live_share_ack_required' }), false)
  assert.equal(assistantForkTurnInProgress({ status: 409, code: 'assistant_turn_fork_in_progress' }), true)
  assert.equal(assistantForkTurnInProgress({ status: 409, code: 'other' }), false)
  assert.equal(assistantForkTurnInProgress(undefined), false)
  assert.equal(assistantLiveShareRecoveryFailure.blocked, false)
})

test('a kept link must belong to THIS deployment and name its own share id', () => {
  const previous = globalThis.location
  globalThis.location = { origin: 'https://lab.example', pathname: '/ui/' }
  try {
    assert.equal(shareRecoveryScope(), 'https://lab.example/ui/')
    const token = 'T'.repeat(43)
    const good = { shareId: A, expiresAt: 1_900_000_000,
      url: `https://lab.example/ui/#/assistant/shared/${A}.${token}` }
    assert.equal(validAssistantShareFallback(good), true)
    for (const [label, patch] of [
      ['another origin', { url: `https://evil.example/ui/#/assistant/shared/${A}.${token}` }],
      ['another path', { url: `https://lab.example/other/#/assistant/shared/${A}.${token}` }],
      ['a query string', { url: `https://lab.example/ui/?x=1#/assistant/shared/${A}.${token}` }],
      ['another share id', { url: `https://lab.example/ui/#/assistant/shared/${B}.${token}` }],
      ['a short token', { url: `https://lab.example/ui/#/assistant/shared/${A}.${'T'.repeat(42)}` }],
      ['no expiry', { expiresAt: 0 }],
      ['not a URL', { url: 'not a url' }],
    ]) {
      assert.equal(validAssistantShareFallback({ ...good, ...patch }), false, label)
    }
    assert.equal(validAssistantShareFallback(null), false)
  } finally {
    if (previous === undefined) delete globalThis.location
    else globalThis.location = previous
  }
})
