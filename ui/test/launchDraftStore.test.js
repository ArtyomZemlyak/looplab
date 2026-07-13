import test from 'node:test'
import assert from 'node:assert/strict'

import {
  clearLaunchDraftSession, launchDraftKey, launchDraftSession, removeLaunchDraft, retainLaunchDraft,
} from '../src/launchDraftStore.js'

test('draft identity is session/proposal scoped and never follows edited run name', () => {
  const args = { sessionId: 'chat-a', messageId: 'turn-7', messageIndex: 7,
    proposalId: 'proposal-1', proposalIndex: 0 }
  const key = launchDraftKey(args)
  assert.equal(key, launchDraftKey({ ...args, runId: 'renamed' }))
  assert.notEqual(key, launchDraftKey({ ...args, sessionId: 'chat-b' }))
  assert.notEqual(key, launchDraftKey({ ...args, proposalId: 'proposal-2' }))
  assert.equal(launchDraftSession(key), 'chat-a')
  const legacy = launchDraftKey({ ...args, proposalId: '' })
  assert.equal(legacy, launchDraftKey({ ...args, proposalId: '', runId: 'another-name' }))
})

test('exact editable JSON survives remount while token-like extras are discarded', () => {
  const key = launchDraftKey({ sessionId: 'chat', messageId: 'turn', proposalIndex: 0 })
  const malformed = '{\n  "half-edited": true,'
  const stored = retainLaunchDraft({}, key, {
    proposal_id: '', run_id: 'edited', source: 'task', task_file: '', task_json: malformed,
    settings_json: '{"max_nodes": 3}', rationale: 'why', setup_steps: ['inspect'],
    validationToken: 'must-not-retain', secretEnvelope: 'must-not-retain',
  })
  assert.equal(stored[key].task_json, malformed)
  assert.equal(stored[key].run_id, 'edited')
  assert.equal(stored[key].validationToken, undefined)
  assert.equal(stored[key].secretEnvelope, undefined)
})

test('draft cleanup is bounded and session-specific', () => {
  const keyA = launchDraftKey({ sessionId: 'a', messageId: 'm', proposalIndex: 0 })
  const keyB = launchDraftKey({ sessionId: 'b', messageId: 'm', proposalIndex: 0 })
  const draft = { run_id: 'r', source: 'task', task_json: '{}', settings_json: '{}', setup_steps: [] }
  const store = retainLaunchDraft(retainLaunchDraft({}, keyA, draft), keyB, draft)
  assert.deepEqual(Object.keys(clearLaunchDraftSession(store, 'a')), [keyB])
  assert.deepEqual(removeLaunchDraft(store, keyA), { [keyB]: store[keyB] })

  let capped = {}
  for (let index = 0; index < 4; index += 1) capped = retainLaunchDraft(capped, `k${index}`, draft, 2)
  assert.deepEqual(Object.keys(capped), ['k2', 'k3'])
})
