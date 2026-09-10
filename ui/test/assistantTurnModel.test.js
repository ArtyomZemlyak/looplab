// `runLLM`'s six decisions (doc 25 UI-05), driven over their truth tables. Before the extraction all
// six were inline branch ladders in a 4,400-line component, reachable only by mounting it and
// driving a real SSE stream — so none of them was covered, including the two that decide whether a
// turn is sent into a LIVE PUBLIC LINK and whether a lost stream is retried or reconciled.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  finalReplyText, restoredComposerInput, sendAbandonReason, sendTurnBlock, shouldSurfaceProgress,
  terminalTurnOutcome,
} from '../src/assistantTurnModel.js'

test('the send gate refuses in one order and says exactly one thing each time', () => {
  assert.equal(sendTurnBlock(), null, 'an idle chat sends')
  assert.equal(sendTurnBlock({ historical: true, readOnlyMessage: 'read-only here' }).message,
    'read-only here', 'the read-only sentence is the caller\'s, not a second copy of it')
  assert.equal(sendTurnBlock({ sessionOpening: true }).code, 'opening')
  assert.equal(sendTurnBlock({ forkingSession: true }).code, 'forking')
  assert.equal(sendTurnBlock({ shareActionActive: true }).code, 'share_action')
  assert.equal(sendTurnBlock({ shareVerifying: true }).code, 'share_verifying')
  assert.equal(sendTurnBlock({ turnActive: true }).code, 'turn_active')
  // Read-only outranks everything: nothing may be sent into a run this tab may not mutate.
  assert.equal(sendTurnBlock({ historical: true, readOnlyMessage: 'ro', turnActive: true }).code,
    'read_only')

  // The one refusal that is not a dead end: unknown public-link state fails CLOSED and asks for the
  // read that can lift it. A turn sent into a live public link publishes the operator's next words.
  const unknown = sendTurnBlock({ shareUnknown: true })
  assert.equal(unknown.verifyShare, true)
  assert.match(unknown.message, /Nothing sent/)
  for (const key of ['historical', 'sessionOpening', 'forkingSession', 'shareActionActive',
                     'shareVerifying', 'turnActive']) {
    assert.equal(sendTurnBlock({ [key]: true, readOnlyMessage: 'ro' }).verifyShare, false,
      `${key} must not schedule a public-link read it did not ask for`)
  }
})

test('the second gate names the two facts the operator has to hear, and no others', () => {
  assert.equal(sendAbandonReason(), null)
  assert.deepEqual(sendAbandonReason({ commandClaimed: true }),
    { code: 'command_claimed', message: 'Draft not sent · a run command claimed this run first' })
  assert.deepEqual(sendAbandonReason({ readOnly: true }),
    { code: 'read_only', message: 'Draft not sent · run access became read-only' })
  // Ownership changes outrank the silent shapes: an unmounted bar cannot be told anything, but a
  // claimed run has to be reported to whoever comes back to the tab.
  assert.equal(sendAbandonReason({ mounted: false, commandClaimed: true }).code, 'command_claimed')
  for (const [key, code] of [['mounted', 'unmounted'], ['attemptCurrent', 'superseded'],
                             ['sessionCurrent', 'session_changed']]) {
    const reason = sendAbandonReason({ [key]: false })
    assert.equal(reason.code, code)
    assert.equal(reason.message, null, 'there is nobody to tell, so nothing is said')
  }
  assert.equal(sendAbandonReason({ sessionOpening: true }).message, null)
  assert.equal(sendAbandonReason({ directCapture: true }).code, 'direct_command')
})

test('the progress mirror fills the buffered gap and never fights a live stream', () => {
  assert.equal(shouldSurfaceProgress('', { active: true, text: 'thinking…' }), true)
  assert.equal(shouldSurfaceProgress('thinking… and more', { active: true, text: 'thinking…' }), false,
    'once the stream overtakes the mirror, the authoritative tokens win')
  assert.equal(shouldSurfaceProgress('ab', { active: true, text: 'ab' }), false, 'equal is not longer')
  assert.equal(shouldSurfaceProgress('', { active: false, text: 'stale' }), false,
    'an inactive turn has nothing live to mirror')
  assert.equal(shouldSurfaceProgress('', null), false)
  assert.equal(shouldSurfaceProgress('', { active: true }), false, 'no text is not progress')
})

test('a terminal frame is decided by the transcript, in one order', () => {
  const mine = { role: 'user', content: 'hi', turn_id: 't1' }
  const reply = { role: 'assistant', content: 'done' }
  assert.equal(terminalTurnOutcome([mine, reply], mine, 2).kind, 'reply')
  assert.equal(terminalTurnOutcome([mine, reply], mine, 2).reply, reply)
  assert.equal(terminalTurnOutcome([mine], mine, 2).kind, 'staged',
    'the user turn is durable and unanswered — retryable exactly once')
  assert.equal(terminalTurnOutcome([mine, { role: 'user', content: 'x', turn_id: 't2' }], mine, 2).kind,
    'unknown', "another tab's trailing user turn is never adopted as ours")
  assert.equal(terminalTurnOutcome([{ role: 'user', content: 'x', turn_id: 't2' }, reply], mine, 2).kind,
    'unknown', 'and neither is its reply')
  assert.equal(terminalTurnOutcome(null, mine, 2).kind, 'invalid')
  assert.equal(terminalTurnOutcome('not a transcript', mine, 2).kind, 'invalid')
  // An assistant message that says NOTHING is not a completed turn — and it is not a staged one
  // either, because the user turn is no longer the last message: the honest answer is `unknown`, and
  // the optimistic bubble stays marked for recovery rather than being retried over a durable row.
  assert.equal(terminalTurnOutcome([mine, { role: 'assistant', content: '' }], mine, 2).kind, 'unknown')
  // Below `priorLen` the reply cannot be ours: the transcript is shorter than what this turn wrote.
  assert.equal(terminalTurnOutcome([mine, reply], mine, 5).kind, 'unknown')
})

test('the bubble says the most authoritative thing it has, and never nothing', () => {
  assert.equal(finalReplyText({ streamedFailure: 'boom', result: { reply: 'r' }, streamed: 'acc' }),
    'boom', 'a streamed failure outranks a reply field')
  assert.equal(finalReplyText({ result: { reply: 'r' }, streamed: 'acc' }), 'r')
  assert.equal(finalReplyText({ streamed: 'acc' }), 'acc')
  assert.equal(finalReplyText({ result: { ok: false, error: 'no capacity' } }),
    'Assistant error: no capacity')
  assert.equal(finalReplyText(), '(no reply)')
  assert.equal(finalReplyText({ result: { ok: false } }), '(no reply)', 'never an empty bubble')
})

test('a refused draft comes back, and what was typed meanwhile is never discarded', () => {
  assert.equal(restoredComposerInput('draft', ''), 'draft')
  assert.equal(restoredComposerInput('draft', null), 'draft')
  assert.equal(restoredComposerInput('draft', 'draft'), 'draft', 'an unchanged composer is not doubled')
  assert.equal(restoredComposerInput('draft', 'draft\n\nmore'), 'draft\n\nmore',
    'an already-restored composer is left exactly as it is')
  assert.equal(restoredComposerInput('draft', 'typed while in flight'),
    'draft\n\ntyped while in flight', 'both survive, in the order they were written')
})
