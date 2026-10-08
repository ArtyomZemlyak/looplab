// The inject FORM's rules (doc 73 §1.4, `src/injectNodeModel.js`): what the panel may offer and,
// above all, WHAT GETS SENT — optional keys left out, the parent fenced by the generation the
// operator saw, `uses` only naming produced artifacts.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildInjectPayload, injectCandidates, injectSubmitDecision, parseInjectParams,
} from '../src/injectNodeModel.js'
import { RUN_ROUTE_PANELS, REVIEW_SAFE_PANEL_NAMES } from '../src/runRouteState.js'

const state = {
  nodes: {
    0: { id: 0, status: 'evaluated', kind: 'artifact', attempt: 0, idea: { rationale: 'shards' } },
    1: { id: 1, status: 'evaluated', attempt: 2, idea: { rationale: 'baseline' } },
    2: { id: 2, status: 'pending', kind: 'artifact', attempt: 0, idea: { rationale: 'rebuild' } },
    3: { id: 3, status: 'failed', attempt: 0, idea: { rationale: 'oom' } },
    4: { id: 4, status: 'evaluated', attempt: 0, tombstoned: true, idea: {} },
  },
}
const ok = parseInjectParams('')
const draft = (over = {}) => ({ rationale: 'try a wider head', kind: 'experiment', uses: [],
  parentId: null, params: ok, ...over })

test('the candidates are evaluated experiments as parents and produced artifacts as inputs', () => {
  const c = injectCandidates(state)
  assert.deepEqual(c.parents.map(p => [p.id, p.attempt]), [[1, 2]])
  assert.deepEqual(c.artifacts.map(a => a.id), [0], 'pending #2 is not produced; #4 is deleted')
})

test('a plain experiment sends only its idea', () => {
  assert.deepEqual(buildInjectPayload({ state, draft: draft() }),
    { idea: { operator: 'inject', rationale: 'try a wider head' } })
})

test('an artifact with a parent carries the kind and the generation the operator saw', () => {
  assert.deepEqual(buildInjectPayload({ state, draft: draft({ kind: 'artifact', parentId: 1 }) }), {
    idea: { operator: 'inject', rationale: 'try a wider head' },
    parent_id: 1, parent_generations: { 1: 2 }, node_kind: 'artifact',
  })
})

test('a consumer names produced artifacts only', () => {
  assert.deepEqual(buildInjectPayload({ state, draft: draft({ uses: [0] }) }).uses, [0])
  assert.equal(injectSubmitDecision({ state, draft: draft({ uses: [2] }) }).code, 'unknown_artifact')
  assert.equal(buildInjectPayload({ state, draft: draft({ uses: [2] }) }), null)
})

test('the refusals come in order and the payload is never built past one', () => {
  assert.equal(injectSubmitDecision({ state, draft: draft({ rationale: '  ' }) }).code, 'no_rationale')
  assert.equal(injectSubmitDecision({ state, draft: draft({ kind: 'x' }) }).code, 'bad_kind')
  assert.equal(injectSubmitDecision({ state, draft: draft({ parentId: 3 }) }).code, 'unknown_parent')
  assert.equal(injectSubmitDecision({ state, draft: draft(), submitting: true }).code, 'submitting')
  const bad = parseInjectParams('{"lr": "fast"}')
  assert.equal(bad.ok, false)
  assert.equal(injectSubmitDecision({ state, draft: draft({ params: bad }) }).code, 'bad_params')
})

test('parameters are an object of finite numbers, and an empty box sends none', () => {
  assert.deepEqual(parseInjectParams('{"lr": 0.001}'), { ok: true, value: { lr: 0.001 } })
  assert.equal(parseInjectParams('[1]').ok, false)
  const sent = buildInjectPayload({ state, draft: draft({ params: parseInjectParams('{"lr": 0.1}') }) })
  assert.deepEqual(sent.idea.params, { lr: 0.1 })
})

test('the panel is a route an owner can open and a review link cannot', () => {
  assert.ok(RUN_ROUTE_PANELS.includes('inject'))
  assert.ok(!REVIEW_SAFE_PANEL_NAMES.includes('inject'))
})
