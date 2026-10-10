// doc 75 UX-28: a fresh server's Settings screen shows nothing customized.
//
// `/api/settings` returned `overrides: {}`, and the screen said "3 customized values", marked
// "Seed from a prior run" as differing from the engine default and enabled "Reset all": three defaults
// are the empty STRING and the form reads a blank field back as `null`. Driven with the three real
// fields' shapes and their real defaults, then the rule's truth table.
import test from 'node:test'
import assert from 'node:assert/strict'

import { differsFromDefault } from '../src/settingsSchema.js'

const text = { type: 'text' }

test('a blank field equals an empty-string default, whichever side is null', () => {
  for (const key of ['seed_from_run', 'developer_step_feedback_command', 'mlflow_tracking_uri']) {
    assert.equal(differsFromDefault(text, null, ''), false, key)
  }
  assert.equal(differsFromDefault(text, '', null), false)
  assert.equal(differsFromDefault(text, undefined, ''), false)
})

test('a real value still differs, and lists keep their empty default', () => {
  assert.equal(differsFromDefault(text, 'runs/a', ''), true)
  assert.equal(differsFromDefault(text, null, 'qwen3:8b'), true)
  assert.equal(differsFromDefault({ type: 'list' }, [], undefined), false)
  assert.equal(differsFromDefault({ type: 'list' }, ['a'], []), true)
  assert.equal(differsFromDefault({ type: 'number' }, 0, null), true, 'zero is a value, not blank')
})
