// The trace SUBJECT's truth table — the pure half of the one trace surface.
//
// Everything here is a rule the mounted surface depends on but cannot state: which path a subject
// reads, which payload it may render, and which two things must never be confused (an unpinned
// attempt with attempt 0; a node payload with an operation's).
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
const model = await vite.ssrLoadModule('/src/traceSurfaceModel.js')
test.after(() => vite.close())

const {
  TRACE_VIEW_CONVERSATION, TRACE_VIEW_SPANS, nodeTraceSubject, opTraceSubject, traceRequestPath,
  traceSubjectAttempt, traceSubjectHasLogs, traceSubjectKey, traceSubjectLead, traceSubjectMatches,
  traceSubjectSpans, traceSubjectValid,
} = model

test('each subject names its own routes, and a trace id is encoded into the path', () => {
  const node = nodeTraceSubject(7, 2)
  assert.equal(traceRequestPath(node, TRACE_VIEW_CONVERSATION), '/nodes/7/conversation')
  assert.equal(traceRequestPath(node, TRACE_VIEW_SPANS), '/nodes/7/trace')
  const op = opTraceSubject('ab/cd?e')
  assert.equal(traceRequestPath(op, TRACE_VIEW_SPANS), '/trace/by_trace/ab%2Fcd%3Fe')
  assert.equal(traceRequestPath(op, TRACE_VIEW_CONVERSATION), '/trace/by_trace/ab%2Fcd%3Fe/conversation')
  // An unusable subject must not compose a path at all: `runApiPath(runId, '')` is the RUN, and a
  // surface that read it would render the run payload's failure to fence as "trace unavailable".
  assert.equal(traceRequestPath(null, TRACE_VIEW_SPANS), '')
  assert.equal(traceSubjectValid(nodeTraceSubject(null)), false)
  assert.equal(traceSubjectValid(opTraceSubject('')), false)
  // Node 0 is a real node and its id is FALSY — the defect this codebase has a whole test file about.
  assert.equal(traceSubjectValid(nodeTraceSubject(0, 0)), true)
  assert.equal(traceRequestPath(nodeTraceSubject(0, 0), TRACE_VIEW_SPANS), '/nodes/0/trace')
})

test('an unpinned attempt is not attempt 0', () => {
  // A caller that knows only the node id (the card board's section rows) must not assert 0: the
  // routes 409 an explicitly stale attempt, so that spelling would break every repaired node.
  assert.equal(traceSubjectAttempt(nodeTraceSubject(7)), null)
  assert.equal(traceSubjectAttempt(nodeTraceSubject(7, 0)), 0)
  assert.notEqual(traceSubjectKey(nodeTraceSubject(7)), traceSubjectKey(nodeTraceSubject(7, 0)))
  // …and two generations of one node are two different readings, so they cannot share a poll scope.
  assert.notEqual(traceSubjectKey(nodeTraceSubject(7, 0)), traceSubjectKey(nodeTraceSubject(7, 1)))
  assert.notEqual(traceSubjectKey(nodeTraceSubject(7, 0)), traceSubjectKey(opTraceSubject('7')))
  assert.equal(traceSubjectAttempt(opTraceSubject('t')), null,
    'an operation has no attempt to send')
})

test('the fence admits only this subject’s payload', () => {
  const node = nodeTraceSubject(7, 1)
  assert.equal(traceSubjectMatches(node, { node_id: 7, attempt: 1 }), true,
    'the routes answer with a numeric node_id; the conversation with a string one')
  assert.equal(traceSubjectMatches(node, { node_id: '7', attempt: 1 }), true)
  assert.equal(traceSubjectMatches(node, { node_id: 7, attempt: 0 }), false,
    'a late response for the previous attempt must never render under this one')
  assert.equal(traceSubjectMatches(node, { node_id: 8, attempt: 1 }), false)
  assert.equal(traceSubjectMatches(node, { trace_id: 'x' }), false)
  assert.equal(traceSubjectMatches(node, null), false)

  // Unpinned: whatever generation the server settled to is what was asked for — but it is still
  // fenced on the NODE.
  const unpinned = nodeTraceSubject(7)
  assert.equal(traceSubjectMatches(unpinned, { node_id: 7, attempt: 3 }), true)
  assert.equal(traceSubjectMatches(unpinned, { node_id: 9, attempt: 3 }), false)

  const op = opTraceSubject('t-1')
  assert.equal(traceSubjectMatches(op, { trace_id: 't-1' }), true)
  assert.equal(traceSubjectMatches(op, { trace_id: 't-2' }), false)
  assert.equal(traceSubjectMatches(op, { node_id: 7, attempt: 0 }), false,
    'a node payload is not this operation’s trace, whatever else it contains')
  assert.equal(traceSubjectMatches(op, {}), false,
    'a payload that names no subject cannot be proven to be this one')
})

test('the span forest is read from the field the route actually answers with', () => {
  const forest = [{ span_id: 's1' }]
  assert.deepEqual(traceSubjectSpans(nodeTraceSubject(7, 0), { nodes: forest, spans: [] }), forest,
    'the node routes answer with `nodes` — reading `spans` there renders an empty tree')
  assert.deepEqual(traceSubjectSpans(opTraceSubject('t'), { spans: forest, nodes: [] }), forest)
  assert.deepEqual(traceSubjectSpans(opTraceSubject('t'), null), [])
  assert.deepEqual(traceSubjectSpans(opTraceSubject('t'), { spans: 'nope' }), [])
})

test('the surface says whose work it is showing, and only a node has logs', () => {
  assert.match(traceSubjectLead(nodeTraceSubject(7, 0)), /Experiment #7/)
  assert.doesNotMatch(traceSubjectLead(opTraceSubject('t')), /Experiment|Node/,
    'a proposal is not an experiment; captioning it as one is a lie about whose work it is')
  assert.equal(traceSubjectHasLogs(nodeTraceSubject(7, 0)), true)
  assert.equal(traceSubjectHasLogs(opTraceSubject('t')), false,
    'a proposal ran no sandbox, so there is no stage log to ask for on every poll')
})
