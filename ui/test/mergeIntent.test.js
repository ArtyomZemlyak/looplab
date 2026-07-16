import test from 'node:test'
import assert from 'node:assert/strict'

import {
  captureMergeIntent, mergeIntentCommand, mergeIntentMatches, selectMergeTarget,
} from '../src/mergeIntent.js'

const GEN_A = 'a'.repeat(64)
const GEN_B = 'b'.repeat(64)
const context = (overrides = {}) => ({
  runId: 'demo', runGeneration: GEN_A,
  nodes: { 7: { id: 7, attempt: 3 }, 8: { id: 8, attempt: 1 } },
  ...overrides,
})

test('merge intent snapshots the exact run generation and both node attempts', () => {
  const source = captureMergeIntent({ ...context(), sourceId: 7 })
  const pair = selectMergeTarget(source, context(), 8)
  assert.deepEqual(mergeIntentCommand(pair), {
    runId: 'demo', ids: [7, 8], parentGenerations: { 7: 3, 8: 1 }, expectedGeneration: GEN_A,
  })
  assert.equal(mergeIntentMatches(pair, context(), true), true)
})

test('merge intent fails closed after a run replacement or either attempt changes', () => {
  const pair = captureMergeIntent({ ...context(), sourceId: 7, targetId: 8 })
  assert.equal(mergeIntentMatches(pair, context({ runGeneration: GEN_B }), true), false)
  assert.equal(mergeIntentMatches(pair, context({ nodes: { 7: { attempt: 4 }, 8: { attempt: 1 } } }), true), false)
  assert.equal(mergeIntentMatches(pair, context({ nodes: { 7: { attempt: 3 }, 8: { attempt: 2 } } }), true), false)
})

test('target selection cannot rebind a source that drifted while the chooser was open', () => {
  const source = captureMergeIntent({ ...context(), sourceId: 7 })
  assert.equal(selectMergeTarget(source,
    context({ nodes: { 7: { attempt: 4 }, 8: { attempt: 1 } } }), 8), null)
  assert.equal(selectMergeTarget(source, context(), 7), null)
  assert.equal(selectMergeTarget(source, context(), 99), null)
})

test('malformed generations, ids, and attempts never create a merge intent', () => {
  assert.equal(captureMergeIntent({ ...context({ runGeneration: 'stale' }), sourceId: 7 }), null)
  assert.equal(captureMergeIntent({ ...context(), sourceId: 7.5 }), null)
  assert.equal(captureMergeIntent({ ...context({ nodes: { 7: { attempt: null } } }), sourceId: 7 }), null)
})
