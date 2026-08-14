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
// action names a node and lets the LIVE state supply the rest — `RunView.jsx::onNodeAction` reads
// `live2?.nodes?.[id]?.attempt`, so a click made while reading seq N is executed against whatever
// the tail happens to hold. That is the reason for the blanket refusal and it still stands. This one
// gesture is different in kind: the whole intent travels in the payload (the operator's idea
// verbatim, the parent named by id, the generation THEY SAW), so there is nothing for the live tail
// to fill in, and the server refuses the request outright if that generation has moved. It is a
// CONTENT compare-and-swap, which is why it does not need — and must not have — a tail-seq CAS: a
// live run appends several unrelated rows per second, so a seq fence would refuse a branch whose
// meaning nothing had touched.
//
// NOT YET WIRED, and this is the whole of what is left (docs/BACKLOG.md survivor #11):
//   1. add `'fork'` to `RunView.jsx::HISTORY_SAFE_PANELS` so the panel may open at `historyActive`;
//   2. give `onNodeAction`'s `mutationReadOnlyMode` guard (RunView.jsx ~:1697) a narrow exception for
//      THIS action only — `readOnlyMode` must keep refusing every other one, and `reviewMode`,
//      `routeFenceBlocked`, `runAuthorityBlocked` and `startOverMutationBlocked` must keep refusing
//      this one (a review capability is not steering authority, and an unresolved start-over is an
//      open destructive operation);
//   3. the form itself: `operator`, `rationale` and `params`, seeded by `forkIdeaFromSnapshot` from
//      the SNAPSHOT node (never `live2`) and submitted through `CONTROL.forkFrom(runId, payload)`;
//   4. a "Branch from here" item on the Dag node menu, enabled exactly when `forkSubmitDecision`
//      says `ok` and otherwise showing `FORK_BLOCKED_REASONS[reason]`.
// It was left out of the change that landed the rest because a `vite build` empties
// `ui/dist/assets` (which breaks `test_server`'s static mount), nothing in the suite MOUNTS
// `RunView`, and a GPU run was executing — so a ~3,000-line JSX edit had no way to prove the tree
// still compiles. Everything below is driven by `ui/test/forkFromSeqModel.test.js` today.

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
      && status !== 408 && status !== 425 && status !== 429) {
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
