// Fork-to-branch: the RULES of branching from a node the operator is reading in a HISTORICAL
// snapshot, with the node's idea edited. The React half is `RunView.jsx` (the panel + the submit
// choreography); this is the pure one, the same split `traceClearModel.js` / `useTraceClear.js`
// already uses and for the same reason — the decision that must never be got wrong here is not a
// render, it is WHAT GETS SENT and WHAT THE OPERATOR IS TOLD WHEN THE RUN MOVED UNDER THEM.
//
// The gesture is `inject_node`, not `fork`. `fork` (EV_FORK) means "Researcher, propose an
// improvement on this node" and carries no idea at all; the operator's own edited idea is exactly
// what `inject_node` already transports, together with the parent and the parent-generation CAS
// that fences it. So this module builds an inject payload plus one extra key, `forked_from`, whose
// two derived fields (`changed_fields`, `base_idea_digest`) the SERVER stamps — see
// `looplab/serve/control_validation.py::_normalize_fork_receipt`.
//
// WHY A HISTORICAL VIEW MAY DO THIS AT ALL, when it refuses every other node action. Every other
// action names a node and a generation and then lets state the PAYLOAD DOES NOT CARRY decide what
// happens: `RunView.jsx::onNodeAction` reads the generation out of `live2?.nodes?.[id]?.attempt` —
// which is the SNAPSHOT while one is on screen and the live tail otherwise — and the engine then
// applies the action to the node as it is when it services the command. So a reset or an abort
// clicked at seq N either loses its CAS or, worse, lands on a lifecycle the operator was not
// looking at. That is the reason for the blanket refusal and it still stands, unweakened. This one
// gesture is different IN KIND rather than merely safer: the whole intent travels in the payload
// (the operator's idea verbatim, the parent named by id, the generation THEY SAW), so there is
// nothing left for any other state to fill in, and the server refuses the request outright if that
// generation has moved. It is a CONTENT compare-and-swap, which is why it does not need — and must
// not have — a tail-seq CAS: a live run appends several unrelated rows per second, so a seq fence
// would refuse a branch whose meaning nothing had touched.
//
// [2026-08-14 — corrected. This paragraph used to say the click "is executed against whatever the
// tail happens to hold". `live2` is `hist || live`, so in a historical view it is the SNAPSHOT's
// attempt that would be sent, not the tail's. The conclusion is unchanged and the reason is
// stronger: what the payload omits is supplied by a state the operator is not reading, whichever
// one it is.]
//
// THE REACT HALF, wired 2026-08-14 (docs/BACKLOG.md survivor #11 is now closed):
//   * `readOnlyNodeActionRefused` below IS the blanket refusal, restated as a function so its ONE
//     exception has a truth table; `RunView.jsx::onNodeAction` calls it instead of testing
//     `mutationReadOnlyMode` directly, and `mutationReadOnlyMode` itself is untouched.
//   * `forkGestureAccess` is the full statement of that exception: only a HISTORICAL view, and only
//     when nothing else is refusing — a review capability is not steering authority, a stale
//     generation link names a run that is gone, an unresolved start-over is an open destructive
//     operation, and an unloaded run has no generation to fence a command with.
//   * `'fork'` is in `RunView.jsx::HISTORY_SAFE_PANELS`, and `ForkFromSeqPanel.jsx` is the form:
//     `operator`, `rationale` and `params`, seeded by `forkIdeaFromSnapshot` from the SNAPSHOT node
//     (never `live`), submitted through `CONTROL.forkFrom(runId, payload, { expectedGeneration })`.
//   * `Dag.jsx::nodeMenuEntries` offers "Branch from here" and, in a historical snapshot, offers
//     NOTHING ELSE — the other nine items are not shown-and-refused, they are absent.
// Everything below is driven by `ui/test/forkFromSeqModel.test.js`; the panel, the menu table and
// the fact that RunView asks this module rather than re-deriving the rule are driven by
// `ui/test/forkFromSeqPanel.test.js`.

// The action id the two halves of the gesture agree on: `Dag.jsx`'s menu emits it and
// `RunView.jsx::onNodeAction` dispatches on it. A bare string literal spelled twice is how the menu
// item comes to name an action nobody handles — the click would then fall through every branch of
// `onNodeAction` and do nothing at all, with no error anywhere.
export const FORK_FROM_SEQ_ACTION = 'fork-from-seq'

// Why each blocker still refuses the branch, in the operator's words. These are NOT the same list as
// `FORK_BLOCKED_REASONS`: those are about the branch (no node, an unedited idea), these are about
// whether this VIEW may steer the run at all.
export const FORK_ACCESS_REASONS = Object.freeze({
  review: 'A review link is read-only. Branching steers the run, which is not a review capability.',
  start_over: 'Start over must be resolved before changing this run.',
  stale_link: 'This link targets an earlier run generation. Open the current generation to branch.',
  unavailable: 'This run is not loaded, so a branch cannot be fenced to its generation.',
  live: 'Branching this way starts from a snapshot. Open a point in the timeline first.',
})

/**
 * May THIS view branch from a snapshot at all?
 *
 * The whole of the exception carved out of the historical view's blanket refusal, in one place so
 * the panel, the menu and the guard cannot come to disagree about it. `history` is the only
 * read-only reason that admits the gesture; every other one still refuses it, and `live` refuses it
 * from the other side — with no snapshot there is no vantage point to record, and `observed_seq`
 * would have to be invented.
 */
export function forkGestureAccess({
  historyActive = false, reviewMode = false, routeFenceBlocked = false,
  runAuthorityBlocked = false, startOverMutationBlocked = false,
} = {}) {
  if (reviewMode) return { ok: false, reason: 'review' }
  if (startOverMutationBlocked) return { ok: false, reason: 'start_over' }
  if (routeFenceBlocked) return { ok: false, reason: 'stale_link' }
  if (runAuthorityBlocked) return { ok: false, reason: 'unavailable' }
  if (!historyActive) return { ok: false, reason: 'live' }
  return { ok: true, reason: null }
}

/**
 * The historical view's blanket refusal of node actions, and its ONE exception.
 *
 * Stated here rather than inline in `RunView.jsx::onNodeAction` because the next reader of that
 * guard will assume the refusal was loosened, and a rule with a truth table is checkable where an
 * `&&` in a 3,000-line component is not. `mutationReadOnlyMode` is computed exactly as it always
 * was and is passed in; nothing in this module may widen it.
 */
export function readOnlyNodeActionRefused(action, {
  mutationReadOnlyMode = false, forkAccess = null,
} = {}) {
  if (!mutationReadOnlyMode) return false
  if (action !== FORK_FROM_SEQ_ACTION) return true
  return !forkAccess?.ok
}

// The idea fields carried over from the snapshot into the branch. Deliberately a CLOSED list of the
// operator-authored substance, and the two exclusions are the load-bearing part:
//
//  - the concept ENVELOPE (`concepts`/`concepts_added`/`concepts_removed`/`concept_mode`). A node
//    whose durable idea authored NO envelope still serializes as `concepts: []` on the wire, and
//    echoing that back makes the server's intake upgrade it to an explicit `concept_mode` — which
//    the fold reads as an authoritative-empty membership rather than an absent one. The branch would
//    then differ from its parent in a field the operator never touched, and `changed_fields` would
//    truthfully say so. Not carrying it is the fix; inventing a taxonomy on the operator's behalf is
//    not this surface's job.
//  - `card_id`, `hypothesis`, `footprint`, `theme`: engine bookkeeping, not idea substance. An
//    inject has never inherited its parent's Card, and quietly attaching an operator's branch to the
//    Researcher's Card would put it inside that Card's budget and ledger.
export const FORK_IDEA_FIELDS = Object.freeze([
  'operator', 'params', 'rationale', 'eval_profile', 'eval_timeout', 'space',
])

// The fields the operator may edit in the branch form. `operator` is the search operator name and is
// editable because a branch often IS a different operator applied to the same parent.
// OPEN[fork-editable-fields-dead-export] a second, unread statement of the form's field rule.
// proof:present:FORK_EDITABLE_FIELDS@ui/src/forkFromSeqModel.js+absent:FORK_EDITABLE_FIELDS@ui/src/ForkFromSeqPanel.jsx
// REVIEW 2026-08-18 (simplification/dead-export): referenced nowhere — not by `ForkFromSeqPanel.jsx`
// (which hardcodes the same three fields as separate useState hooks) and not by any test — so this
// is a second, unread statement of the rule that can silently drift from the form. Fix direction:
// either drive the panel's fields from it (and pin it in the panel test) or delete the export.
export const FORK_EDITABLE_FIELDS = Object.freeze(['operator', 'rationale', 'params'])

/** The idea a branch starts from: the snapshot node's own idea, narrowed to `FORK_IDEA_FIELDS`. */
export function forkIdeaFromSnapshot(node) {
  const idea = (node && typeof node === 'object' && node.idea) || {}
  const out = {}
  for (const field of FORK_IDEA_FIELDS) {
    const value = idea[field]
    if (value === undefined || value === null) continue
    out[field] = value
  }
  if (typeof out.operator !== 'string' || !out.operator.trim()) out.operator = 'manual'
  return out
}

/** Are two ideas the same, field by field, over the fields this surface carries? */
export function forkIdeaEdited(base, draft) {
  if (!base || !draft) return false
  return FORK_IDEA_FIELDS.some(field => JSON.stringify(base[field] ?? null)
    !== JSON.stringify(draft[field] ?? null))
}

// What the operator must have in hand before a branch can be formed. Each is a REFUSAL the panel
// prints rather than a disabled control with no explanation: an operator looking at a snapshot in
// which their node is gone needs to be told that, not shown a greyed-out button.
export const FORK_BLOCKED_REASONS = Object.freeze({
  no_node: 'Select an experiment to branch from.',
  no_snapshot: 'This branch needs the snapshot it was read from; reload the historical view.',
  tombstoned: 'This experiment was removed from the run and can no longer be branched from.',
  aborted: 'This experiment was aborted and can no longer be branched from.',
  unedited: 'Edit the idea before branching — an unchanged copy is not a new experiment.',
  submitting: 'This branch is already being submitted.',
})

/**
 * May this branch be submitted, and if not, why?
 *
 * Everything here is decided from the SNAPSHOT the operator is reading. It deliberately does not
 * consult the live state: the live state is the server's job to check, and a client that
 * pre-flighted against the tail would either refuse branches the server would accept or — worse —
 * accept ones formed against a node that is not the one displayed.
 */
export function forkSubmitDecision({ node, viewSeq, base, draft, submitting = false } = {}) {
  if (submitting) return { ok: false, reason: 'submitting' }
  if (!node || node.id == null) return { ok: false, reason: 'no_node' }
  if (!Number.isInteger(viewSeq) || viewSeq < 0) return { ok: false, reason: 'no_snapshot' }
  if (node.tombstoned) return { ok: false, reason: 'tombstoned' }
  if (node.aborted) return { ok: false, reason: 'aborted' }
  if (!forkIdeaEdited(base, draft)) return { ok: false, reason: 'unedited' }
  return { ok: true, reason: null }
}

/**
 * The exact `inject_node` payload for a branch, or null when `forkSubmitDecision` refuses.
 *
 * `generation` is the attempt THE OPERATOR SAW, never the live one — that is the whole compare-and-
 * swap. It is sent twice on purpose: `parent_generations` is the fence `_normalize_inject_node`
 * already enforced for every inject, and `forked_from.generation` is the receipt's own copy, so a
 * hand-built payload cannot record a lineage its own CAS did not check.
 */
export function buildForkPayload({ node, viewSeq, base, draft, submitting = false } = {}) {
  const decision = forkSubmitDecision({ node, viewSeq, base, draft, submitting })
  if (!decision.ok) return null
  const generation = Number.isInteger(node.attempt) ? node.attempt : 0
  return {
    // A DEEP copy, not a spread: `params`/`space` are nested objects the form goes on editing, and a
    // shared reference would let a keystroke after submit change the body of an in-flight request —
    // so the durable event would not be the one the operator confirmed. The idea is JSON by
    // definition (it travels as the event's payload), so a JSON round-trip is the exact clone.
    idea: JSON.parse(JSON.stringify(draft)),
    parent_id: node.id,
    parent_generations: { [node.id]: generation },
    forked_from: { node_id: node.id, generation, observed_seq: viewSeq },
  }
}

// The server's refusal codes for this gesture, and what each one MEANS to an operator who is looking
// at a snapshot. `stale parent` is the one that matters: it is not an error, it is the run having
// moved on, and the honest instruction is to re-read the node rather than to retry the same bytes.
const FORK_REFUSALS = Object.freeze({
  fork_parent_mismatch: 'This branch names an experiment it is not descended from. Reopen the '
    + 'experiment and branch again.',
  fork_observed_seq_out_of_range: 'This branch names a point in the run that does not exist. '
    + 'Reopen the snapshot and branch again.',
  fork_receipt_forged: 'This branch tried to supply provenance the server derives. Reload the page.',
})

const STALE_PARENT_RE = /stale parent #(\d+): current generation is (\d+)/

// The three 4xx statuses that do NOT prove the intake refused. Everything else in the 4xx range is a
// validator saying no before anything was appended; these three a PROXY can synthesize after it has
// already forwarded the request, so the branch may be sitting in the queue.
//
// The same three numbers as `commandModel.js::TRANSIENT_HTTP` and
// `traceClearModel.js::TRACE_CLEAR_RETRYABLE_STATUSES`, and — as there — deliberately NOT the same
// constant. `TRANSIENT_HTTP` answers "may this command read be retried"; this one answers "can the
// server prove nothing was appended", which is what `classifyForkFailure` turns into `applied` and
// `forkRetryable` turns into whether the submit button re-arms. Sharing the list would let a change
// made for the command lifecycle move a PAID submission between "nothing was queued, edit and
// resubmit" and "outcome unknown, do not press again" — one idea becoming two GPU experiments.
// NAMED rather than spelled inline mid-expression because it is the fourth site of these three
// numbers and the one that spends money: a status quietly dropping out of an unlabelled
// `status !== 408 && …` chain is not a thing grep can find.
export const FORK_UNPROVEN_4XX_STATUSES = Object.freeze([408, 425, 429])

/**
 * Classify a failed branch submission.
 *
 * `applied: false` is a CLAIM and only a proven pre-append refusal may make it — the same rule
 * `traceClearModel.js` states for a destructive DELETE, for the same reason: a 409/400 from the
 * intake proves the validator refused before anything reached the log, while a timeout or a 5xx
 * leaves an intent that may already be queued, and telling the operator "nothing happened" there
 * would invite a second branch for the same idea.
 */
export function classifyForkFailure(error) {
  // Two shapes reach here and it is worth knowing why, because the durable path's refusal does NOT
  // look like an HTTP error. `/commands` answers 200 with a REJECTED record whose `error` carries
  // the code, message and remediation — measured: a stale-parent branch comes back as
  // `{status: 'rejected', error: {code: 'command_target_not_found', message: 'stale parent #0: …'}}`.
  // Reading only `error.message` off that object would find the wrapper's text, not the refusal's,
  // so a genuine "the run moved" would degrade into the unknown-outcome branch and tell the operator
  // their branch might already exist.
  const isRecord = Boolean(error && typeof error === 'object' && error.error
    && typeof error.status === 'string')
  // A `rejected` record is the durable lifecycle's own proof that the INTAKE refused, so nothing was
  // appended. `failed` and `timed_out` are NOT that: those records were accepted first and only then
  // failed to reach their postcondition, so the intent may already be sitting in the queue.
  const recordProvesNoAppend = isRecord && error.status === 'rejected'
  const record = isRecord ? error.error : error
  const status = record && (record.status ?? record.statusCode)
  const detail = (record && (record.code || record.detail?.code)) || ''
  const text = String((record && (record.message || record.detail?.message)) || '')
  const stale = STALE_PARENT_RE.exec(text)
  if (stale) {
    return {
      applied: false,
      moved: true,
      code: 'stale_parent',
      nodeId: Number(stale[1]),
      currentGeneration: Number(stale[2]),
      message: `Experiment #${stale[1]} has been re-run since this snapshot (it is now attempt `
        + `${stale[2]}). Reopen it and branch from what is there now.`,
    }
  }
  const known = FORK_REFUSALS[detail] || Object.keys(FORK_REFUSALS)
    .filter(code => text.includes(code)).map(code => FORK_REFUSALS[code])[0]
  if (known) return { applied: false, moved: false, code: detail || 'refused', message: known }
  if (typeof status === 'number' && status >= 400 && status < 500
      && !FORK_UNPROVEN_4XX_STATUSES.includes(status)) {
    return {
      applied: false,
      moved: false,
      code: 'refused',
      message: text || 'The branch was refused and nothing was queued.',
    }
  }
  if (recordProvesNoAppend) {
    return {
      applied: false,
      moved: false,
      code: detail || 'refused',
      message: text || 'The branch was refused and nothing was queued.',
    }
  }
  return {
    applied: null,
    moved: false,
    code: 'unknown',
    message: 'The branch may or may not have been queued. Reload the run before submitting it '
      + 'again, or it may be created twice.',
  }
}

/**
 * After a failed submission, may the operator press the same button again?
 *
 * This is the rendered consequence of the distinction above and the reason the panel has to make it
 * at all. Three outcomes, three affordances:
 *
 *  - a proven pre-append refusal the operator can still fix from this snapshot (a payload the
 *    validator rejected) leaves the form live: nothing was queued, so editing and resubmitting is
 *    exactly right;
 *  - `moved` is equally proven and equally un-queued, but it is NOT fixable here — every resubmit
 *    from this snapshot names the same superseded generation and earns the same 409. The form
 *    fences and the panel offers the only thing that helps, which is to go and read the node as it
 *    is now;
 *  - `applied: null` must never re-arm. The branch may already be queued, and this queues a PAID
 *    unit of work: a second press is how one idea becomes two experiments.
 */
export function forkRetryable(failure) {
  if (!failure) return true
  return failure.applied === false && !failure.moved
}

/**
 * What the operator should be told when the branch LANDED but the run has moved since the snapshot.
 *
 * Not a refusal — the branch is exactly what they asked for. It is the honest note that the node
 * they were reading has a different lifecycle now, which the snapshot could not show them.
 */
export function forkLandedNotice({ node, live, viewSeq } = {}) {
  if (!node || !live) return null
  const now = live[node.id] ?? live[String(node.id)]
  if (!now) {
    return `Branched from experiment #${node.id} as you saw it at seq ${viewSeq}; that experiment is `
      + 'no longer in the live run.'
  }
  if (now.status && node.status && now.status !== node.status) {
    return `Branched from experiment #${node.id} as you saw it at seq ${viewSeq}; it is now `
      + `${now.status}.`
  }
  return null
}
