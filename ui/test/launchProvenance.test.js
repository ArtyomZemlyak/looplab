import test from 'node:test'
import assert from 'node:assert/strict'

import { proposalLaunchChat } from '../src/launchProvenance.js'

test('proposal provenance starts at its latest explicit Genesis command', () => {
  const messages = [
    { role: 'user', content: 'unrelated secret discussion' },
    { role: 'assistant', content: 'old answer' },
    { role: 'user', content: '/new optimize recall' },
    { role: 'assistant', content: 'Which dataset?' },
    { role: 'user', content: 'Use fixtures/data.csv' },
    { role: 'tool', content: 'raw tool payload' },
    { role: 'assistant', content: 'proposal', proposals: [{ proposal_id: 'p1' }] },
  ]
  assert.deepEqual(proposalLaunchChat(messages, 6), [
    { role: 'user', content: '/new optimize recall' },
    { role: 'assistant', content: 'Which dataset?' },
    { role: 'user', content: 'Use fixtures/data.csv' },
    { role: 'assistant', content: 'proposal' },
  ])
})

test('separate proposals bind to their own command and unsafe history fails closed', () => {
  const messages = [
    { role: 'user', content: '/new first' },
    { role: 'assistant', content: 'first proposal', proposals: [{}] },
    { role: 'user', content: 'ordinary unrelated question' },
    { role: 'assistant', content: 'ordinary answer' },
    { role: 'user', content: '/genesis second' },
    { role: 'assistant', content: '', streaming: true },
    { role: 'assistant', content: 'second proposal', proposals: [{}] },
  ]
  assert.deepEqual(proposalLaunchChat(messages, 1).map(row => row.content), ['/new first', 'first proposal'])
  assert.deepEqual(proposalLaunchChat(messages, 6).map(row => row.content), ['/genesis second', 'second proposal'])
  assert.deepEqual(proposalLaunchChat(messages.slice(2, 4), 1), [])
  assert.deepEqual(proposalLaunchChat([
    { role: 'user', content: '/run alias' }, { role: 'assistant', content: 'ready' },
  ], 1).map(row => row.content), ['/run alias', 'ready'])
})

test('provenance exports only exact visible user and assistant content', () => {
  const messages = [
    { role: 'user', content: '/new exact\ntext\n[UI context: legacy raw path]',
      context: { files: [{ raw: 'no' }] } },
    { role: 'system', content: 'system secret' },
    { role: 'assistant', content: '   ' },
    { role: 'assistant', content: 'final', raw: 'provider payload' },
  ]
  assert.deepEqual(proposalLaunchChat(messages, 3), [
    { role: 'user', content: '/new exact\ntext' }, { role: 'assistant', content: 'final' },
  ])
})
