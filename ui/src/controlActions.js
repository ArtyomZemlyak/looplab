// CONTROL: the operator ACTION VOCABULARY — every control a person can apply to a run, each one a
// named `{event type, payload}` over the single authoritative command lifecycle in
// commandProtocol.js. A MEMBER of the api.js barrel (doc 25 UI-02); it never imports api.js back and
// every name below is re-exported from there, so no consumer changed.
//
// UI-02's resolution declined to extract this one, on the grounds that it is "the action vocabulary
// a reader opens an API module to find, and the only remaining resident that reaches `runCommand`,
// `jobAwait` and the endpoint plumbing at once". Both halves are why it is here now rather than in
// api.js: a vocabulary is exactly the thing that should be readable in one screen without the 1,000
// lines of endpoint functions around it, and reaching three of the split members at once makes it a
// CONSUMER of them — which is a module that sits above them, not a resident of the barrel.
//
// The map is deliberately flat and deliberately thin: every entry states the event and the payload
// and nothing else, because engine policy — whether a command may run now, what it wakes, what it
// refuses — belongs to the server (`serve/control_validation.py::CONTROL_EVENTS` is the other side
// of this vocabulary and the authority). Two entries are not one-liners and each says why in its own
// comment: `forkFrom`, the only run command that widens `allowRunMutationModes`, and `refreshReport`,
// which is a paid background job rather than an event append.
import { _authHeaders, runApiPath, assertNotReviewMutation } from './apiClient.js'
import {
  COMMAND_REQUEST_TIMEOUT_MS, runGenerationError, safeIdentityText, validRunGeneration,
} from './commandModel.js'
import { commandJson, jobAwait, runCommand } from './commandProtocol.js'
import { assertRunMutationAllowed } from './runMode.js'

export const CONTROL = {
  // Three operator controls (see docs/guide/concepts.md → "Stopping a run"):
  //   stop     — freeze the run, NO finalization (event: pause). Resumable; finalize later if wanted.
  //   finalize — stop AND wrap up (report / cross-run lessons+case / cost roll-up). event: run_abort.
  //   resume   — continue from ANY stopped state (pause / finalize / natural finish). event: resume.
  stop: (rid) => runCommand(rid, 'pause', {}),
  finalize: (rid) => runCommand(rid, 'run_abort', { reason: 'finalized' }),
  resume: (rid) => runCommand(rid, 'resume', {}),
  // One durable server-owned pause -> replacement-owner handoff. The browser does not orchestrate
  // two commands, so navigation or reload cannot strand a run between them.
  restart: (rid) => runCommand(rid, 'restart', {}),
  // back-compat aliases (older callers / NL control): pause≡stop, abort≡finalize, reopen≡resume.
  pause: (rid) => runCommand(rid, 'pause', {}),
  abort: (rid) => runCommand(rid, 'run_abort', { reason: 'finalized' }),
  nodeAbort: (rid, id, generation) => runCommand(
    rid, 'node_abort', { node_id: id, generation, reason: 'ui' }),
  // Re-run an existing node IN PLACE from a stage (no new node): eval=re-score (keep code),
  // implement=re-run the Developer (keep the idea), propose=full redo. The command service drives it.
  resetNode: (rid, id, stage, generation) => runCommand(
    rid, 'node_reset', { node_id: id, generation, from_stage: stage }),
  approve: (rid, id, generation) => runCommand(
    rid, 'approval_granted', { node_id: id, generation }),
  ratify: (rid) => runCommand(rid, 'spec_approved', {}),
  hint: (rid, text) => runCommand(rid, 'hint', { text }),
  // `max_eval_seconds` is the absolute cumulative ceiling (durable LWW), not an additive delta.
  setEvalCeiling: (rid, seconds) =>
    runCommand(rid, 'budget_extend', { max_eval_seconds: seconds }),
  forceConfirm: (rid, id, generation) => runCommand(
    rid, 'force_confirm', { node_id: id, generation }),
  forceAblate: (rid, id, generation) => runCommand(
    rid, 'force_ablate', { node_id: id, generation }),
  fork: (rid, id, generation) => runCommand(
    rid, 'fork', { from_node_id: id, generation }),
  annotate: (rid, id, text) => runCommand(rid, 'annotation', { node_id: id, text }),
  // Structured comments are append-only run commands. The caller supplies the exact displayed run
  // generation separately from the node's attempt generation; a late click can therefore update
  // neither a replacement run nor a reset incarnation of the same numeric node id.
  createComment: (rid, { nodeId, nodeGeneration, text }, options = {}) => runCommand(
    rid, 'comment_created', { node_id: nodeId, node_generation: nodeGeneration, text }, options),
  editComment: (rid, { commentId, nodeId, nodeGeneration, expectedVersion, text }, options = {}) => runCommand(
    rid, 'comment_edited', {
      comment_id: commentId, node_id: nodeId, node_generation: nodeGeneration,
      expected_version: expectedVersion, text,
    }, options),
  setCommentResolved: (rid, {
    commentId, nodeId, nodeGeneration, expectedVersion, resolved,
  }, options = {}) => runCommand(rid, 'comment_resolution_changed', {
    comment_id: commentId, node_id: nodeId, node_generation: nodeGeneration,
    expected_version: expectedVersion, resolved,
  }, options),
  // PART V Phase 2c: an operator replaces ONE node's concept tags (full set). Generation-fenced like a
  // comment (the node's attempt separate from the displayed run generation), so a late click cannot
  // re-tag a replacement run or a reset incarnation of the same numeric node id. Folds with
  // `operator-edited` provenance the classifier re-tag cadence must not clobber.
  retagConcepts: (rid, { nodeId, nodeGeneration, concepts }, options = {}) => runCommand(
    rid, 'concept_tag_edited', {
      node_id: nodeId, node_generation: nodeGeneration, concepts,
    }, options),
  promote: (rid, id, generation) => runCommand(
    rid, 'promote', { node_id: id, generation, alias: 'champion' }),
  // Operator-authored experiment: hand-add a node to the search tree. `idea` = {operator, params,
  // rationale, theme?}; optional parent_id (branch from a node) and code (ship ready-made code).
  inject: (rid, { idea, parent_id = null, parent_generation = null, code = null }) =>
    runCommand(rid, 'inject_node', {
      idea, parent_id, code,
      parent_generations: parent_id != null && parent_generation != null
        ? { [parent_id]: parent_generation } : undefined,
    }),
  // Fork-to-branch: the operator branches from an experiment they are reading — usually in a
  // HISTORICAL snapshot — with its idea EDITED. Deliberately `inject_node` and not `fork`: `fork`
  // asks the Researcher to improve a node and carries no idea at all, while an operator-authored
  // idea with a parent and a parent-generation CAS is exactly what inject_node already transports.
  // `payload` comes from `forkFromSeqModel.js::buildForkPayload` whole, so the generation fenced
  // here is the one the operator SAW; the server validates `forked_from` and stamps its two derived
  // fields (see `control_validation.py::_normalize_fork_receipt`). Never hand-build this body: the
  // receipt and the CAS must carry ONE generation, which is the model's invariant, not this call's.
  //
  // The only RUN COMMAND that names `allowRunMutationModes`, and `['history']` is its whole content:
  // the client's own run-access envelope marks a run being read at seq N read-only, so without this
  // the request never leaves the browser (`runMode.js::assertRunMutationAllowed`). It admits the
  // HISTORICAL mode only — a review capability, a stale-generation link and an unresolved start-over
  // each keep refusing this command exactly as they refuse every other one. `resetRun` below is the
  // seam's other caller and is deliberately wider (`start-over`/`stale-link`/`history`): Start over
  // is the operation that RESOLVES those two states, so refusing it in them would strand the run. A
  // branch resolves nothing, which is why its list is one entry long.
  forkFrom: (rid, payload, options = {}) => runCommand(rid, 'inject_node', payload, {
    ...options, allowRunMutationModes: ['history'],
  }),
  reopen: (rid) => runCommand(rid, 'run_reopened', {}),
  // U3: merge two nodes — inject a multi-parent `merge` node; the engine recombines the parents'
  // solutions via its real merge/ensemble operator (not a blank manual node).
  merge: (rid, ids, parentGenerations = undefined, options = {}) => runCommand(rid, 'inject_node', {
      idea: { operator: 'merge', rationale: `merge ${ids.map(i => '#' + i).join(' + ')}` },
      parent_ids: ids, parent_generations: parentGenerations,
    }, options),
  // A7/L2: pin the Strategist live. The strict server contract accepts policy/fidelity plus canonical
  // eval_parallel, llm_parallel, the closed llm_lane_limits allocation, and the atomic Card-scoring
  // treatment (never legacy aliases).
  // {policy?, policy_params?, fidelity?, eval_parallel?, llm_parallel?, llm_lane_limits?, card_scoring?}.
  setStrategy: (rid, strategy) => runCommand(rid, 'set_strategy', { strategy }),
  // P2: ask the engine to run Deep Research now (read its disclosed bounded result sample + the web,
  // then write a memo; the compact evidence brief never claims omitted middle results were read).
  deepResearch: (rid) => runCommand(rid, 'deep_research', {}),
  // P1: register an open hypothesis on the board (a question the search should resolve), or drop one.
  addHypothesis: (rid, statement) => runCommand(rid, 'hypothesis_added', { statement, source: 'human' }),
  abandonHypothesis: (rid, id) => runCommand(rid, 'hypothesis_updated', { id, status: 'abandoned' }),
  deleteHypothesis: (rid, id) => runCommand(rid, 'hypothesis_updated', { id, status: 'deleted' }),
  // Layer-6 Card board controls. Authority/provenance is server-stamped; clients submit only the
  // exact subject and editable value through the generation-fenced command protocol.
  reprioritizeCard: (rid, id, priority) => runCommand(
    rid, 'card_reprioritized', { id, priority }),
  editCard: (rid, id, statement) => runCommand(rid, 'card_edited', { id, statement }),
  pinCardResources: (rid, id, gpus, gpuMemMiB = null) => runCommand(
    rid, 'card_resource_pinned', {
      id, gpus, ...(gpuMemMiB == null ? {} : { gpu_mem_mib: gpuMemMiB }),
    }),
  dropCard: (rid, id, reason = 'operator dropped') => runCommand(
    rid, 'card_dropped', { id, reason }),
  // The counterpart a drop never had: an operator putting a stopped card back on the board. Same
  // command path, same generation fence — a reopen is as much a selection decision as the drop was.
  reopenCard: (rid, id, reason = 'operator reopened') => runCommand(
    rid, 'card_reopened', { id, reason }),
  // Workstream A: force a high-quality regeneration of the agent-authored run report now. Dedicated
  // endpoint (not /control) — appends a `report_generated` event. Runs as a background job, so we
  // jobAwait the response (a slow/large regen can't 504 behind a proxy; a fast one returns inline).
  // Contract preserved: resolves to {ok, seq, generation, content} (or {ok:false} offline), never a
  // job_id. The same key rejoins ambiguous retries to one paid server job.
  refreshReport: async (rid, { expectedGeneration, idempotencyKey, signal,
    requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS } = {}) => {
    if (!validRunGeneration(expectedGeneration)) {
      throw runGenerationError(
        'invalid_run_generation',
        'A verified run generation is required before refreshing the report.',
        'Reload the run before generating its report.',
      )
    }
    if (!safeIdentityText(idempotencyKey)) {
      throw new Error('A valid report refresh idempotency key is required.')
    }
    const path = runApiPath(rid, '/report_refresh')
    assertNotReviewMutation(path)
    assertRunMutationAllowed(path)
    const response = await commandJson(path, {
      method: 'POST',
      headers: _authHeaders({
        'Content-Type': 'application/json', 'Idempotency-Key': String(idempotencyKey),
      }),
      body: JSON.stringify({ expected_generation: expectedGeneration }),
      signal,
    }, requestTimeoutMs, { submission: true })
    const result = await jobAwait(response, { maxTransientErrors: 3, signal })
    if (result?.ambiguous !== true
        && (!validRunGeneration(result?.generation) || result.generation !== expectedGeneration)) {
      const error = new Error('Invalid report generation receipt.')
      error.code = 'REPORT_REFRESH_PROTOCOL_ERROR'
      error.ambiguous = true
      error.submissionMayHaveSucceeded = true
      throw error
    }
    return result
  },
  // Generic authoritative command by {type, data}; slash commands and action routers share this path.
  raw: (rid, type, data = {}) => runCommand(rid, type, data),
}

// Apply one assistant/boss action through the same authoritative lifecycle. Report regeneration keeps
// its dedicated background-job endpoint; all event commands delegate engine policy to the server.
export async function appendAction(runId, action, options = {}) {
  if (action.type === '__refresh_report__') return CONTROL.refreshReport(runId, options)
  return CONTROL.raw(runId, action.type, action.data || {})
}
