// The pure half of review 2026-09-22, UI-06 (`src/assistantTranscriptModel.js`): what a memoized
// transcript `Turn` compares, and the stable faces the bar hands it for per-message handlers. The
// mounted half — that a stream chunk really re-renders one Turn — is
// `assistantTranscriptRenders.test.js`; these are the rules it rests on, driven with no React.
import test from 'node:test'
import assert from 'node:assert/strict'

import { proposalLaunchChat } from '../src/launchProvenance.js'
import { latestHandlers, sameLaunchChat, turnPropsEqual } from '../src/assistantTranscriptModel.js'

const genesisChat = () => [
  { role: 'user', content: '/new tune the baseline' },
  { role: 'assistant', content: 'Here is a launch card.', proposals: [{ proposal_id: 'p1' }] },
  { role: 'user', content: 'and make it faster' },
]

test('launchChat is compared by its rows, because the bar rebuilds it on every render', () => {
  const messages = genesisChat()
  // The producer really does hand a fresh array per render — the reason identity cannot be the rule.
  const first = proposalLaunchChat(messages, 1)
  const again = proposalLaunchChat(messages, 1)
  assert.notEqual(first, again)
  assert.equal(sameLaunchChat(first, again), true, 'same rows, same chat')
  assert.notEqual(proposalLaunchChat([], 0), proposalLaunchChat([], 0), 'even the empty case is fresh')
  assert.equal(sameLaunchChat(proposalLaunchChat([], 0), proposalLaunchChat([], 0)), true)

  const edited = proposalLaunchChat([messages[0], { ...messages[1], content: 'A different card.' }], 1)
  assert.equal(sameLaunchChat(first, edited), false, 'a changed row is a changed chat')
  assert.equal(sameLaunchChat(first, proposalLaunchChat(messages, 2)), false, 'a longer chat differs')
  assert.equal(sameLaunchChat(first, [...first].reverse()), false, 'order is part of the chat')
  // Every field of a row counts — a field added to the row shape later is never compared away.
  assert.equal(sameLaunchChat([{ role: 'user', content: 'x' }],
    [{ role: 'user', content: 'x', display: 'y' }]), false)
  assert.equal(sameLaunchChat([{ role: 'user', content: 'x', display: 'y' }],
    [{ role: 'user', content: 'x', display: 'z' }]), false)
  assert.equal(sameLaunchChat(undefined, undefined), true, 'a transcript with no launch chat')
  assert.equal(sameLaunchChat(undefined, []), false)
  assert.equal(sameLaunchChat([null], [null]), true)
  assert.equal(sameLaunchChat([null], [{}]), false)
})

test('every other Turn prop is compared by identity', () => {
  const message = { role: 'assistant', content: 'done' }
  const onRetry = () => {}
  const base = { m: message, onRetry, readOnly: false, retryLabel: 'Retry', launchChat: [] }
  assert.equal(turnPropsEqual(base, { ...base, launchChat: [] }), true,
    'a settled message handed the same props across a chunk is skipped')
  assert.equal(turnPropsEqual(base, { ...base, m: { ...message } }), false,
    'a replaced message re-renders even when it reads the same: the streaming turn, every chunk')
  assert.equal(turnPropsEqual(base, { ...base, onRetry: () => {} }), false,
    'a new callback re-renders: stale closures are never kept by the comparison')
  assert.equal(turnPropsEqual(base, { ...base, onRetry: null }), false,
    'losing the Retry handler re-renders, so the button goes with it')
  assert.equal(turnPropsEqual(base, { ...base, readOnly: true }), false)
  assert.equal(turnPropsEqual(base, { ...base, retryLabel: 'Verify status' }), false)
  assert.equal(turnPropsEqual(base, { ...base, extra: undefined }), false, 'a prop that appears')
  const { onRetry: _dropped, ...withoutRetry } = base
  assert.equal(turnPropsEqual(base, { ...withoutRetry, other: onRetry }), false,
    'a prop that disappears, even when another takes its place')
  assert.equal(turnPropsEqual({ value: NaN }, { value: NaN }), true, 'Object.is, like React')
  assert.equal(turnPropsEqual({ value: 0 }, { value: -0 }), false, 'Object.is, like React')
})

test('a handler face is stable, calls the latest render, and is absent with its handler', () => {
  const handlers = latestHandlers()
  const calls = []
  handlers.publish([() => calls.push('render-1:0'), null, () => calls.push('render-1:2')])
  const first = handlers.face(0)
  assert.equal(typeof first, 'function')
  assert.equal(handlers.face(1), null, 'no handler, no face: the affordance stays absent')
  assert.equal(handlers.face(7), null, 'nothing published at that index')

  handlers.publish([(...args) => calls.push(['render-2:0', ...args]), () => calls.push('render-2:1')])
  assert.equal(handlers.face(0), first, 'the face survives the render — this is what the memo needs')
  first('event')
  assert.deepEqual(calls, [['render-2:0', 'event']], 'and it calls the LATEST render, arguments intact')
  assert.equal(typeof handlers.face(1), 'function', 'a handler that appears gets a face')
  assert.equal(handlers.face(2), null, 'a handler the latest render did not publish is gone')

  handlers.publish([])
  assert.equal(handlers.face(0), null)
  assert.doesNotThrow(() => first(), 'a face outliving its handler does nothing rather than throw')
  assert.equal(calls.length, 1)
  handlers.publish(undefined)
  assert.equal(handlers.face(0), null, 'a missing table is an empty one')
})
