// PART V Phase 2c: unit tests for the per-node concept re-tag affordance — the pure input/prefill
// helpers and the durable `concept_tag_edited` command payload. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  MAX_NODE_CONCEPTS, nodeCanonicalConcepts, parseConceptTagsInput,
} from '../src/conceptChips.js'
import { CONTROL } from '../src/api.js'

const GEN_A = 'a'.repeat(64)

const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  json: async () => body, headers: { get: () => null },
})

const withHttpGlobals = async (fetchImpl, fn) => {
  const previous = {
    location: globalThis.location, fetch: globalThis.fetch, sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/proxy/app/', hash: '' }
  globalThis.sessionStorage = { getItem: () => '' }
  globalThis.fetch = (url, options = {}) => String(url).endsWith('/state') && options.method == null
    ? Promise.resolve(jsonResponse({ state: {}, seq: 0, generation: GEN_A }))
    : fetchImpl(url, options)
  try { return await fn() } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
}

test('parseConceptTagsInput normalizes, de-dupes, and reports dropped invalid tokens', () => {
  const { concepts, dropped } = parseConceptTagsInput(
    'loss/contrastive, architecture/moe\nloss/contrastive\n  \n!!!\ndata/synth,')
  // first-seen order preserved; the duplicate and the pure-whitespace line are not "dropped" tokens.
  assert.deepEqual(concepts, ['loss/contrastive', 'architecture/moe', 'data/synth'])
  assert.equal(dropped, 1)                          // "!!!" has no letter/number segment → invalid id
})

test('parseConceptTagsInput accepts both comma and newline separators and trims each token', () => {
  const { concepts } = parseConceptTagsInput('  loss/mnr  ,\n data/synth ')
  assert.deepEqual(concepts, ['loss/mnr', 'data/synth'])
})

test('parseConceptTagsInput handles empty / whitespace-only input as an explicit clear', () => {
  assert.deepEqual(parseConceptTagsInput(''), { concepts: [], dropped: 0 })
  assert.deepEqual(parseConceptTagsInput('   \n , '), { concepts: [], dropped: 0 })
  assert.deepEqual(parseConceptTagsInput(null), { concepts: [], dropped: 0 })
})

test('parseConceptTagsInput caps at the server field limit and counts the overflow as dropped', () => {
  const many = Array.from({ length: MAX_NODE_CONCEPTS + 5 }, (_, i) => `axis/c${i}`).join('\n')
  const { concepts, dropped } = parseConceptTagsInput(many)
  assert.equal(concepts.length, MAX_NODE_CONCEPTS)
  assert.equal(dropped, 5)
})

test('nodeCanonicalConcepts reads string OR numeric keys, canonicalizes and de-dupes', () => {
  const nodeConcepts = { 7: ['loss/old', 'architecture/moe', 'loss/old'] }
  const rename = { 'loss/old': 'loss/contrastive' }
  // node.id is numeric; the wire key may be either — both must resolve, and the retired id canonicalizes.
  assert.deepEqual(
    nodeCanonicalConcepts(nodeConcepts, 7, rename), ['loss/contrastive', 'architecture/moe'])
  assert.deepEqual(
    nodeCanonicalConcepts({ '7': ['loss/old'] }, 7, rename), ['loss/contrastive'])
  assert.deepEqual(nodeCanonicalConcepts({}, 7, rename), [])
  assert.deepEqual(nodeCanonicalConcepts({ 7: 'not-an-array' }, 7), [])
})

test('CONTROL.retagConcepts posts a generation-fenced concept_tag_edited command', async () => {
  const calls = []
  await withHttpGlobals(async (url, options = {}) => {
    calls.push({ url, options })
    return jsonResponse({ id: 'cmd-1', status: 'succeeded', event_type: 'concept_tag_edited' })
  }, async () => {
    const result = await CONTROL.retagConcepts(
      'demo', { nodeId: 7, nodeGeneration: 2, concepts: ['loss/contrastive', 'data/synth'] },
      { waitMs: 50, pollMs: 0, submitRetries: 0 })
    assert.equal(result.status, 'succeeded')
  })
  assert.equal(calls[0].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    type: 'concept_tag_edited',
    data: { node_id: 7, node_generation: 2, concepts: ['loss/contrastive', 'data/synth'] },
    expected_generation: GEN_A,
  })
})
