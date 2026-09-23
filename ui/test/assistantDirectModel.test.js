// The assistant's direct run-control grammar, driven over its truth table (review 2026-09-22, UI-06).
// While these rules were module-private in the 4,000-line `AssistantBar.jsx`, the only way to reach
// them was to mount the component and type; none of the refusals below was covered.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  DIRECT, UNKNOWN_DIRECT_SPEC, directCopy, directSpec, parseDirect, preRoute,
} from '../src/assistantDirectModel.js'

test('a lone command fires directly; anything with trailing prose goes to the model', () => {
  assert.deepEqual(parseDirect('/stop'), { name: 'stop', spec: DIRECT.stop, arg: null })
  assert.equal(parseDirect('/STOP').name, 'stop', 'the command name is case-insensitive')
  assert.equal(parseDirect('/stop  ').name, 'stop', 'trailing whitespace is not prose')
  assert.equal(parseDirect('/stop please'), null)
  assert.equal(parseDirect('stop'), null, 'no slash, no direct command')
  assert.equal(parseDirect('/unknown'), null)
  assert.equal(parseDirect('/__proto__'), null, 'the table is read by own property only')
})

test('a node id is required where the command needs one and refused where it would be dropped', () => {
  assert.equal(parseDirect('/approve'), null, '/approve without a node is not a direct command')
  assert.deepEqual(parseDirect('/approve #12'), { name: 'approve', spec: DIRECT.approve, arg: 12 })
  assert.equal(parseDirect('/approve 12').arg, 12, 'the # is optional')
  assert.deepEqual(parseDirect('/stop #3'), {
    invalid: true,
    message: '/stop controls the whole run and does not accept #3. Remove the node id to continue.',
  }, 'a run-wide command never silently discards a node id')
})

test('the natural-language pre-router fires only when the phrase names the run', () => {
  for (const [phrase, name] of [
    ['stop the run', 'stop'], ['please finalize run.', 'finalize'], ['halt this run!', 'finalize'],
    ['continue the run', 'resume'], ['can you pause this run', 'stop'], ['wrapup run', 'finalize'],
  ]) {
    const routed = preRoute(phrase)
    assert.equal(routed?.name, name, phrase)
    assert.equal(routed.arg, null)
    assert.equal(routed.spec, directSpec(name))
  }
  for (const phrase of ['stop', 'continue', 'run', 'stop the build', 'constructor run', 'toString run']) {
    assert.equal(preRoute(phrase), null, phrase)
  }
})

test('the copy tables answer for every command, including a parametrised one', () => {
  assert.equal(directSpec('toString'), null)
  assert.equal(directCopy(DIRECT.approve.success, 7), '✓ approved #7')
  assert.equal(directCopy(DIRECT.stop.success), '⏸ run stopped (not finalized)')
  assert.deepEqual(Object.keys(UNKNOWN_DIRECT_SPEC).sort(), ['executing', 'noop', 'success'])
  for (const spec of Object.values(DIRECT)) {
    for (const key of ['success', 'noop', 'executing']) {
      assert.equal(typeof directCopy(spec[key], 1), 'string', key)
    }
  }
})
