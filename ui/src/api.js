// The UI's server API: every /api/* endpoint function, the CONTROL action map, and the paid
// concept-lens / artifact / authoring / launch protocols built on top of them. Originally split out
// of util.js (mega-refactor P5.2 — bodies verbatim); util.js re-exports everything, so importers are
// unchanged.
//
// It is now also a BARREL. Six concerns that had re-accreted here moved into their own modules
// (doc 25 UI-02 — bodies verbatim again) and are re-exported below, so no importer of api.js or
// util.js changed:
//
//   apiClient.js          the fetch client + auth/review/prefix plumbing (the security boundary)
//   commandModel.js       the pure command vocabulary, identities and record presentation
//   commandStorage.js     the sessionStorage command envelopes / lock / launch transports
//   commandProtocol.js    submit -> observe -> retry, and the generic background-job await
//   eventStream.js        the WHATWG event-stream parser + authenticated SSE transport
//   scopeReportActions.js the paid scope-report action protocol and its reconciliation
//
// The dependency direction is one-way: those modules never import api.js. A name two of them share
// goes DOWN into apiClient.js or commandModel.js, because under the build's native ESM execution
// order a barrel<->member cycle is a load-time TDZ crash rather than a build error. The re-export
// lists are explicit rather than `export *` so the browser-facing surface stays exactly what it was;
// test/apiBarrel.test.js derives the names consumers actually take and proves each one resolves.

import { assertRunMutationAllowed } from './runMode.js'
import { deadlineRequest } from './requestDeadline.js'
import {
  _authHeaders, _throw, apiUrl, assertNotReviewMutation, deadlineGet, get, post, runApiPath,
  runNodeApiPath, send,
} from './apiClient.js'
import {
  COMMAND_REQUEST_TIMEOUT_MS, TRANSIENT_HTTP, UUID_V4_RE, createIdempotencyKey, hasOnlyKeys,
  runGenerationError, safeIdentityText, validRunGeneration,
} from './commandModel.js'
import { commandJson, commandRead, jobAwait, runCommand } from './commandProtocol.js'
import { createEventStreamParser } from './eventStream.js'

export {
  apiPrefix, apiUrl, authStatus, clearOwnerToken, conditionalGet, deadlineGet, deadlineSharedAssistant, get,
  isReviewLocation, post, putText, reviewReadPath, reviewTokenFromLocation, runApiPath,
  runNodeApiPath, setOwnerToken, verifyOwnerToken,
} from './apiClient.js'
export {
  COMMAND_FAILED, COMMAND_PENDING, COMMAND_SUCCEEDED, commandActionForEvent, commandCanRetry,
  commandErrorMessage,
  commandEventForAction, commandFailureRecord, commandFeedback, createIdempotencyKey,
  getObservedRunGeneration, isTransientCommandReadError, normalizeRunGeneration, observeRunGeneration,
  submitCommand, validRunGeneration,
} from './commandModel.js'
export {
  COMMAND_ENVELOPE_VERSION,
  clearAssistantRunTransport, clearDamagedLaunchTransport, clearLaunchTransport, clearRunCommandLock,
  clearRunTransport, commandRecordMatchesAction, listLaunchTransports, loadAssistantRunTransport,
  loadLaunchTransport, loadRunCommandLock, loadRunTransport, peekReportRefreshIntent,
  reportRefreshIntent, saveAssistantRunTransport, saveLaunchTransport, saveRunCommandLock,
  saveRunTransport, subscribeLaunchTransports, subscribeRunCommandLock,
} from './commandStorage.js'
export {
  getRunCommand, getRunGeneration, jobAwait, retryRunCommand, runCommand, submitRunCommand,
} from './commandProtocol.js'
export { createEventStreamParser, fetchEventStream } from './eventStream.js'
export {
  abandonScopeReportAction, genScopeReport, getScopeReport, getScopeReportAction,
  reconcileScopeReportGeneration,
} from './scopeReportActions.js'

async function paidConceptLensPost(runId, suffix, body, {
  idempotencyKey, signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  if (!validRunGeneration(body?.expected_generation)) {
    throw runGenerationError('invalid_run_generation',
      'A verified run generation is required before paid concept-lens work.',
      'Reload Concepts before continuing this request.')
  }
  if (!safeIdentityText(idempotencyKey)) {
    throw new Error('A valid saved concept-lens idempotency key is required.')
  }
  const path = runApiPath(runId, suffix)
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  try {
    return await commandJson(path, {
      method: 'POST', signal,
      headers: _authHeaders({
        'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey,
      }),
      body: JSON.stringify(body),
    }, requestTimeoutMs, { submission: true })
  } catch (error) {
    if (error?.status == null || error?.code === 'COMMAND_REQUEST_TIMEOUT'
        || error?.code === 'COMMAND_PROTOCOL_ERROR') error.submissionMayHaveSucceeded = true
    throw error
  }
}

export const submitConceptLens = (runId, prompt, expectedGeneration, options) =>
  paidConceptLensPost(runId, '/concepts/lens', {
    prompt, expected_generation: expectedGeneration,
  }, options)

export const abandonConceptLens = (runId, expectedGeneration, requestId, options) =>
  paidConceptLensPost(runId, '/concepts/lens/abandon', {
    expected_generation: expectedGeneration, request_id: requestId,
  }, options)

// Lost-tab recovery is deliberately owner-plane even though discovery is a GET.  Do not route it
// through get(): reviewReadPath() would translate the URL into a reviewer capability, while this
// projection is the authority used to decide whether another paid identity may be created.
export async function getConceptLensRecovery(runId, expectedGeneration, {
  signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
} = {}) {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError('invalid_run_generation',
      'A verified run generation is required before paid concept-lens recovery.',
      'Reload Concepts before inspecting paid work.')
  }
  const basePath = runApiPath(runId, '/concepts/lens/recovery')
  assertNotReviewMutation(basePath)
  const path = `${basePath}?expected_generation=${encodeURIComponent(expectedGeneration)}`
  return commandRead(path, { errorPath: basePath, signal, requestTimeoutMs })
}

export function awaitConceptLensRecoveryJob(jobId, options = {}) {
  const path = `/api/jobs/${encodeURIComponent(String(jobId || ''))}`
  assertNotReviewMutation(path)
  if (!/^[0-9a-f]{16}$/.test(jobId || '')) {
    throw new Error('An exact recovered concept-lens job id is required.')
  }
  return jobAwait({ status: 'running', job_id: jobId }, options)
}

export async function abandonRecoveredConceptLens(
  runId, expectedGeneration, requestId, expectedStartedSeq, {
    resolutionIdempotencyKey, signal, requestTimeoutMs = COMMAND_REQUEST_TIMEOUT_MS,
  } = {},
) {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError('invalid_run_generation',
      'A verified run generation is required before resolving recovered paid work.',
      'Reload Concepts and inspect the current recovery receipt.')
  }
  if (!safeIdentityText(requestId) || !/^[0-9a-f]{64}$/.test(requestId)) {
    throw new Error('An exact recovered concept-lens request id is required.')
  }
  if (!Number.isSafeInteger(expectedStartedSeq) || expectedStartedSeq < 0) {
    throw new Error('An exact recovered concept-lens start sequence is required.')
  }
  if (!UUID_V4_RE.test(resolutionIdempotencyKey || '')) {
    throw new Error('A valid recovery resolution idempotency key is required.')
  }
  const path = runApiPath(runId, '/concepts/lens/recovery/abandon')
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  try {
    return await commandJson(path, {
      method: 'POST', signal,
      headers: _authHeaders({
        'Content-Type': 'application/json',
        'Resolution-Idempotency-Key': resolutionIdempotencyKey,
      }),
      body: JSON.stringify({
        expected_generation: expectedGeneration,
        request_id: requestId,
        expected_started_seq: expectedStartedSeq,
      }),
    }, requestTimeoutMs, { submission: true })
  } catch (error) {
    if (error?.status == null || error?.code === 'COMMAND_REQUEST_TIMEOUT'
        || error?.code === 'COMMAND_PROTOCOL_ERROR') error.submissionMayHaveSucceeded = true
    throw error
  }
}

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

// Generation-fenced, operation-idempotent Replay: archive the finished generation and re-spawn the
// same run id. The durable operation id lets an unknown browser outcome rejoin the exact request.
export const resetRun = (rid, expectedGeneration, operationId, options = {}) =>
  post(runApiPath(rid, '/reset'), {
    expected_generation: expectedGeneration,
    operation_id: operationId,
  }, { ...options, allowRunMutationModes: ['start-over', 'stale-link', 'history'] })

// Clear ONE node's trace only for the exact run + node lifecycle the operator inspected. Never
// substitute a later observed generation here: the confirmation belongs to the rendered snapshot,
// and a stale tab must fail closed instead of deleting a replacement run/attempt's diagnostics.
export const clearNodeTrace = (
  rid, id, {
    expectedGeneration, expectedTraceRevision, nodeGeneration, operationId, signal,
  } = {},
) => {
  if (!validRunGeneration(expectedGeneration)) {
    const error = new Error('An exact run generation is required to clear trace data.')
    error.code = 'run_generation_unavailable'
    throw error
  }
  if (!Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0) {
    const error = new Error('An exact experiment attempt is required to clear trace data.')
    error.code = 'node_generation_unavailable'
    throw error
  }
  if (!validRunGeneration(expectedTraceRevision)) {
    const error = new Error('An exact trace snapshot is required to clear trace data.')
    error.code = 'trace_revision_unavailable'
    throw error
  }
  if (!/^tc_[0-9a-f]{32}$/.test(operationId || '')) {
    const error = new Error('A stable trace clear operation id is required.')
    error.code = 'trace_clear_operation_unavailable'
    throw error
  }
  return post(runNodeApiPath(rid, id, '/clear_trace'), {
    expected_generation: expectedGeneration,
    expected_trace_revision: expectedTraceRevision,
    node_generation: nodeGeneration,
    operation_id: operationId,
  }, { signal })
}

const validSettingsRevision = value => typeof value === 'string' && value.length > 0 && value.length <= 256
const validLlmHealthIdentity = value => typeof value === 'string'
  && /^probe-v1:[\da-f]{64}$/i.test(value)
const validLlmHealthFailureCode = value => typeof value === 'string'
  && /^[a-z][a-z0-9_:-]{0,127}$/i.test(value)

const llmHealthRevisionError = (code, message, detail = null) => {
  const error = new Error(message)
  error.code = code
  if (detail) error.detail = detail
  return error
}

export async function llmHealth(
  expectedSettingsRevision, expectedSecretRevision, operationId, options = {},
) {
  if (!validSettingsRevision(expectedSettingsRevision) || !validSettingsRevision(expectedSecretRevision)) {
    throw llmHealthRevisionError(
      'llm_health_revision_unavailable',
      'Saved settings revisions are required before testing the LLM configuration.',
    )
  }
  if (!UUID_V4_RE.test(operationId || '')) {
    throw llmHealthRevisionError(
      'llm_health_operation_unavailable',
      'A unique operation identity is required before testing the LLM configuration.',
    )
  }
  const { replayOnly = false, ...requestOptions } = options
  let payload
  try {
    payload = await post('/api/llm/health', {
      expected_settings_revision: expectedSettingsRevision,
      expected_secret_revision: expectedSecretRevision,
      operation_id: operationId,
      replay_only: replayOnly === true,
    }, requestOptions)
  } catch (error) {
    const detail = error?.detail && typeof error.detail === 'object'
      && !Array.isArray(error.detail) ? error.detail : null
    const exactErrorEnvelope = detail
      && detail.operation_id === operationId
      && detail.expected_settings_revision === expectedSettingsRevision
      && detail.expected_secret_revision === expectedSecretRevision
      && validLlmHealthFailureCode(detail.code)
      && typeof detail.provider_attempted === 'boolean'
      && typeof detail.outcome_unknown === 'boolean'
      && (detail.ambiguous == null || typeof detail.ambiguous === 'boolean')
      && !(detail.ambiguous === true && detail.outcome_unknown !== true)
    if (!exactErrorEnvelope) {
      throw llmHealthRevisionError(
        'llm_health_identity_protocol_error',
        'The LLM health error did not prove which operation or saved configuration it belonged to.',
        {
          operation_id: operationId,
          provider_attempted: detail?.provider_attempted === true,
          ambiguous: true,
          outcome_unknown: true,
        },
      )
    }
    throw error
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || payload.operation_id !== operationId
      || payload.settings_revision !== expectedSettingsRevision
      || payload.secret_revision !== expectedSecretRevision
      || typeof payload.ok !== 'boolean'
      || typeof payload.provider_attempted !== 'boolean'
      || typeof payload.outcome_unknown !== 'boolean'
      || (payload.ambiguous != null && typeof payload.ambiguous !== 'boolean')
      || !validLlmHealthIdentity(payload.effective_identity)
      || (payload.ok === true
        && (payload.provider_attempted !== true || payload.outcome_unknown !== false))
      || (payload.outcome_unknown === true && payload.provider_attempted !== true)
      || (payload.ambiguous === true && payload.outcome_unknown !== true)
      || (payload.ok === false
        && !validLlmHealthFailureCode(payload.code || payload.error_kind))) {
    throw llmHealthRevisionError(
      'llm_health_identity_protocol_error',
      'The LLM health response did not match the requested operation, saved settings revisions, or provider-attempt contract.',
      {
        operation_id: operationId,
        provider_attempted: payload?.provider_attempted === true,
        // A malformed success envelope cannot prove that a paid provider attempt did not happen.
        ambiguous: true,
        outcome_unknown: true,
      },
    )
  }
  return payload
}

const artifactGenerationQuery = expectedGeneration => {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError(
      'run_generation_unavailable',
      'A verified run generation is required before reading files.',
      'Wait for the current run identity, then reopen Files.',
    )
  }
  const query = new URLSearchParams()
  query.set('expected_generation', expectedGeneration)
  return query
}

const validateArtifactGeneration = (payload, expectedGeneration, path) => {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || !validRunGeneration(payload.run_generation)) {
    const error = new Error(`${path}: invalid artifact generation receipt`)
    error.code = 'artifact_generation_protocol_error'
    throw error
  }
  if (payload.run_generation !== expectedGeneration) {
    const error = runGenerationError(
      'run_generation_changed',
      'The run changed while files were loading.',
      'Reload the file inventory for the current run generation.',
    )
    error.status = 409
    error.detail = {
      expected_generation: expectedGeneration,
      current_generation: payload.run_generation,
    }
    throw error
  }
  return payload
}

const artifactProtocolError = (code, message, path) => {
  const error = new Error(`${path}: ${message}`)
  error.code = code
  return error
}

const validateArtifactContentIdentity = (payload, expected, requestPath) => {
  if (payload.root !== expected.root || payload.path !== expected.path) {
    throw artifactProtocolError(
      'artifact_path_protocol_error',
      'the response did not echo the requested file identity',
      requestPath,
    )
  }
  const responseHasNodeIdentity = payload.node_id != null || payload.attempt != null
  if (responseHasNodeIdentity !== expected.hasNodeIdentity
      || (expected.hasNodeIdentity
        && (payload.node_id !== expected.nodeId || payload.attempt !== expected.attempt))) {
    throw artifactProtocolError(
      'artifact_attempt_protocol_error',
      'the response did not echo the requested experiment attempt',
      requestPath,
    )
  }
  return payload
}

export async function getRunArtifactInventory(runId, expectedGeneration, options = {}) {
  const query = artifactGenerationQuery(expectedGeneration)
  const path = runApiPath(runId, `/artifacts?${query}`)
  const payload = await get(path, { ...options, cache: 'no-store' })
  const fenced = validateArtifactGeneration(payload, expectedGeneration, path)
  if (fenced.run_id !== String(runId)) {
    throw artifactProtocolError(
      'artifact_run_protocol_error',
      'the response did not echo the requested run identity',
      path,
    )
  }
  return fenced
}

export async function getRunArtifactContent(runId, {
  root, path: artifactPath, expectedGeneration, nodeId = null, attempt = null, ...options
} = {}) {
  if (typeof root !== 'string' || typeof artifactPath !== 'string') {
    throw artifactProtocolError(
      'artifact_path_protocol_error',
      'the file inventory returned an invalid root or path',
      runApiPath(runId, '/artifact'),
    )
  }
  const query = artifactGenerationQuery(expectedGeneration)
  query.set('root', root)
  query.set('path', artifactPath)
  const hasNodeIdentity = nodeId != null || attempt != null
  if (hasNodeIdentity) {
    if (!Number.isSafeInteger(nodeId) || nodeId < 0
        || !Number.isSafeInteger(attempt) || attempt < 0) {
      const error = new Error('The artifact inventory returned an invalid experiment attempt identity.')
      error.code = 'artifact_attempt_protocol_error'
      throw error
    }
    query.set('node_id', String(nodeId))
    query.set('attempt', String(attempt))
  }
  const requestPath = runApiPath(runId, `/artifact?${query}`)
  const payload = await get(requestPath, { ...options, cache: 'no-store' })
  const fenced = validateArtifactGeneration(payload, expectedGeneration, requestPath)
  return validateArtifactContentIdentity(fenced, {
    root,
    path: artifactPath,
    nodeId,
    attempt,
    hasNodeIdentity,
  }, requestPath)
}

export const AUTHORING_MISSING_REVISION = 'missing'
const AUTHORING_KINDS = new Set(['prompts', 'skills', 'knowledge'])
const AUTHORING_MAX_BYTES = 256 * 1024
const AUTHORING_REVISION_RE = /^(?:missing|sha256:[0-9a-f]{64})$/
const AUTHORING_RESULT_REVISION_RE = /^(?:missing|oversized|sha256:[0-9a-f]{64})$/
const AUTHORING_TARGET_ROOT_ID_RE = /^root-sha256:[0-9a-f]{64}$/
const AUTHORING_RECEIPT_KEYS = new Set([
  'schema', 'operation_id', 'kind', 'name', 'target_root_id', 'expected_revision',
  'desired_revision', 'status', 'result_revision', 'code', 'created_at', 'updated_at',
  'ok', 'replayable',
])
export const validAuthoringName = value => typeof value === 'string'
  && value.length > 3 && [...value].length <= 255 && value.endsWith('.md')
  && !/[\\/]/.test(value) && !/\p{C}/u.test(value)
const validAuthoringOperationId = value => typeof value === 'string'
  && value.length === 36 && value === value.toLowerCase() && UUID_V4_RE.test(value)
const validAuthoringRevision = value => typeof value === 'string'
  && (value === AUTHORING_MISSING_REVISION
    || (value.length === 71 && AUTHORING_REVISION_RE.test(value)))
const validAuthoringDesiredRevision = value => value !== AUTHORING_MISSING_REVISION
  && validAuthoringRevision(value)
const validAuthoringResultRevision = value => typeof value === 'string'
  && (value === AUTHORING_MISSING_REVISION || value === 'oversized'
    || (value.length === 71 && AUTHORING_RESULT_REVISION_RE.test(value)))
export const validAuthoringTargetRootId = value => typeof value === 'string'
  && value.length === 76 && AUTHORING_TARGET_ROOT_ID_RE.test(value)
const authoringTextWellFormed = text => {
  if (typeof text !== 'string') return false
  if (typeof text.isWellFormed === 'function') return text.isWellFormed()
  for (let index = 0; index < text.length; index++) {
    const unit = text.charCodeAt(index)
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = text.charCodeAt(index + 1)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false
      index++
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false
  }
  return true
}

const authoringOperationPath = (kind, name, operationId) => {
  const normalizedKind = String(kind || '')
  const normalizedName = String(name || '')
  const normalizedOperationId = String(operationId || '')
  if (!AUTHORING_KINDS.has(normalizedKind) || !validAuthoringName(normalizedName)
      || !validAuthoringOperationId(normalizedOperationId)) {
    const error = new Error('Invalid authoring operation identity')
    error.code = 'AUTHORING_PROTOCOL_ERROR'
    throw error
  }
  return `/api/${encodeURIComponent(normalizedKind)}/${encodeURIComponent(normalizedName)}`
    + `/operations/${encodeURIComponent(normalizedOperationId)}`
}

function authoringProtocolError(message, receipt = null) {
  const error = new Error(message)
  error.code = 'AUTHORING_PROTOCOL_ERROR'
  if (receipt && typeof receipt === 'object') error.receipt = receipt
  return error
}

export async function authoringTextRevision(text, source = globalThis.crypto) {
  if (typeof text !== 'string' || typeof TextEncoder === 'undefined'
      || typeof source?.subtle?.digest !== 'function') {
    throw authoringProtocolError('Secure UTF-8 hashing is unavailable for this authoring payload.')
  }
  if (!authoringTextWellFormed(text)) {
    throw authoringProtocolError('Authoring text contains an unpaired Unicode surrogate.')
  }
  const bytes = new TextEncoder().encode(text)
  if (bytes.byteLength > AUTHORING_MAX_BYTES) {
    throw authoringProtocolError(`Authoring text exceeds ${AUTHORING_MAX_BYTES} UTF-8 bytes.`)
  }
  const digest = await source.subtle.digest('SHA-256', bytes)
  return 'sha256:' + [...new Uint8Array(digest)]
    .map(value => value.toString(16).padStart(2, '0')).join('')
}

export function validateAuthoringReceipt(receipt, expected = {}) {
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)
      || !hasOnlyKeys(receipt, AUTHORING_RECEIPT_KEYS)
      || Object.keys(receipt).length !== AUTHORING_RECEIPT_KEYS.size
      || receipt.schema !== 'looplab.authoring-operation/v1'
      || !validAuthoringOperationId(receipt.operation_id)
      || !AUTHORING_KINDS.has(receipt.kind)
      || !validAuthoringName(receipt.name)
      || !validAuthoringTargetRootId(receipt.target_root_id)
      || !validAuthoringRevision(receipt.expected_revision)
      || !validAuthoringDesiredRevision(receipt.desired_revision)
      || !['prepared', 'succeeded', 'conflict'].includes(receipt.status)
      || !Number.isSafeInteger(receipt.created_at) || receipt.created_at < 0
      || !Number.isSafeInteger(receipt.updated_at) || receipt.updated_at < receipt.created_at) {
    throw authoringProtocolError('The server returned an invalid authoring receipt.', receipt)
  }
  const validTerminal = receipt.status === 'prepared'
    ? receipt.result_revision === null && receipt.code === null
    : receipt.status === 'succeeded'
      ? receipt.result_revision === receipt.desired_revision && receipt.code === null
      : validAuthoringResultRevision(receipt.result_revision)
        && ['authoring_revision_conflict', 'authoring_intervening_write'].includes(receipt.code)
  if (!validTerminal || receipt.ok !== (receipt.status === 'succeeded')
      || receipt.replayable !== (receipt.status === 'prepared')) {
    throw authoringProtocolError('The server returned an inconsistent authoring receipt.', receipt)
  }
  const expectedOperationId = expected.operationId == null ? null : String(expected.operationId)
  const expectedKind = expected.kind == null ? null : String(expected.kind)
  const expectedName = expected.name == null ? null : String(expected.name)
  const expectedRevision = expected.expectedRevision == null
    ? null : String(expected.expectedRevision)
  const expectedTargetRootId = expected.expectedTargetRootId == null
    ? null : String(expected.expectedTargetRootId)
  const desiredRevision = expected.desiredRevision == null
    ? null : String(expected.desiredRevision)
  if ((expectedOperationId != null && receipt.operation_id !== expectedOperationId)
      || (expectedKind != null && receipt.kind !== expectedKind)
      || (expectedName != null && receipt.name !== expectedName)
      || (expectedTargetRootId != null && receipt.target_root_id !== expectedTargetRootId)
      || (expectedRevision != null && receipt.expected_revision !== expectedRevision)
      || (desiredRevision != null && receipt.desired_revision !== desiredRevision)) {
    throw authoringProtocolError(
      'The authoring receipt does not match the requested operation identity.', receipt)
  }
  return receipt
}

export async function putAuthoringOperation(
  kind, name, operationId, {
    text, expectedRevision, expectedTargetRootId, desiredRevision = null,
  }, { signal } = {},
) {
  const path = authoringOperationPath(kind, name, operationId)
  if (typeof text !== 'string' || !validAuthoringRevision(expectedRevision)
      || !validAuthoringTargetRootId(expectedTargetRootId)) {
    throw authoringProtocolError('Invalid authoring operation payload.')
  }
  const submittedRevision = await authoringTextRevision(text)
  if (desiredRevision != null && desiredRevision !== submittedRevision) {
    throw authoringProtocolError('The authoring payload does not match its durable desired revision.')
  }
  const receipt = await send(path, 'PUT', {
    text,
    expected_revision: expectedRevision,
    expected_target_root_id: expectedTargetRootId,
  }, { signal })
  return validateAuthoringReceipt(receipt, {
    operationId, kind, name, expectedRevision, expectedTargetRootId,
    desiredRevision: submittedRevision,
  })
}

export async function getAuthoringOperation(
  kind, name, operationId, {
    signal, expectedRevision, expectedTargetRootId, desiredRevision,
  } = {},
) {
  if (!validAuthoringTargetRootId(expectedTargetRootId)
      || !validAuthoringRevision(expectedRevision)
      || !validAuthoringDesiredRevision(desiredRevision)) {
    throw authoringProtocolError('An exact authoring operation identity is required for receipt lookup.')
  }
  const path = authoringOperationPath(kind, name, operationId)
    + `?expected_target_root_id=${encodeURIComponent(expectedTargetRootId)}`
    + `&expected_revision=${encodeURIComponent(expectedRevision)}`
    + `&desired_revision=${encodeURIComponent(desiredRevision)}`
  const receipt = await get(path, { signal, cache: 'no-store' })
  return validateAuthoringReceipt(receipt, {
    operationId, kind, name, expectedRevision, expectedTargetRootId, desiredRevision,
  })
}

// ---- ClearML-style project API ----
export const listProjects = options => get('/api/projects', options)
export const createProject = (name, parent_id = null) => post('/api/projects', { name, parent_id })
export const patchProject = (id, body) => send(`/api/projects/${encodeURIComponent(id)}`, 'PATCH', body)
export const deleteProject = (id) => send(`/api/projects/${encodeURIComponent(id)}`, 'DELETE')
const runOrganizationBody = (field, value, expectedGeneration, expectedCurrent) => {
  if (!validRunGeneration(expectedGeneration)) {
    throw runGenerationError(
      'run_generation_unavailable',
      'An exact observed run generation is required before changing run organization.',
      'Refresh the Runs list and repeat the change on the intended run.',
    )
  }
  if (expectedCurrent !== null && typeof expectedCurrent !== 'string') {
    throw runGenerationError(
      'run_organization_unavailable',
      'The exact current organization value is required before changing run organization.',
      'Refresh the Runs list and repeat the change from the current run card.',
    )
  }
  return {
    [field]: value,
    [`expected_${field}`]: expectedCurrent,
    expected_generation: expectedGeneration,
  }
}
export const assignRun = (runId, project_id, expectedGeneration, expectedProjectId) => post(
  runApiPath(runId, '/project'),
  runOrganizationBody('project_id', project_id, expectedGeneration, expectedProjectId))
export const renameRun = (runId, label, expectedGeneration, expectedLabel) => send(
  runApiPath(runId), 'PATCH',
  runOrganizationBody('label', label, expectedGeneration, expectedLabel))
export function submitRunDeletion(runId, expectedGeneration, expectedSeq, operationId, options = {}) {
  if (!validRunGeneration(expectedGeneration)) {
    throw Object.assign(new Error('An exact run generation is required to delete a run.'), {
      code: 'run_generation_unavailable',
    })
  }
  if (!Number.isSafeInteger(expectedSeq) || expectedSeq < -1) {
    throw Object.assign(new Error('An exact run sequence is required to delete a run.'), {
      code: 'run_sequence_unavailable',
    })
  }
  if (!UUID_V4_RE.test(operationId || '')) {
    throw Object.assign(new Error('A stable deletion operation id is required.'), {
      code: 'delete_operation_invalid',
    })
  }
  // `delete_memory` is sent ONLY when true. The server's body validator is a strict key set, and an
  // always-present `false` would make every existing deletion request a different shape for no gain.
  const body = {
    operation_id: String(operationId).toLowerCase(),
    expected_generation: expectedGeneration,
    expected_seq: expectedSeq,
  }
  if (options.deleteMemory === true) body.delete_memory = true
  return post(runApiPath(runId, '/deletions'), body, options)
}

/** What a cascading delete would remove from cross-run memory. Read-only; deletes nothing. */
export const getRunMemoryAttribution = (runId, options = {}) => get(
  runApiPath(runId, '/memory-attribution'), options)

// Finishes a cascade whose store was locked when the run was deleted. Idempotent, and it must CARRY
// the identity: this is only ever reached after the run is gone, and the server refuses an empty
// body in exactly that case (400 `memory_purge_identity_required`) rather than guess which run a
// reused directory name means. Posting `{}` made the retry button unable to succeed even once. Both
// halves come from the deletion receipt's `memory` block — `memory_dir` because it is a per-RUN
// setting, so falling back to the server's current global store finds nothing and reports a clean
// `deleted: 0` over rows that are still there.
export const purgeRunMemory = (runId, identity = {}, options = {}) => post(
  runApiPath(runId, '/memory-purge'),
  { run_uid: String(identity?.run_uid || ''), memory_dir: String(identity?.memory_dir || '') },
  options)

export function getRunDeletion(runId, operationId, options = {}) {
  if (!UUID_V4_RE.test(operationId || '')) {
    throw Object.assign(new Error('A stable deletion operation id is required.'), {
      code: 'delete_operation_invalid',
    })
  }
  return get(runApiPath(runId, `/deletions/${encodeURIComponent(String(operationId).toLowerCase())}`), {
    ...options, cache: 'no-store',
  })
}
export const createRunReview = (runId, {
  ttl_seconds, include_evidence = false, expected_generation, request_id, token_secret,
} = {}, options) =>
  post(runApiPath(runId, '/reviews'),
    { ttl_seconds, include_evidence, expected_generation, request_id, token_secret }, options)
export const listRunReviews = (runId, options) =>
  get(runApiPath(runId, '/reviews'), options)
export const revokeRunReview = (runId, linkId, options) =>
  send(runApiPath(runId, `/reviews/${encodeURIComponent(linkId)}`),
    'DELETE', null, options)

// Bounded collaboration projections. In review mode reviewReadPath() translates both owner paths to
// `/api/review/comments...`; the capability still supplies the run identity and every mutation is
// rejected before fetch by assertNotReviewMutation().
export const runComments = (runId, {
  nodeId = null, nodeGeneration = null, includeResolved = true, limit = 100, cursor = null,
  signal,
} = {}) => {
  const query = new URLSearchParams()
  if (nodeId != null) query.set('node_id', String(nodeId))
  if (nodeGeneration != null) query.set('node_generation', String(nodeGeneration))
  query.set('include_resolved', includeResolved ? 'true' : 'false')
  query.set('limit', String(Math.max(1, Math.min(100, Math.trunc(Number(limit) || 100)))))
  if (cursor != null) query.set('cursor', String(cursor))
  return get(runApiPath(runId, `/comments?${query}`),
    { cache: 'no-store', signal })
}
export const commentHistory = (runId, commentId, { limit = 100, cursor = null } = {}) => {
  const query = new URLSearchParams()
  query.set('limit', String(Math.max(1, Math.min(100, Math.trunc(Number(limit) || 100)))))
  if (cursor != null) query.set('cursor', String(cursor))
  return get(runApiPath(runId, `/comments/${encodeURIComponent(commentId)}/history?${query}`),
    { cache: 'no-store' })
}

// super-tasks: a user-managed, flat grouping of runs by the global task they attack (parallel axis
// to projects). create / rename / delete the bucket, then assign any run (existing or new) to it.
export const listSupertasks = options => get('/api/supertasks', options)
export const createSupertask = (name, task_id = null) => post('/api/supertasks', { name, task_id })
export const renameSupertask = (id, name) => send(`/api/supertasks/${encodeURIComponent(id)}`, 'PATCH', { name })
export const deleteSupertask = (id) => send(`/api/supertasks/${encodeURIComponent(id)}`, 'DELETE')
export const assignSupertask = (
  runId, supertask_id, expectedGeneration, expectedSupertaskId,
) => post(
  runApiPath(runId, '/supertask'),
  runOrganizationBody(
    'supertask_id', supertask_id, expectedGeneration, expectedSupertaskId))

export const gpuStat = () => get('/api/gpu')

// ---- settings + run launch ----
export const getSettings = () => get('/api/settings')
export const getSettingsSchema = (options = {}) => get('/api/settings/schema/2', options)
export const saveSettings = (settings, { expectedRevision, ...options } = {}) =>
  send('/api/settings', 'PUT', {
    settings, ...(expectedRevision == null ? {} : { expected_revision: expectedRevision }),
  }, options)
// Store (or clear, value='') a secret credential. The write is fenced by the exact settings +
// secret snapshot displayed by the owner: accepting only one revision could bind a key to an
// endpoint the user never reviewed. The value itself is never echoed back.
export const saveSecret = (key, value, {
  expectedSettingsRevision, expectedSecretRevision, ...options
} = {}) =>
  send('/api/settings/secret', 'PUT', {
    key,
    value,
    ...(expectedSettingsRevision == null
      ? {} : { expected_settings_revision: expectedSettingsRevision }),
    ...(expectedSecretRevision == null
      ? {} : { expected_secret_revision: expectedSecretRevision }),
  }, options)
// Per-run settings: edit a specific run's config.snapshot.json so the next RESUME picks up the
// change (only changed fields are sent). A live engine keeps its in-memory copy until restart.
export const saveRunConfig = (rid, settings, {
  expectedRevision, expectedGeneration, ...options
} = {}) =>
  send(runApiPath(rid, '/config'), 'PUT', {
    settings, ...(expectedRevision == null ? {} : { expected_revision: expectedRevision }),
    ...(expectedGeneration == null ? {} : { expected_generation: expectedGeneration }),
  }, options)

// Experimental Claims & Curation reads: owner-only, read-only projections over the shared memory
// portfolio. The ROUTE names below are the server's and are unchanged by the F7 surface rename
// (doc 29) — `/api/cross-run/atlas` still serves the mixed-evidence claim records the screen reads.
// Bypass browser caches so Refresh observes newly finalized runs/governance without a stale intermediary.
const crossRunRead = (path, options = {}) => get(path, { ...options, cache: 'no-store' })
export function boundedLedgerText(value, max = 360) {
  if (!['string', 'number', 'boolean'].includes(typeof value)) return ''
  const limit = Number.isSafeInteger(max) ? Math.max(0, Math.min(2000, max)) : 360
  const text = String(value).slice(0, limit)
  return text.replace(/[\u0000-\u001f\u007f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, ' ')
    .replace(/\s+/gu, ' ').trim().slice(0, limit)
}
// An ALLOWLIST: a field absent here never reaches React state. F7 dropped the concepts section, so
// the concept sections of the atlas envelope (`explored`/`thin_coverage` — up to 24 rows carrying 6
// run references each) and the `concept_capsules.jsonl` read receipt beside them are no longer
// listed. They are still SERVED; nothing renders them, so nothing keeps them.
//
// THE OTHER DIRECTION IS THE ONE THAT BITES, and it had: a field this list omits that something
// DOES render is not a smaller payload, it is a render branch no server response can reach — and it
// is silent, because the field simply arrives `undefined`. Five of `CrossRunClaim`'s were in exactly
// that state (`decision`/`note`/`by`/`at`, `polarity`, `sources`, `verification`,
// `evidence_digest`), so `ClaimsCuration.jsx`'s Decision line, the polarity half of its metric line
// and its whole "Sources and verification" disclosure were dead markup while
// `claimsCurationModel.js::normalizeClaim` went on bounding and validating all five. On a Claims &
// Curation screen the steward's own verdict — who ratified a claim, when, and why — is the thing an
// operator came for. `ui/test/claimsCuration.test.js` now derives the model's wire reads and fails
// on any name that is not here, so the two halves cannot drift apart again.
const CROSS_RUN_STATE_FIELDS = `portfolio_id n_runs n_contested
  claim_source contradictions
  revisions claims n revision v status complete entries limit source_complete
  runs run_id metric polarity sources verification evidence_digest
  decision note by at
  claim_uid statement epistemic maturity decision_fresh n_support n_oppose n_unverified
  n_contradicts support oppose unverified contradicts scopes receipt_known read_complete
  research_source_complete lessons research snapshot_digest rows_total rows_retained
  rows_quarantined malformed_rows invalid_rows outcome proposals receipt merges splits purges
  decisions applied concept_governance`.split(/\s+/)
const CROSS_RUN_STATE_CAPS = {
  contradictions: 12, claims: 40, entries: 20,
  runs: 6, support: 6, oppose: 6, unverified: 6, contradicts: 6, scopes: 6,
}
const CROSS_RUN_COUNT_ARRAYS = new Set(['merges', 'splits', 'purges', 'decisions', 'applied'])
function projectCrossRunValue(value, key = '', depth = 0) {
  if (typeof value === 'string') return boundedLedgerText(value, 500)
  if (typeof value === 'number' || typeof value === 'boolean' || value == null) return value
  if (Array.isArray(value)) {
    if (CROSS_RUN_COUNT_ARRAYS.has(key)) return value.length
    return value.slice(0, CROSS_RUN_STATE_CAPS[key] || 6)
      .map(item => projectCrossRunValue(item, key, depth + 1))
  }
  if (typeof value !== 'object' || depth >= 7) return null
  const out = {}
  for (const field of CROSS_RUN_STATE_FIELDS) {
    if (Object.hasOwn(value, field)) {
      out[field] = projectCrossRunValue(value[field], field, depth + 1)
    }
  }
  return out
}
export function projectLedgerSource(key, value) {
  const projected = projectCrossRunValue(value)
  if (key === 'atlas') {
    const contested = Array.isArray(value?.contradictions) ? value.contradictions.length : 0
    projected.n_contested = Math.max(
      Number.isSafeInteger(projected.n_contested) && projected.n_contested >= 0
        ? projected.n_contested : 0,
      contested,
    )
  }
  return projected
}
const boundedCrossRunInt = (value, fallback, maximum, minimum = 0) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, Math.trunc(parsed))) : fallback
}
const crossRunLimitArgs = (limitOrOptions, fallback, maximum, options) =>
  limitOrOptions && typeof limitOrOptions === 'object'
    ? { limit: fallback, options: limitOrOptions }
    : { limit: boundedCrossRunInt(limitOrOptions, fallback, maximum, 1), options }
// Bounds exist on both sides of the wire. Client render caps prevent DOM amplification; these query
// caps also prevent a routine claim-ledger navigation from requesting an unbounded shared ledger.
export const getCrossRunAtlas = (limitOrOptions = 24, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 24, 50, options)
  return crossRunRead(`/api/cross-run/atlas?limit=${args.limit}`, args.options)
}
export const getCrossRunClaims = (limitOrOptions = 80, offset = 0, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 80, 200, options)
  const offsetIsOptions = offset && typeof offset === 'object'
  if (offsetIsOptions && args.options == null) args.options = offset
  return crossRunRead(
    `/api/cross-run/claims?limit=${args.limit}&offset=${boundedCrossRunInt(offsetIsOptions ? 0 : offset, 0, 1_000_000)}`,
    args.options)
}
export const getCrossRunCurationLog = (limitOrOptions = 20, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 20, 50, options)
  return crossRunRead(`/api/cross-run/curation-log?limit=${args.limit}`, args.options)
}
export const getCrossRunClaimCurationLog = (limitOrOptions = 20, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 20, 50, options)
  return crossRunRead(`/api/cross-run/claim-curation-log?limit=${args.limit}`, args.options)
}
// These four sat in the block of shared constants at the top of api.js; the block moved out with the
// vocabulary that needed sharing (doc 25 UI-02) and nothing outside this section ever read them.
const LAUNCH_PREFLIGHT_TIMEOUT_MS = 12_000
const LAUNCH_SUBMISSION_TIMEOUT_MS = 12_000
const LAUNCH_STATUS_TIMEOUT_MS = 5_000
const MAX_LAUNCH_REQUEST_TIMEOUT_MS = 60_000
// New-run creation is propose -> edit -> validate -> start.  The preflight is non-billable and
// side-effect free; its opaque token binds the exact payload the server checked.  An inconclusive
// launch is observed by idempotency key instead of blindly POSTing a second engine start.
const boundedLaunchTimeout = (value, fallback) => {
  const parsed = Number(value)
  return Number.isFinite(parsed)
    ? Math.max(1, Math.min(MAX_LAUNCH_REQUEST_TIMEOUT_MS, Math.trunc(parsed)))
    : fallback
}

const launchDeadline = async (read, timeoutMs, code, { submission = false } = {}) => {
  try {
    return await deadlineRequest(read, timeoutMs).promise
  } catch (cause) {
    const status = Number(cause?.status)
    const ambiguous = cause?.name === 'TimeoutError' || cause?.status == null
      || (Number.isFinite(status) && (status >= 500 || TRANSIENT_HTTP.has(status)))
    if (cause?.name !== 'TimeoutError' && !(submission && ambiguous)) throw cause
    const error = Object.assign(new Error(submission
      ? 'Startup submission did not return before its deadline.'
      : code === 'LAUNCH_PREFLIGHT_TIMEOUT'
        ? 'Launch validation did not return before its deadline.'
        : 'Startup status did not return before its deadline.'), {
      name: cause?.name === 'TimeoutError' ? 'TimeoutError' : (cause?.name || 'Error'),
      code, transient: true, cause,
      ...(submission ? { submissionMayHaveSucceeded: true } : {}),
    })
    throw error
  }
}

export const preflightRunStart = (body, { requestTimeoutMs = LAUNCH_PREFLIGHT_TIMEOUT_MS } = {}) =>
  launchDeadline(
    signal => post('/api/start/preflight', body, { signal }),
    boundedLaunchTimeout(requestTimeoutMs, LAUNCH_PREFLIGHT_TIMEOUT_MS),
    'LAUNCH_PREFLIGHT_TIMEOUT')

// Exactly one POST leaves for a caller invocation. Any lost/late response is marked ambiguous and
// recovered only through getStartStatus with the caller's already-durable idempotency key.
export const startRun = (body, { requestTimeoutMs = LAUNCH_SUBMISSION_TIMEOUT_MS } = {}) =>
  launchDeadline(
    signal => post('/api/start', body, { signal }),
    boundedLaunchTimeout(requestTimeoutMs, LAUNCH_SUBMISSION_TIMEOUT_MS),
    'LAUNCH_SUBMISSION_UNKNOWN', { submission: true })

export const getStartStatus = (runId, idempotencyKey, {
  requestTimeoutMs = LAUNCH_STATUS_TIMEOUT_MS,
} = {}) => launchDeadline(
  signal => get(`/api/start/${encodeURIComponent(runId)}/status`, {
    cache: 'no-store', signal, headers: { 'Idempotency-Key': String(idempotencyKey || '') },
  }),
  boundedLaunchTimeout(requestTimeoutMs, LAUNCH_STATUS_TIMEOUT_MS),
  'LAUNCH_STATUS_TIMEOUT')

// ---- assistant (general chat agent — the evolution of Genesis) ----
export const assistantSessions = (options) => get('/api/assistant/sessions', options)
export const assistantCreate = (title = '', mode = 'plan', options) =>
  post('/api/assistant/sessions', { title, mode }, options)
export const assistantGet = (sid, options) =>
  get(`/api/assistant/sessions/${encodeURIComponent(sid)}`, options)
export const assistantDelete = (sid, options) =>
  send(`/api/assistant/sessions/${encodeURIComponent(sid)}`, 'DELETE', undefined, options)
// Standing watches (BACKLOG §F4). The list is the ONLY thing the browser owns here — the watching
// itself is server-side and durable, so a closed tab costs the monitoring nothing and these calls
// are a read plus a stop, never anything the schedule depends on.
export const assistantWatches = (sid, options) =>
  get(`/api/assistant/watches?session=${encodeURIComponent(sid)}`, options)
export const assistantWatchStop = (watchId, options) =>
  send(`/api/assistant/watches/${encodeURIComponent(watchId)}`, 'DELETE', undefined, options)
const validAssistantForkActionId = value => typeof value === 'string'
  && value === value.toLowerCase() && UUID_V4_RE.test(value)
const assistantForkPath = (sid, actionId = null) => {
  const base = `/api/assistant/sessions/${encodeURIComponent(sid)}/fork`
  return actionId == null ? base : `${base}/${encodeURIComponent(actionId)}`
}
const assistantForkIdentity = actionId => {
  const normalized = String(actionId || '').toLowerCase()
  if (!validAssistantForkActionId(normalized)) {
    const error = new Error('Invalid Assistant fork action identity')
    error.code = 'ASSISTANT_FORK_PROTOCOL_ERROR'
    throw error
  }
  return normalized
}
export const assistantFork = (sid, {
  actionId = createIdempotencyKey(), expectedMessages = null,
} = {}, options = {}) => {
  const identity = assistantForkIdentity(actionId)
  if (expectedMessages != null
      && (!Number.isSafeInteger(expectedMessages) || expectedMessages < 0)) {
    const error = new Error('Invalid Assistant fork source version')
    error.code = 'ASSISTANT_FORK_PROTOCOL_ERROR'
    throw error
  }
  return post(assistantForkPath(sid), {
    action_id: identity,
    ...(expectedMessages == null ? {} : { expected_messages: expectedMessages }),
  }, options)
}
export const assistantForkStatus = (sid, actionId, {
  expectedMessages = null, ...options
} = {}) => {
  if (expectedMessages != null
      && (!Number.isSafeInteger(expectedMessages) || expectedMessages < 0)) {
    const error = new Error('Invalid Assistant fork source version')
    error.code = 'ASSISTANT_FORK_PROTOCOL_ERROR'
    throw error
  }
  const query = expectedMessages == null ? '' : `?expected_messages=${expectedMessages}`
  return get(`${assistantForkPath(sid, assistantForkIdentity(actionId))}${query}`,
    { ...options, cache: 'no-store' })
}
// Streaming turn: POST and read the SSE stream, invoking callbacks for token/step/todos/done/error.
// Real token streaming of the final answer (Claude-Desktop feel). Returns the final result dict.
const incompleteAssistantStream = reason => {
  const error = new Error(`Assistant stream ended before a terminal event: ${reason}`)
  error.code = 'assistant_stream_incomplete'
  error.transient = true
  error.ambiguous = true
  error.submissionMayHaveSucceeded = true
  return error
}

const abortedAssistantStream = () => {
  const error = new Error('Assistant stream aborted')
  error.name = 'AbortError'
  return error
}

const ASSISTANT_LIVE_SHARE_ACK_MAX = 4096
const assistantLiveShareAckIds = value => {
  if (!Array.isArray(value) || value.length > ASSISTANT_LIVE_SHARE_ACK_MAX) {
    const error = new Error('Invalid Assistant live-share acknowledgement list')
    error.code = 'assistant_live_share_ack_invalid'
    throw error
  }
  const seen = new Set()
  const ids = []
  for (const id of value) {
    if (typeof id !== 'string' || !/^[0-9a-f]{32}$/.test(id) || seen.has(id)) {
      const error = new Error('Invalid Assistant live-share acknowledgement id')
      error.code = 'assistant_live_share_ack_invalid'
      throw error
    }
    seen.add(id)
    ids.push(id)
  }
  return ids
}

export async function assistantMessageStream(sid, instruction, mode, cbs = {}, signal, display = null,
  acknowledgedLiveShareIds = []) {
  const body = {
    instruction,
    mode,
    acknowledged_live_share_ids: assistantLiveShareAckIds(acknowledgedLiveShareIds),
  }
  if (display != null) body.display = display
  const r = await fetch(apiUrl(`/api/assistant/sessions/${encodeURIComponent(sid)}/message_stream`),
    { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
      // Recovery must send the persisted clean display even when it happens to equal `instruction`:
      // the explicit body is the exact durable-turn contract, not a newly composed send. The live-share
      // acknowledgement is always present (including an empty array) so legacy omission cannot bypass
      // the server's compare-before-stage privacy precondition.
      body: JSON.stringify(body), signal })
  if (!r.ok) { await _throw(r, 'message_stream'); return null }
  if (signal?.aborted) throw abortedAssistantStream()
  // A 2xx only proves that the turn may have been accepted. Without a readable stream there is no
  // terminal receipt, so the caller must reconcile the existing turn instead of inventing success.
  if (!r.body || typeof r.body.getReader !== 'function') {
    throw incompleteAssistantStream('no readable response body')
  }

  let result = null
  let terminal = false
  const parser = createEventStreamParser(({ type: ev, data }) => {
    if (terminal) return
    let parsed
    try { parsed = JSON.parse(data) } catch { parsed = data }
    if (ev === 'token') cbs.onToken?.(parsed)
    else if (ev === 'text') cbs.onText?.(parsed)
    else if (ev === 'step') cbs.onStep?.(parsed)
    else if (ev === 'todos') cbs.onTodos?.(parsed)
    else if (ev === 'error') {
      cbs.onError?.(parsed)
      result = { ok: false, error: parsed }
      terminal = true
    } else if (ev === 'done') {
      result = parsed
      cbs.onDone?.(parsed)
      terminal = true
    }
  })
  const reader = r.body.getReader()
  const dec = new TextDecoder()
  for (;;) {
    let chunk
    try { chunk = await reader.read() }
    catch (error) {
      if (signal?.aborted) throw abortedAssistantStream()
      throw error
    }
    const { done, value } = chunk
    if (done) break
    parser.push(dec.decode(value, { stream: true }))
    if (terminal) {
      try { await reader.cancel() } catch { /* the terminal receipt already owns the outcome */ }
      return result
    }
  }
  if (signal?.aborted) throw abortedAssistantStream()
  parser.push(dec.decode())
  parser.finish()
  if (!terminal) throw incompleteAssistantStream('connection closed')
  return result
}
// Shared query/deadline wiring for bounded trace reads. The optional generation names the run
// whose sidecar bytes the caller is prepared to display.
export const traceGenerationMatches = (payload, expectedGeneration) =>
  !expectedGeneration || payload?.run_generation === expectedGeneration
// `before` is the window's ANCHOR, not a filter: the same `limit` spans ending at that step instead
// of at the node's newest one. It travels here rather than at the call sites because a trace read has
// exactly one spelling of its query, and an anchor a surface forgot to send is a surface silently
// reading the tail while its picker says otherwise.
export const traceReadQuery = (
  expectedGeneration, attempt, limit, before = null, snapshot = null,
) => {
  const query = new URLSearchParams()
  if (attempt != null) query.set('attempt', attempt)
  if (limit) query.set('limit', limit)
  if (before) query.set('before', before)
  // Episode-map pagination pins a backward walk to the newest band from its first page. Other trace
  // routes never pass this fifth argument, so their wire contract remains byte-for-byte unchanged.
  if (snapshot) query.set('snapshot', snapshot)
  if (expectedGeneration) query.set('expected_generation', expectedGeneration)
  return query.size ? `?${query}` : ''
}
export const traceDeadlineGet = (path, expectedGeneration, attempt, limit, timeout, before = null) =>
  deadlineGet(path + traceReadQuery(expectedGeneration, attempt, limit, before), timeout)

// Stop an in-flight assistant turn server-side (survives a page reload, unlike aborting the local
// stream). Also used to poll whether a turn is still running (reattach after switch/reload).
export const assistantCancel = (sid, options) =>
  post(`/api/assistant/sessions/${encodeURIComponent(sid)}/cancel`, {}, options)
export const assistantProgress = (sid, options = {}) => get(
  `/api/assistant/progress?session=${encodeURIComponent(sid)}`, options)

export const assistantCommands = () => get('/api/assistant/commands')
export const assistantRevert = (change, options) => {
  // Both values are opaque receipt material. A legal POSIX path may contain leading/trailing spaces;
  // normalizing either field would turn an exact request into a different request.
  const path = typeof change?.abs_path === 'string' ? change.abs_path : ''
  const recoveryId = typeof change?.recovery_id === 'string' ? change.recovery_id : ''
  const exists = change?.recovery_postimage_exists
  const digest = change?.recovery_postimage_digest
  const mode = change?.recovery_postimage_mode
  if (!path || !recoveryId || typeof exists !== 'boolean'
      || (exists && (typeof digest !== 'string' || !/^[0-9a-f]{64}$/.test(digest)))
      || (exists && (!Number.isInteger(mode) || mode < 0 || mode > 0o7777))
      || (!exists && (digest != null || mode != null))) {
    const error = new Error('This file change has no exact recovery identity.')
    error.code = 'ASSISTANT_REVERT_IDENTITY_REQUIRED'
    throw error
  }
  return post('/api/assistant/revert', {
    path,
    recovery_id: recoveryId,
    expected_postimage: { exists, digest: exists ? digest : null, mode: exists ? mode : null },
  }, options)
}
// A share link is its own capability: the response carries the token-bearing URL, when it expires,
// and whether it follows the chat (`live`) or is frozen at the turns that existed when it was minted.
// The session id is NOT a share link — unshare revokes every link without touching the conversation.
export const assistantShare = (sid, live = false, options) =>
  post(`/api/assistant/sessions/${encodeURIComponent(sid)}/share`, { live }, options)
export const assistantUnshare = (sid, options) =>
  send(`/api/assistant/sessions/${encodeURIComponent(sid)}/share`, 'DELETE', null, options)
// Pending human-in-the-loop confirm requests for a session, and resolving one.
export const assistantPermissions = (sid = null, options = {}) => get(sid == null
  ? '/api/assistant/permissions'
  : `/api/assistant/permissions?session=${encodeURIComponent(sid)}`, options)
export const assistantResolve = (reqId, decision) =>
  post(`/api/assistant/permissions/${encodeURIComponent(reqId)}`, { decision })
export const attentionFeed = (limit = 200, cursor = null, options = {}) => get(
  `/api/attention?limit=${encodeURIComponent(limit)}`
  + (cursor == null ? '' : `&cursor=${encodeURIComponent(cursor)}`),
  options,
)
