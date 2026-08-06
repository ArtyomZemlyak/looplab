import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  DELETE_FAILURE_CODES, sessionDeleteBlock, sessionDeleteFailure, sessionReadSuperseded,
} from '../src/assistantSessionModel.js'

const assistantSource = () => readFile(new URL('../src/AssistantBar.jsx', import.meta.url), 'utf8')

// One read of chat A, issued at epoch 7, owning the pending-open slot, with A visible.
const token = { seq: 7, id: 'a1b2c3d4e5f60718' }
const current = {
  mounted: true, sessionId: 'a1b2c3d4e5f60718', seq: 7, choiceSeq: 7,
  token, pendingToken: token, deleted: false, visibleSessionId: 'a1b2c3d4e5f60718',
}

test('a session read that is still current commits, with or without the visibility conjunct', () => {
  assert.equal(sessionReadSuperseded(current), null)
  assert.equal(sessionReadSuperseded({ ...current, requireVisible: true }), null)
})

test('each fact that can end a session read ends it, and names itself', () => {
  assert.equal(sessionReadSuperseded({ ...current, mounted: false }), 'unmounted')
  assert.equal(sessionReadSuperseded({ ...current, choiceSeq: 8 }), 'newer-choice')
  assert.equal(sessionReadSuperseded({ ...current, seq: 6 }), 'newer-choice')
  assert.equal(sessionReadSuperseded({ ...current, deleted: true }), 'deleted')
  assert.equal(sessionReadSuperseded({ ...current, pendingToken: { ...token } }), 'replaced')
  assert.equal(sessionReadSuperseded({ ...current, pendingToken: null }), 'replaced')
  assert.equal(
    sessionReadSuperseded({ ...current, requireVisible: true, visibleSessionId: 'ffffffffffffffff' }),
    'not-visible')
})

test('a tombstoned chat loses even when nothing else moved', () => {
  // Deleting a chat that is NOT the pending one leaves the epoch and the slot untouched, so the
  // tombstone is the only fact standing between a successful GET and a transcript being committed for
  // a chat whose deletion has already started.
  assert.equal(sessionReadSuperseded({ ...current, deleted: true, choiceSeq: 7, pendingToken: token }),
    'deleted')
})

test('the reasons are ordered most-authoritative first', () => {
  const doomed = {
    ...current, mounted: false, choiceSeq: 99, deleted: true, pendingToken: null,
    requireVisible: true, visibleSessionId: 'ffffffffffffffff',
  }
  assert.equal(sessionReadSuperseded(doomed), 'unmounted')
  assert.equal(sessionReadSuperseded({ ...doomed, mounted: true }), 'newer-choice')
  assert.equal(sessionReadSuperseded({ ...doomed, mounted: true, choiceSeq: 7 }), 'deleted')
  assert.equal(sessionReadSuperseded({ ...doomed, mounted: true, choiceSeq: 7, deleted: false }),
    'not-visible')
  assert.equal(sessionReadSuperseded({
    ...doomed, mounted: true, choiceSeq: 7, deleted: false, visibleSessionId: current.sessionId,
  }), 'replaced')
})

test('visibility is only consulted where the caller asks for it', () => {
  // The FIRST fence runs before `sid` has been replaced, so the visible chat is still the previous
  // one by construction: consulting it there would reject every legitimate switch from A to B.
  assert.equal(sessionReadSuperseded({ ...current, visibleSessionId: 'ffffffffffffffff' }), null)
  assert.equal(sessionReadSuperseded({ ...current, visibleSessionId: null }), null)
  // ...and once the transcript IS committed, a chat that is no longer visible must lose.
  assert.equal(sessionReadSuperseded({
    ...current, requireVisible: true, visibleSessionId: null,
  }), 'not-visible')
})

test('an observational read owns no slot, so nothing can replace it', () => {
  // The live-session observer deliberately reuses the current epoch and never takes the pending-open
  // slot. Comparing it against whatever else holds that slot would supersede a read that is still the
  // only authority for the transcript it is about to return.
  const observational = { ...current, token: null }
  assert.equal(sessionReadSuperseded({ ...observational, pendingToken: null }), null)
  assert.equal(sessionReadSuperseded({ ...observational, pendingToken: { seq: 9, id: 'other' } }), null)
  // Every other fact still applies to it.
  assert.equal(sessionReadSuperseded({ ...observational, choiceSeq: 8 }), 'newer-choice')
  assert.equal(sessionReadSuperseded({ ...observational, deleted: true }), 'deleted')
})

test('slot ownership is object identity, not the id the slot holds', () => {
  // Two reads of the SAME chat create two distinct tokens; the older one must lose. An id comparison
  // would let a superseded A-read commit over the newer A-read that replaced it.
  const newer = { seq: 8, id: current.sessionId }
  assert.equal(sessionReadSuperseded({ ...current, pendingToken: newer }), 'replaced')
})

test('a deletion is refused by the innermost cause the operator can act on', () => {
  const clear = {
    launchRecovery: null, isCurrentSession: true, shareActionOwned: false, forkOwned: false,
    openPending: false, turnActive: false, publiclyShared: false, alreadyDeleting: false,
  }
  assert.equal(sessionDeleteBlock(clear), null)
  assert.equal(sessionDeleteBlock(), null, 'no known blocker means no refusal')
  const ordered = [
    ['launchRecovery', { runId: 'demo' }, 'Check or release startup recovery for “demo” before deleting this chat'],
    ['shareActionOwned', true, 'Wait for the current share action before deleting this chat'],
    ['forkOwned', true, 'Wait for this chat to finish forking before deleting it'],
    ['openPending', true, 'Wait for this chat to finish opening before deleting it'],
    ['turnActive', true, 'Stop or finish the current Assistant turn before deleting this chat'],
    ['publiclyShared', true, 'Unshare this chat before deleting it so every public link is explicitly revoked'],
  ]
  // Each blocker set ALONE gives its own message...
  for (const [field, value, message] of ordered) {
    assert.equal(sessionDeleteBlock({ ...clear, [field]: value }).message, message, field)
  }
  // ...and with every LATER blocker also set, the earlier one still wins. That is the property: the
  // operator is told about the cause nearest to them, not whichever check was written first.
  for (let index = 0; index < ordered.length; index += 1) {
    const facts = { ...clear, alreadyDeleting: true }
    for (const [field, value] of ordered.slice(index)) facts[field] = value
    assert.equal(sessionDeleteBlock(facts).message, ordered[index][2], ordered[index][0])
  }
})

test('a public link must be revoked by an explicit act, never orphaned by a deletion', () => {
  const shared = { publiclyShared: true }
  assert.match(sessionDeleteBlock(shared).message, /^Unshare this chat before deleting it/)
  // A second click on a deletion already running is not a second deletion — and it must not be
  // allowed to overtake the unshare rule either.
  assert.match(sessionDeleteBlock({ ...shared, alreadyDeleting: true }).message, /^Unshare/)
  assert.deepEqual(sessionDeleteBlock({ alreadyDeleting: true }), { duplicate: true })
  assert.equal(sessionDeleteBlock({ alreadyDeleting: true }).message, undefined,
    'a duplicate click is silent: it has nothing to tell the operator')
})

test('unresolved startup recovery is only worth revealing on the chat that is on screen', () => {
  assert.equal(sessionDeleteBlock({ launchRecovery: { runId: 'demo' }, isCurrentSession: true })
    .revealRecovery, true)
  assert.equal(sessionDeleteBlock({ launchRecovery: { runId: 'demo' }, isCurrentSession: false })
    .revealRecovery, false)
  assert.equal(sessionDeleteBlock({ launchRecovery: {} }).message,
    'Check or release startup recovery before deleting this chat',
    'a transport with no run id names no run rather than an empty one')
})

test('every unconfirmed deletion tells the operator one story, in both places', () => {
  // The dialog detail and the toast were two nested ternaries a dozen lines apart. One table now, so
  // a row cannot gain a detail without a notice.
  assert.deepEqual(DELETE_FAILURE_CODES, ['assistant_delete_shared', 'assistant_delete_fork_in_progress',
    'assistant_delete_busy', 'assistant_delete_incomplete'])
  const seen = new Set()
  for (const code of DELETE_FAILURE_CODES) {
    for (const ambiguous of [false, true]) {
      for (const listedPresence of [null, true, false]) {
        const failure = sessionDeleteFailure({ code, ambiguous, listedPresence })
        assert.equal(typeof failure.notice, 'string')
        assert.equal(typeof failure.detail, 'string')
        assert.ok(failure.notice && failure.detail, code)
        // An authoritative code is the whole story: ambiguity about the REQUEST cannot change what the
        // server explicitly said about the chat.
        assert.deepEqual(failure, sessionDeleteFailure({ code }), code)
        seen.add(`${failure.notice}|${failure.detail}`)
      }
    }
  }
  assert.equal(seen.size, DELETE_FAILURE_CODES.length, 'each code owns its own pair')
  // ...and neither half of a pair may quietly degrade to the generic wording. When the server said
  // WHY, the toast has to carry that reason too — a dialog that says "wait for the fork" beside a
  // toast that says "could not confirm" is two different stories about one durable outcome.
  const generic = sessionDeleteFailure({})
  const notices = new Set()
  for (const code of DELETE_FAILURE_CODES) {
    const failure = sessionDeleteFailure({ code })
    assert.notEqual(failure.notice, generic.notice, `${code}: its toast fell back to the generic one`)
    assert.notEqual(failure.detail, generic.detail, `${code}: its dialog fell back to the generic one`)
    notices.add(failure.notice)
  }
  assert.equal(notices.size, DELETE_FAILURE_CODES.length, 'two codes may not share one toast')
  assert.equal(sessionDeleteFailure({ code: 'assistant_delete_shared' }).notice,
    'Unshare this chat before deleting it')
  assert.equal(sessionDeleteFailure({ code: 'toString' }).notice, 'Could not confirm chat deletion',
    'a prototype member is not a delete-failure code')
})

test('an unreadable session list is not evidence that a deletion landed', () => {
  // Three-valued on purpose: only an authoritative list showing the chat can say "still available".
  // The list projection intentionally skips unreadable or partially deleted session metadata, so a
  // failed read (`null`) must not be reported as absence.
  const present = sessionDeleteFailure({ ambiguous: true, listedPresence: true })
  const unread = sessionDeleteFailure({ ambiguous: true, listedPresence: null })
  const absent = sessionDeleteFailure({ ambiguous: true, listedPresence: false })
  assert.match(present.detail, /still appears available/)
  assert.match(unread.detail, /shown again for safety/)
  assert.deepEqual(absent, unread, 'absence from a list proves nothing that an unread list does not')
  assert.equal(sessionDeleteFailure({ ambiguous: false, listedPresence: true }).detail,
    'The chat was not deleted. It is shown again; you can cancel or try again.',
    'an authoritative failure is not an ambiguous one, whatever the list says')
  for (const failure of [present, unread, absent]) {
    assert.equal(failure.notice, 'Could not confirm chat deletion')
  }
})

test('every session-read await in AssistantBar goes through the shared fence', async () => {
  const source = await assistantSource()
  const open = source.slice(source.indexOf('const openSession ='),
    source.indexOf('openSessionRef.current = openSession'))
  // Doc 25 UI-05. The fence was written out longhand at all three awaits, differing in one conjunct.
  // What must hold is that no await re-derives it: a fourth site written by hand is how the missing
  // `requireVisible` (or the missing tombstone check) comes back.
  assert.doesNotMatch(open, /seq !== openSessionSeqRef\.current/,
    'a hand-written epoch comparison is a second opinion about when a read may commit')
  assert.equal((open.match(/readSuperseded\(/g) || []).length, 3,
    'exactly the three await sites call it, and each of the three still does')
  assert.match(open, /const readSuperseded = \(options = \{\}\) => sessionReadSuperseded\(\{[\s\S]*?mounted: mountedRef\.current, sessionId: id, seq, choiceSeq: openSessionSeqRef\.current,[\s\S]*?token: sessionRead, pendingToken: openSessionPendingRef\.current,[\s\S]*?visibleSessionId: sidRef\.current,[\s\S]*?deleted: sessionDeleteTombstonesRef\.current\.has\(String\(id\)\),/,
    'the fence must observe every fact, including the deletion tombstone')

  // The PROGRESS await is the one that runs after `sid` has been replaced, and it is the only one that
  // may also require the chat to still be visible. Anchoring on the await keeps that pairing checked.
  const progress = open.indexOf('await boundedRequest(signal => assistantProgress(id, { signal }), 8000)')
  const visibleFence = open.indexOf('readSuperseded({ requireVisible: true })')
  const transcriptRead = open.indexOf('await boundedRequest(signal => assistantGet(id, { signal }))')
  const firstFence = open.indexOf('if (readSuperseded()) return')
  assert.ok(transcriptRead >= 0 && firstFence > transcriptRead,
    'the transcript read must be fenced before it can replace the visible session')
  assert.ok(progress > firstFence && visibleFence > progress,
    'the progress read must be fenced after it returns, not reuse the transcript fence')
  assert.equal((open.match(/requireVisible: true/g) || []).length, 1,
    'only the post-commit await has a visible session to compare against')
})
