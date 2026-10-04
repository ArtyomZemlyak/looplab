import test from 'node:test'
import assert from 'node:assert/strict'
import { seedReadError } from '../src/seedReadError.js'
import { nodeChip } from '../src/report.js'

test('base read recovery distinguishes timeout, stale identity, missing archive and failed integrity in both languages', () => {
  for (const ru of [false, true]) {
    const failures = [{ name: 'TimeoutError' }, { code: 'node_attempt_changed' },
      { code: 'seed_archive_unavailable' }, { code: 'seed_page_invalid' }, new TypeError('private connection secret')]
    const messages = failures.map(error => seedReadError(error, ru))
    assert.equal(new Set(messages).size, 5)
    assert.ok(messages.every(message => !message.includes('private connection secret')))
    assert.equal(seedReadError({ code: 'run_generation_conflict' }, ru), messages[1])
    assert.equal(seedReadError({ code: 'run_generation_changed' }, ru), messages[1])
    assert.match(messages[2], ru ? /восстановите исходные данные/ : /restore the original source/)
  }
})

test('independent roots do not claim to be a measured task baseline', () => {
  for (const id of [0, 1, 29]) {
    const node = { id, parent_ids: [], operator: 'draft', idea: { rationale: 'Momentum SGD' } }
    assert.match(nodeChip(node, { [id]: node }), /^initial experiment/)
    assert.doesNotMatch(nodeChip(node, { [id]: node }), /baseline/)
  }
})
