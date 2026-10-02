import test from 'node:test'
import assert from 'node:assert/strict'
import { mountHarness } from './_mount.js'

let harness, UpstreamPanel
test.before(async () => {
  harness = await mountHarness()
  ;({ default: UpstreamPanel } = await harness.load('/src/UpstreamPanel.jsx'))
})
test.after(async () => { await harness?.close() })

const start = { type: 'upstream_gate_started', seq: 10, action_id: 'check-one',
  proposal_id: 'up_one', request_hash: 'r', input_identity: 'i' }
const finish = { type: 'upstream_gate_finished', seq: 12, action_id: 'check-one',
  proposal_id: 'up_one', request_hash: 'r', result: { passed: true, input_identity: 'i',
    eval_seconds: 2.5, executions: [{ seconds: 2.5 }] } }
const render = history => harness.render(UpstreamPanel, {
  state: { nodes: {}, upstream_enabled: true, upstream_history: history },
})

test('a new unfinished check cannot present the preceding passing check as current', () => {
  const markup = render([start, finish, { ...start, seq: 20, action_id: 'check-two' }])
  assert.match(markup, /Unfinished check/)
  assert.doesNotMatch(markup, /<strong>passed<\/strong>/)
  assert.deepEqual(harness.fetch.calls, [])
})

test('an abandoned claim stays revoked after a late passing completion', () => {
  const markup = render([start, { type: 'upstream_gate_abandoned', seq: 11,
    claim_action_id: start.action_id }, finish])
  assert.match(markup, /Abandoned check/)
  assert.doesNotMatch(markup, /<strong>passed<\/strong>/)
})

test('a bounded history without the claim cannot present a current passing verdict', () => {
  assert.match(render([finish]), /Check evidence unavailable/)
  assert.match(render([start, finish, { ...finish, seq: 20, action_id: 'orphan-completion' }]),
    /Check evidence unavailable/)
})

test('a late older completion cannot displace the newer unfinished claim', () => {
  assert.match(render([start, { ...start, seq: 20, action_id: 'check-two' },
    { ...finish, seq: 22 }]), /Unfinished check/)
})

test('incomplete identities and duplicate claim records provide no passing verdict', () => {
  for (const history of [[start, start, finish], [start, finish, finish], [null, finish],
    [{ ...start, request_hash: undefined }, { ...finish, request_hash: undefined }],
    [start, { ...finish, proposal_id: 'another-proposal' }],
    [start, { ...finish, result: { ...finish.result, input_identity: 'another-context' } }]]) {
    const markup = render(history)
    assert.match(markup, /Check evidence unavailable/)
    assert.doesNotMatch(markup, /<strong>passed<\/strong>/)
  }
})

test('missing or malformed costs remain unavailable and never become zero seconds', () => {
  for (const cost of [null, undefined, '2.5', -1, Infinity]) {
    const markup = render([start, { ...finish, result: { ...finish.result, eval_seconds: cost } }])
    assert.match(markup, /cost unavailable/)
    assert.doesNotMatch(markup, /0\.0 s/)
  }
})

test('a complete check is a recorded result and requires a fresh evidence read before advancement', () => {
  const markup = render([start, finish])
  assert.match(markup, /Recorded completed check/)
  assert.match(markup, /<strong>passed<\/strong>/)
  assert.match(markup, /2\.5 s/)
  assert.match(markup, /Read current upstream evidence before advancement/)
})
