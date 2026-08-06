// The RULES of the trace-clear recovery (doc 25 UI-09), lifted out of `Inspector.jsx::Trace`. Trace
// was a ~510-line function in which view toggling, the confirm/busy/verify/blocked recovery phases
// and the whole render lived in one closure; `useTraceClear.js` is the React half and this is the
// pure one, the same split `resourceModel.js` / `useScopedResource` already uses.
//
// The reason THIS is the half worth stating is that trace clear is a destructive operation whose
// failure classification decides what the operator is told about a DELETE that may already have
// landed. "Nothing was cleared" and "outcome unknown, verify the same operation" are different
// claims, and only one of them is safe to make wrongly. That decision was ~75 lines of nested
// ternaries inside an async catch block reachable only by mounting the Inspector with a stubbed
// `clearNodeTrace` — so no test stated it, and a wrong branch would read as a wording change.

// A response that proves the handler refused BEFORE mutating anything. Everything else — a timeout,
// a 5xx, a dropped connection, an error with no status at all — leaves the outcome unknown, because
// the request may have reached the server and deleted the trace before the answer was lost.
export const TRACE_CLEAR_DEFINITELY_NOT_MUTATED_CODES = Object.freeze([
  'run_generation_changed', 'node_generation_changed', 'trace_revision_changed',
  'run_generation_unavailable', 'node_generation_unavailable', 'trace_revision_unavailable',
  'trace_clear_operation_unavailable', 'trace_clear_operation_conflict',
  'engine_running', 'engine_lock_unavailable', 'run_lifecycle_lock_unavailable',
  'STALE_LINK_READ_ONLY',
])

// A 4xx is pre-mutation EXCEPT for the three "try again" statuses, which a proxy can also synthesize
// after the request was already forwarded.
//
// These are the same three numbers as `commandModel.js::TRANSIENT_HTTP`, and they are deliberately
// NOT the same constant. That one answers "may this command READ be retried"; this one answers "can
// the server prove it did not already delete the trace". Sharing the list would let a change made for
// the command lifecycle silently move a destructive operation's safety verdict between "Nothing was
// cleared" and "outcome unknown" — the one distinction this whole module exists to keep honest.
export const TRACE_CLEAR_RETRYABLE_STATUSES = Object.freeze([408, 425, 429])

// Identity codes that end a VERIFY: the lifecycle moved on, so no matching pending operation can
// still be resolved and re-verifying the same one forever would be the only alternative.
const VERIFICATION_IDENTITY_CODES = Object.freeze([
  'run_generation_changed',
  'node_generation_changed',
  'trace_revision_changed',
])

const TRACE_CLEAR_OPERATION_ID_RE = /^tc_[0-9a-f]{32}$/

// The recovery record a remount restores, rendered as the notice the operator sees first. A `busy`
// record means a POST left this tab and its answer did not come back; the other phases are already
// carrying their own message, except `confirm` (a two-click confirmation is not a durable intent, so
// it is cancelled) and an unrecognised phase (fail closed and demand a refresh).
export function initialTraceClearMessage(recovery) {
  if (!recovery) return null
  if (recovery.phase === 'busy') {
    return {
      kind: 'status',
      blocking: true,
      pending: true,
      verifyOperation: recovery.mode === 'verify',
      text: recovery.mode === 'verify'
        ? 'Checking whether the original trace clear completed…'
        : 'Trace clear is still pending. This experiment will stay locked until the request settles.',
    }
  }
  if (['blocked', 'reconcile', 'ambiguous'].includes(recovery.phase)) return recovery.message
  if (recovery.phase === 'confirm') {
    return {
      kind: 'success',
      blocking: false,
      text: 'Trace clear confirmation was cancelled because this view changed. Nothing was submitted.',
    }
  }
  return {
    kind: 'error',
    blocking: true,
    text: 'Trace clear state could not be recovered. Refresh this experiment before clearing.',
  }
}

// The whole failure decision: what the operator is told, and whether the exact operation must be
// kept so it can be verified rather than re-submitted. Returns `{ message, recovery }` — `recovery`
// is the durable `ambiguous` record when the outcome is unknown, and null when the server proved
// nothing was deleted.
export function classifyTraceClearFailure(e, { verifying = false, operation = null,
  nodeId = null } = {}) {
  // Keep server free text out of the UI. Generation conflicts mean the exact confirmation scope
  // disappeared and are deliberately distinct from the ordinary "run is active" 409.
  const currentNodeGeneration = Number.isSafeInteger(e?.detail?.current_node_generation)
    ? e.detail.current_node_generation : null
  const definitelyNotMutated = TRACE_CLEAR_DEFINITELY_NOT_MUTATED_CODES.includes(e?.code)
    || (e?.status >= 400 && e.status < 500
      && !TRACE_CLEAR_RETRYABLE_STATUSES.includes(e.status))
  const verificationIdentityChanged = verifying && VERIFICATION_IDENTITY_CODES.includes(e?.code)
  const verificationTerminal = verificationIdentityChanged || (verifying && [
    'trace_clear_operation_superseded',
    'trace_clear_operation_conflict',
  ].includes(e?.code))
  // Once the first request may have escaped, a Verify response is contextual: even a normally
  // pre-mutation 4xx/503 cannot prove what the earlier handler did. Keep the exact operation
  // until the server returns success or a durable terminal receipt.
  const verificationUnresolved = verifying && !verificationTerminal
  const outcomeUnknown = verificationUnresolved || (!definitelyNotMutated
    && (e?.status == null || TRACE_CLEAR_RETRYABLE_STATUSES.includes(e.status) || e.status >= 500)
  )
  const pendingOperationId = e?.code === 'trace_clear_pending'
    && TRACE_CLEAR_OPERATION_ID_RE.test(e?.detail?.operation_id || '')
    ? e.detail.operation_id : null
  const recoveryOperation = pendingOperationId
    ? { ...operation, operationId: pendingOperationId }
    : operation
  const text = verificationUnresolved
    ? e?.code === 'trace_clear_pending'
      ? 'The original trace clear is still being resolved. Check this same operation again; do not submit a new clear.'
      : 'The original trace clear could not be verified yet. Its outcome may already have changed the trace; check this same operation again.'
    : verificationIdentityChanged
      ? 'The trace lifecycle changed and no matching pending clear operation remains. No additional deletion was attempted. Refresh the current experiment before deciding what to do next.'
    : e?.code === 'run_generation_changed' || e?.code === 'STALE_LINK_READ_ONLY'
    ? 'The run changed before the trace was cleared. Nothing was cleared. Review the current run and confirm again.'
      : e?.code === 'trace_clear_operation_superseded'
        ? 'The trace changed after an interrupted clear, so the original outcome cannot be reconstructed. No additional deletion was attempted. Refresh the current experiment before deciding what to do next.'
      : e?.code === 'trace_path_invalid'
        ? 'Trace storage is not a valid run-owned file. Nothing was cleared. Restore the trace file, then refresh this experiment.'
      : e?.code === 'trace_clear_operation_conflict'
        ? 'This clear operation belongs to a different trace lifecycle. Nothing new was cleared. Refresh this experiment before trying again.'
      : e?.code === 'node_generation_changed'
      ? currentNodeGeneration == null
        ? `Experiment #${nodeId} changed attempts. Nothing was cleared. Review the current attempt and confirm again.`
        : `Experiment #${nodeId} moved to attempt ${currentNodeGeneration}. Nothing was cleared. Review the current attempt and confirm again.`
      : e?.code === 'trace_revision_changed'
        ? 'Trace diagnostics changed before the clear acquired ownership. Nothing was cleared. Refresh this experiment and confirm against the current trace.'
        : ['run_generation_unavailable', 'node_generation_unavailable',
            'trace_revision_unavailable',
            'trace_clear_operation_unavailable'].includes(e?.code)
            || e?.status === 428
          ? 'The exact run, trace snapshot, or experiment attempt is unavailable. Nothing was cleared. Reload the run and confirm again.'
          : e?.code === 'trace_clear_pending'
            ? 'The original trace clear is still being resolved. Verify this same operation again; do not submit a new clear.'
          : e?.status === 409
            ? 'Trace wasn’t cleared because the run is active, busy, or its write ownership could not be verified. Wait for current work to settle, refresh, then try again.'
            : outcomeUnknown
              ? 'Trace clear outcome is unknown. Verify this same operation before trying again.'
              : 'Trace clear was rejected. Nothing was confirmed as deleted. Refresh this experiment before trying again.'
  const message = {
    kind: 'error',
    blocking: true,
    verifyOperation: outcomeUnknown,
    text,
  }
  return {
    message,
    recovery: outcomeUnknown
      ? { phase: 'ambiguous', message, operation: recoveryOperation }
      : null,
  }
}

// Whether the clear control may be offered at all, and — when it may not — the one sentence that
// says why. The order of the branches is the order of the causes an operator can act on: an
// incomplete node first, then a run that still owns its own trace, then the refreshes in flight.
export function traceClearAvailability({ nodeStatus, nodeGeneration, expectedGeneration,
  expectedTraceRevision, detailStatus, reloadPending, unavailable, live } = {}) {
  const scopeReady = /^[0-9a-f]{64}$/.test(expectedGeneration || '')
    && /^[0-9a-f]{64}$/.test(expectedTraceRevision || '')
    && nodeGeneration != null && nodeStatus !== 'building'
    && detailStatus === 'ready' && !reloadPending && !unavailable
  const runWritingTrace = live?.engine_running === true
  const runTraceOwnershipKnown = live?.engine_running === false
  const unavailableText = nodeStatus === 'building'
    ? 'Experiment creation is incomplete; trace clear is unavailable.'
    : runWritingTrace
    ? 'The run is active; trace clear is unavailable until it stops.'
    : !runTraceOwnershipKnown
      ? 'Run write ownership is being verified; trace clear is unavailable.'
    : reloadPending
    ? 'Experiment details are refreshing; clear is unavailable.'
    : detailStatus === 'loading'
    ? 'Experiment details are loading; clear is unavailable.'
    : detailStatus !== 'ready'
      ? 'Experiment details must refresh successfully before trace can be cleared.'
      : unavailable
        ? 'Trace diagnostics could not be loaded; clear is unavailable until a refresh succeeds.'
      : 'Trace identity is loading; clear is unavailable.'
  return { available: scopeReady && runTraceOwnershipKnown, unavailableText }
}
