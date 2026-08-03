import test from 'node:test'
import assert from 'node:assert/strict'

import {
  assistantDirectDecision,
  assistantDirectPresentation,
} from '../src/assistantDirectPolicy.js'

test('direct run controls mirror the Assistant consequential-action mode policy', () => {
  assert.equal(assistantDirectDecision('plan'), 'deny')
  assert.equal(assistantDirectDecision('default'), 'ask')
  assert.equal(assistantDirectDecision('acceptEdits'), 'ask')
  assert.equal(assistantDirectDecision('auto'), 'inline')
  assert.equal(assistantDirectDecision('future-mode'), 'deny')
  assert.equal(assistantDirectDecision('__proto__'), 'deny')
  assert.equal(assistantDirectDecision('constructor'), 'deny')
})

test('direct confirmation copy names the exact command and run', () => {
  assert.deepEqual(
    assistantDirectPresentation('approve', 12, 'run-a'),
    {
      title: 'Approve this experiment?',
      actionLabel: 'Approve experiment',
      consequence: 'Accepts the currently pending experiment result so the run can continue.',
      commandText: '/approve #12',
      runId: 'run-a',
    },
  )
  const finalize = assistantDirectPresentation('finalize', null, 'run-b')
  assert.equal(finalize.commandText, '/finalize')
  assert.equal(finalize.runId, 'run-b')
  assert.equal(finalize.danger, true)
  assert.equal(assistantDirectPresentation('__proto__', null, 'run-c').actionLabel, 'Run command')
})
