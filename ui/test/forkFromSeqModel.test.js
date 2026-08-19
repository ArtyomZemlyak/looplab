// THE BRANCH-FROM-A-SNAPSHOT truth table. Every rule here is one the panel depends on and cannot
// state: what a branch actually SENDS (the generation the operator saw, never the live one), what it
// refuses to carry over, and — the one that must never be got wrong — whether a failed submission
// left an intent queued. "Nothing was queued" and "this may already exist" are different claims
// about a PAID unit of work, exactly the distinction `traceClearModel.js` exists to keep honest for
// a destructive one.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer, parseAst } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
const model = await vite.ssrLoadModule('/src/forkFromSeqModel.js')
test.after(() => vite.close())

const {
  FORK_BLOCKED_REASONS, FORK_EDITABLE_FIELDS, FORK_IDEA_FIELDS, FORK_UNPROVEN_4XX_STATUSES,
  buildForkPayload, classifyForkFailure, forkDraftIdea, forkIdeaEdited, forkIdeaFromSnapshot,
  forkLandedNotice, forkRetryable, forkSubmitDecision,
} = model

// A node exactly as the historical `/state?seq=` projection serves it: the concept envelope is
// present-and-empty even though the durable idea authored none.
const snapshotNode = (over = {}) => ({
  id: 3,
  attempt: 0,
  status: 'evaluated',
  idea: {
    operator: 'improve',
    params: { x: 1.5 },
    rationale: 'widen the search',
    eval_profile: null,
    eval_timeout: null,
    theme: null,
    concepts: [],
    concepts_added: [],
    concepts_removed: [],
    space: {},
    hypothesis: null,
    card_id: 'card-0',
    footprint: null,
  },
  ...over,
})

test('the branch carries idea SUBSTANCE and never a taxonomy the operator did not author', () => {
  const base = forkIdeaFromSnapshot(snapshotNode())
  assert.deepEqual(base, {
    operator: 'improve', params: { x: 1.5 }, rationale: 'widen the search', space: {},
  })
  // The exclusions are the point: echoing `concepts: []` back would make the server upgrade the
  // idea to an explicit concept_mode, and `card_id` would attach the branch to the Researcher's Card.
  for (const excluded of ['concepts', 'concepts_added', 'concepts_removed', 'concept_mode',
    'card_id', 'hypothesis', 'footprint', 'theme']) {
    assert.equal(excluded in base, false, `${excluded} must not ride along`)
    assert.equal(FORK_IDEA_FIELDS.includes(excluded), false)
  }
  // An absent/blank operator still yields a legal Idea rather than a payload the server 400s.
  assert.equal(forkIdeaFromSnapshot({ idea: { rationale: 'x' } }).operator, 'manual')
  assert.equal(forkIdeaFromSnapshot(null).operator, 'manual')
})

test('an unchanged copy is not a branch', () => {
  const base = forkIdeaFromSnapshot(snapshotNode())
  assert.equal(forkIdeaEdited(base, { ...base }), false)
  assert.equal(forkIdeaEdited(base, { ...base, rationale: 'narrow it instead' }), true)
  // A nested edit counts — a params tweak is the most common branch there is.
  assert.equal(forkIdeaEdited(base, { ...base, params: { x: 1.6 } }), true)
  // ...and a field going absent is a change, not a no-op.
  assert.equal(forkIdeaEdited(base, { operator: 'improve', params: { x: 1.5 } }), true)
})

test('the submit decision names its refusal instead of silently disabling a control', () => {
  const node = snapshotNode()
  const base = forkIdeaFromSnapshot(node)
  const draft = { ...base, rationale: 'try a coarser grid' }
  assert.deepEqual(forkSubmitDecision({ node, viewSeq: 12, base, draft }), { ok: true, reason: null })

  const cases = [
    [{ node: null, viewSeq: 12, base, draft }, 'no_node'],
    [{ node, viewSeq: null, base, draft }, 'no_snapshot'],
    [{ node, viewSeq: -1, base, draft }, 'no_snapshot'],
    [{ node: snapshotNode({ tombstoned: true }), viewSeq: 12, base, draft }, 'tombstoned'],
    [{ node: snapshotNode({ aborted: true }), viewSeq: 12, base, draft }, 'aborted'],
    [{ node, viewSeq: 12, base, draft: { ...base } }, 'unedited'],
    [{ node, viewSeq: 12, base, draft, submitting: true }, 'submitting'],
  ]
  for (const [input, reason] of cases) {
    assert.deepEqual(forkSubmitDecision(input), { ok: false, reason })
    assert.equal(typeof FORK_BLOCKED_REASONS[reason], 'string')
    assert.equal(buildForkPayload(input), null, `${reason} must build no payload`)
  }
})

test('the payload fences on the generation the operator SAW, not the live one', () => {
  const node = snapshotNode({ attempt: 2 })
  const base = forkIdeaFromSnapshot(node)
  const draft = { ...base, params: { x: 0.5 } }
  const payload = buildForkPayload({ node, viewSeq: 41, base, draft })
  assert.deepEqual(payload, {
    idea: { operator: 'improve', params: { x: 0.5 }, rationale: 'widen the search', space: {} },
    parent_id: 3,
    parent_generations: { 3: 2 },
    forked_from: { node_id: 3, generation: 2, observed_seq: 41 },
  })
  // The receipt's generation and the CAS's generation are one number by construction — a payload
  // that recorded a lineage its own compare-and-swap did not check would be unfalsifiable.
  assert.equal(payload.forked_from.generation, payload.parent_generations[payload.parent_id])
  // The two SERVER-derived fields are never sent.
  assert.equal('changed_fields' in payload.forked_from, false)
  assert.equal('base_idea_digest' in payload.forked_from, false)
  // The draft is COPIED: a later keystroke in the form cannot mutate an in-flight request body.
  draft.params.x = 99
  assert.equal(payload.idea.params.x, 0.5)
})

test('"the run moved under you" is a distinct outcome from "refused"', () => {
  const moved = classifyForkFailure({
    status: 409, message: 'stale parent #3: current generation is 2',
  })
  assert.equal(moved.applied, false)
  assert.equal(moved.moved, true)
  assert.equal(moved.code, 'stale_parent')
  assert.equal(moved.nodeId, 3)
  assert.equal(moved.currentGeneration, 2)
  assert.match(moved.message, /#3 has been re-run/)
  assert.match(moved.message, /attempt 2/)

  for (const code of ['fork_parent_mismatch', 'fork_observed_seq_out_of_range',
    'fork_receipt_forged']) {
    const refused = classifyForkFailure({ status: 400, code })
    assert.equal(refused.applied, false)
    assert.equal(refused.moved, false)
    assert.equal(refused.code, code)
    assert.ok(refused.message.length > 0)
  }
  // The code may only be legible in the body text; that must classify the same way.
  assert.equal(classifyForkFailure({
    status: 400, message: '{"code":"fork_parent_mismatch"}',
  }).applied, false)

  // The DURABLE path refuses with 200 + a rejected RECORD, not an HTTP error. This is the exact
  // shape `/api/runs/{id}/commands` returned when driven against a re-run parent.
  const rejected = classifyForkFailure({
    status: 'rejected',
    event_type: 'inject_node',
    error: {
      code: 'command_target_not_found',
      message: 'stale parent #0: current generation is 1',
      remediation: 'correct the command payload and submit it with a new idempotency key',
      retryable: false,
    },
  })
  assert.equal(rejected.code, 'stale_parent')
  assert.equal(rejected.moved, true)
  assert.equal(rejected.applied, false)
  assert.equal(rejected.nodeId, 0)
  assert.equal(rejected.currentGeneration, 1)

  // A rejected record with a code this surface does not know is STILL a proven pre-append refusal —
  // the durable lifecycle only mints `rejected` when the intake refused.
  const unknownRejection = classifyForkFailure({
    status: 'rejected',
    error: { code: 'command_target_not_found', message: 'parent #4 is tombstoned' },
  })
  assert.equal(unknownRejection.applied, false)
  assert.equal(unknownRejection.message, 'parent #4 is tombstoned')

  // ...but `failed` and `timed_out` are not. Those were ACCEPTED first, so the append may have
  // landed and only the postcondition was never observed.
  for (const status of ['failed', 'timed_out']) {
    const late = classifyForkFailure({ status, error: { code: 'engine_ack_timeout', message: 'x' } })
    assert.equal(late.applied, null, `${status} must not claim a clean refusal`)
  }
})

test('only a PROVEN pre-append refusal may claim nothing was queued', () => {
  // A plain 4xx from the intake is proof: the validator refused before any append.
  assert.equal(classifyForkFailure({ status: 404, message: 'no node #9 in this run' }).applied, false)
  // Everything else leaves an intent that may already be sitting in the queue, and this one queues
  // a PAID unit of work — telling the operator "nothing happened" would buy the experiment twice.
  for (const error of [
    { status: 500, message: 'boom' },
    { status: 502 },
    { status: 408, message: 'timeout' },
    { status: 425 },
    { status: 429, message: 'slow down' },
    { message: 'network error' },
    {},
    null,
  ]) {
    const verdict = classifyForkFailure(error)
    assert.equal(verdict.applied, null, `${JSON.stringify(error)} must not claim a clean refusal`)
    assert.equal(verdict.code, 'unknown')
    assert.match(verdict.message, /may or may not/)
  }
})

test('a landed branch still says what the snapshot could not show', () => {
  const node = snapshotNode({ status: 'pending' })
  assert.equal(forkLandedNotice({ node, live: { 3: { status: 'pending' } }, viewSeq: 12 }), null)
  assert.match(
    forkLandedNotice({ node, live: { 3: { status: 'failed' } }, viewSeq: 12 }),
    /you saw it at seq 12; it is now failed/)
  assert.match(
    forkLandedNotice({ node, live: {}, viewSeq: 12 }),
    /no longer in the live run/)
  // String-keyed node maps (the /state wire shape) resolve the same as numeric ones.
  assert.equal(forkLandedNotice({ node, live: { 3: { status: 'pending' } }, viewSeq: 12 }), null)
  assert.equal(forkLandedNotice({ node: null, live: {}, viewSeq: 1 }), null)
})

test('the 4xx statuses that prove nothing are a NAMED list, and the classifier reads it', () => {
  // These three were spelled inline, unnamed, mid-expression (`status !== 408 && status !== 425 &&
  // status !== 429`) in the one decision that gates a PAID re-submit: `classifyForkFailure` turns it
  // into `applied`, `forkRetryable` turns `applied` into whether the submit button re-arms, and a
  // press re-arms into a second GPU experiment for one idea. A status silently leaving that chain is
  // not something grep can find, so the list is a constant with the reason written beside it — the
  // third such fork in this codebase, after `commandModel.js::TRANSIENT_HTTP` and
  // `traceClearModel.js::TRACE_CLEAR_RETRYABLE_STATUSES`.
  assert.deepEqual([...FORK_UNPROVEN_4XX_STATUSES], [408, 425, 429])
  // Membership above is the pin; below is the CONSEQUENCE, so a member that stops being read by the
  // classifier is red even while the constant still lists it.
  for (const status of FORK_UNPROVEN_4XX_STATUSES) {
    const verdict = classifyForkFailure({ status, message: 'proxy said so' })
    assert.equal(verdict.applied, null, `${status}: a proxy can synthesize this AFTER forwarding`)
    assert.equal(verdict.code, 'unknown')
    assert.equal(forkRetryable(verdict), false, `${status} must never re-arm a paid submit`)
  }
  // And every OTHER 4xx is the validator refusing before any append: the form stays live, because
  // nothing was queued and the operator can still fix the payload from this snapshot.
  const proven = [400, 401, 403, 404, 409, 410, 413, 422, 428, 499]
  for (const status of proven) {
    assert.ok(!FORK_UNPROVEN_4XX_STATUSES.includes(status), `${status} is not a "try again" status`)
    const verdict = classifyForkFailure({ status, message: 'refused' })
    assert.equal(verdict.applied, false, `${status} is a proven pre-append refusal`)
    assert.equal(forkRetryable(verdict), true, `${status} must leave the form usable`)
  }
})

test('the forked status list is the fork lifecycle’s own, not the command lifecycle’s', async () => {
  // Same three numbers as `commandModel.js::TRANSIENT_HTTP` and deliberately NOT the same constant:
  // that one answers "may this command READ be retried", this one answers "can the server prove
  // nothing was appended". This test does NOT assert the two stay equal — that would re-couple what
  // was forked on purpose. It asserts the fork: two module-owned lists, so a change made for the
  // command lifecycle cannot move a paid submission between "nothing was queued" and "unknown".
  const commandModel = await vite.ssrLoadModule('/src/commandModel.js')
  assert.notEqual(FORK_UNPROVEN_4XX_STATUSES, commandModel.TRANSIENT_HTTP)
  // Read as IMPORTS, not as text: this module's own comment names `commandModel.js::TRANSIENT_HTTP`
  // to say why it does not use it, so a substring pin here would fail on the explanation.
  const source = await readFile(new URL('../src/forkFromSeqModel.js', import.meta.url), 'utf8')
  const imported = []
  for (const node of parseAst(source).body) {
    if (node.type === 'ImportDeclaration') imported.push(node.source.value)
  }
  assert.deepEqual(imported, [], 'this module owns its list; it imports nothing at all')
})

// ---------------------------------------------------------------- the draft the form offers

test('an untouched form is an unchanged copy, and the receipt must not blame the operator for it', () => {
  // A parent whose durable idea authored NO rationale — `forkIdeaFromSnapshot` omits the field
  // rather than inventing '' for it. The form seeds its textarea with '' all the same, because a
  // textarea has no other empty value.
  const base = forkIdeaFromSnapshot(snapshotNode({ idea: { operator: 'improve', params: { x: 1 } } }))
  assert.equal('rationale' in base, false)
  const untouched = forkDraftIdea(base, { operator: 'improve', rationale: '', params: { x: 1 } })
  assert.equal('rationale' in untouched, false,
    'a field the operator put nothing in must not travel as one they wrote')
  assert.equal(forkIdeaEdited(base, untouched), false)
  // ...so the refusal that exists for exactly this actually fires. Before `forkDraftIdea` owned the
  // shape, the panel spread an unconditional `rationale: ''` onto the draft, `forkIdeaEdited`
  // compared '""' against 'null', submit was live on a byte-for-byte copy, and the server stamped
  // `rationale` as a field the operator changed — which is now what a later reader is SHOWN.
  const decision = forkSubmitDecision({ node: snapshotNode(), viewSeq: 7, base, draft: untouched })
  assert.deepEqual(decision, { ok: false, reason: 'unedited' })
  assert.equal(buildForkPayload({ node: snapshotNode(), viewSeq: 7, base, draft: untouched }), null)
})

test('clearing a rationale the PARENT had is a real edit and survives to the payload', () => {
  // The other half of the rule, and the reason it is not simply "drop empty fields": erasing the
  // parent's justification is a deliberate act, and a draft that silently restored it would send an
  // idea the operator did not author.
  const base = forkIdeaFromSnapshot(snapshotNode({
    idea: { operator: 'improve', params: { x: 1 }, rationale: "the parent's reason" } }))
  const cleared = forkDraftIdea(base, { operator: 'improve', rationale: '', params: { x: 1 } })
  assert.equal(cleared.rationale, '')
  assert.equal(forkIdeaEdited(base, cleared), true)
})

test('the draft rule is stated once, and the form is its only reader', async () => {
  // `FORK_EDITABLE_FIELDS` used to be exported and read by nobody while `ForkFromSeqPanel.jsx`
  // hardcoded the same three fields, i.e. two statements of one rule free to drift apart.
  assert.deepEqual([...FORK_EDITABLE_FIELDS], ['operator', 'rationale', 'params'])
  // An edit to a field NOT on the list is not an edit this form can make, so it is ignored.
  const base = forkIdeaFromSnapshot(snapshotNode())
  const draft = forkDraftIdea(base, { hypothesis: 'smuggled', operator: 'improve' })
  assert.equal('hypothesis' in draft, false)
  // A blank operator falls back rather than sending '' — `inject_node` refuses an empty operator.
  assert.equal(forkDraftIdea(base, { operator: '   ' }).operator, 'manual')
  const panel = await readFile(new URL('../src/ForkFromSeqPanel.jsx', import.meta.url), 'utf8')
  assert.match(panel, /forkDraftIdea\(base, \{/,
    'the panel must ASK the model for its draft rather than restate it')
})
