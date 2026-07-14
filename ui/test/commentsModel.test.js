import assert from 'node:assert/strict'
import test from 'node:test'

import {
  COMMENT_MAX_BYTES, commentConflict, commentDraftState, commentMatchesSubject, commentMutationError,
  filterComments, normalizeComment, normalizeCommentHistory, normalizeCommentsPage,
  utf8Bytes, validUnicode,
} from '../src/commentsModel.js'

const GEN = 'a'.repeat(64)
const COMMENT_ID = `cmt_${'1'.repeat(32)}`

const current = (overrides = {}) => ({
  comment_id: COMMENT_ID,
  node_id: 7,
  node_generation: 2,
  text: 'Keep the exact attempt.',
  actor_kind: 'deployment_owner',
  actor_label: 'untrusted server label',
  version: 3,
  resolved: false,
  created_at: 10,
  updated_at: 12,
  legacy: false,
  editable: true,
  ...overrides,
})

test('comment normalization fixes generic attribution and preserves exact node attempt identity', () => {
  const normalized = normalizeComment(current())
  assert.deepEqual(normalized, {
    id: COMMENT_ID,
    nodeId: 7,
    nodeGeneration: 2,
    text: 'Keep the exact attempt.',
    actorKind: 'deployment_owner',
    actorLabel: 'Deployment owner',
    version: 3,
    resolved: false,
    createdAt: 10,
    updatedAt: 12,
    legacy: false,
    editable: true,
  })
  assert.equal(normalizeComment(current({ actor_kind: 'person@example.test' })), null,
    'unknown identities must not be relabelled as a trusted actor')
})

test('legacy notes keep unknown attempt/actor identity and are always read only', () => {
  const legacy = normalizeComment(current({
    comment_id: 'legacy_9', node_generation: null, actor_kind: 'deployment_owner',
    legacy: true, editable: true, version: 1,
  }))
  assert.equal(legacy.nodeGeneration, null)
  assert.equal(legacy.actorKind, 'legacy_unknown')
  assert.equal(legacy.actorLabel, 'Legacy note')
  assert.equal(legacy.editable, false)
  assert.equal(normalizeComment(current({ node_generation: null })), null)
})

test('draft validation enforces the 8 KiB UTF-8 boundary and strict Unicode', () => {
  assert.equal(commentDraftState('x'.repeat(COMMENT_MAX_BYTES)).valid, true)
  const multibyte = commentDraftState('🚀'.repeat((COMMENT_MAX_BYTES / 4) + 1))
  assert.equal(multibyte.tooLarge, true)
  assert.equal(multibyte.valid, false)
  assert.equal(validUnicode('\ud800'), false)
  assert.equal(commentDraftState('\ud800').invalidUnicode, true)
  assert.equal(commentDraftState('   ').valid, false)

  const previous = globalThis.TextEncoder
  try {
    globalThis.TextEncoder = undefined
    assert.equal(utf8Bytes('\ud800'), Number.POSITIVE_INFINITY)
  } finally { globalThis.TextEncoder = previous }
})

test('list and history envelopes fail closed on malformed rows, cursors, or generations', () => {
  const page = normalizeCommentsPage({
    comments: [current()], next_cursor: null, has_more: false, run_generation: GEN,
  }, GEN)
  assert.equal(page.comments.length, 1)
  assert.equal(normalizeCommentsPage({
    comments: [current({ version: 0 })], next_cursor: null, has_more: false,
    run_generation: GEN,
  }, GEN), null)
  assert.equal(normalizeCommentsPage({
    comments: [current()], next_cursor: 'cursor', has_more: false, run_generation: GEN,
  }, GEN), null)
  assert.equal(normalizeCommentsPage({
    comments: [current()], next_cursor: null, has_more: false, run_generation: 'b'.repeat(64),
  }, GEN), null)
  assert.equal(normalizeCommentsPage({
    comments: [current({ updated_at: 1e20 })], next_cursor: null, has_more: false,
    run_generation: GEN,
  }, GEN), null, 'an out-of-TimeClip timestamp must fail the whole authoritative page')
  assert.equal(normalizeCommentsPage({
    comments: [current({ created_at: 8.64e12, updated_at: 8.64e12 })],
    next_cursor: null, has_more: false, run_generation: GEN,
  }, GEN)?.comments.length, 1, 'the exact JavaScript Date boundary remains representable')

  const normalized = page.comments[0]
  const history = normalizeCommentHistory({
    comment_id: COMMENT_ID,
    versions: [{
      version: 3, action: 'edited', text: 'Keep the exact attempt.', resolved: false,
      actor_kind: 'local_operator', updated_at: 12, event_seq: 44,
    }],
    next_cursor: null,
    has_more: false,
    run_generation: GEN,
  }, normalized, GEN)
  assert.equal(history.versions[0].actorLabel, 'Local operator')
  assert.equal(history.versions[0].action, 'edited')
  assert.equal(normalizeCommentHistory({
    comment_id: COMMENT_ID,
    versions: [{
      version: 3, action: 'edited', text: 'unsafe date', resolved: false,
      actor_kind: 'local_operator', updated_at: 1e20,
    }],
    next_cursor: null, has_more: false, run_generation: GEN,
  }, normalized, GEN), null)
})

test('filters and CAS conflict copy keep the current projection safe', () => {
  const open = normalizeComment(current())
  const resolved = normalizeComment(current({ comment_id: `cmt_${'2'.repeat(32)}`, resolved: true }))
  assert.deepEqual(filterComments([open, resolved], 'open'), [open])
  assert.deepEqual(filterComments([open, resolved], 'resolved'), [resolved])
  const conflict = { code: 'comment_version_changed' }
  assert.equal(commentConflict(conflict), true)
  assert.match(commentMutationError(conflict), /draft is preserved/i)
  assert.match(commentMutationError({ code: 'event_lock_unavailable' }), /temporarily unavailable/i)
  assert.match(commentMutationError({ code: 'node_generation_changed' }), /attempt changed/i)
  assert.match(commentMutationError({ commandRecord: { status: 'accepted' } }),
    /outcome is not known.*draft is preserved/i)
})

test('node feeds accept only the exact non-legacy experiment attempt', () => {
  const exact = normalizeComment(current())
  const otherAttempt = normalizeComment(current({ node_generation: 3 }))
  const legacy = normalizeComment(current({
    comment_id: 'legacy_9', node_generation: null, actor_kind: 'legacy_unknown',
    legacy: true, editable: false, version: 1,
  }))
  assert.equal(commentMatchesSubject(exact, 7, 2), true)
  assert.equal(commentMatchesSubject(otherAttempt, 7, 2), false)
  assert.equal(commentMatchesSubject(legacy, 7, 2), false)
  assert.equal(commentMatchesSubject(exact, null, null), true, 'the run-wide feed is intentionally global')
})
