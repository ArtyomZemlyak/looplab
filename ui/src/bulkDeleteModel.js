// Deleting many runs at once — which is a QUEUE of the existing one-run transaction, not a new one.
//
// Run deletion is an operation-bound durable transaction: an idempotency key, a generation+seq fence
// read from the exact row the operator inspected, a receipt, and a recovery record that survives the
// tab. None of that can be skipped for a batch, and a bulk endpoint that took a list of ids would
// have to reinvent every part of it. So the batch is a SEQUENCE of single deletions, and this module
// owns the two things a sequence needs and a single deletion did not:
//
//   * a PLAN — which of the selected runs can actually be deleted, and why the rest cannot, computed
//     before anything is submitted so the operator agrees to a real list rather than a count;
//   * a RUNNING TALLY that stays true when the batch stops early, because "8 of 20 deleted, stopped
//     at run-9" is the only honest thing to say and "deletion failed" is not.
//
// Sequential, never parallel: each deletion's receipt is validated against a REFRESHED run list, and
// concurrent deletions would race that refresh — one deletion's confirmation read would observe
// another's half-applied state. The cost is wall-clock; the alternative is a confirmation that means
// nothing.
import { retryableCascadeIdentity } from './memoryCascadeModel.js'
import { RUN_GENERATION_RE } from './panelPrimitives.js'

/** Runs are deleted oldest-selection-first; a batch that stops early has then done the ones the
 *  operator has been looking at longest. Nothing depends on it, but an arbitrary order would make
 *  "stopped at run-9" impossible to predict. */
export function bulkDeletionPlan(selectedIds = [], runs = [], recoveries = new Map()) {
  const byId = new Map((Array.isArray(runs) ? runs : []).map(run => [run?.run_id, run]))
  const ready = []
  const blocked = []
  for (const id of Array.isArray(selectedIds) ? selectedIds : []) {
    const runId = String(id || '')
    if (!runId) continue
    const run = byId.get(runId)
    if (!run) {
      blocked.push({ runId, reason: 'not in the current run list' })
      continue
    }
    if (recoveries?.has?.(runId)) {
      // An unresolved deletion owns this run already. Submitting a SECOND operation against it is
      // exactly what the durable record exists to prevent.
      blocked.push({ runId, reason: 'an unfinished deletion already owns it' })
      continue
    }
    const generation = String(run.deletion_generation || run.generation || '')
    const seq = run.seq
    if (!RUN_GENERATION_RE.test(generation)
        || !Number.isSafeInteger(seq) || seq < -1) {
      blocked.push({ runId, reason: 'its exact deletion identity is unavailable' })
      continue
    }
    ready.push({ runId, label: String(run.label || ''), expectedGeneration: generation,
      expectedSeq: seq })
  }
  return { ready, blocked }
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/** The line on the confirm button and in the dialog header. */
export function bulkDeletionSummary(plan) {
  const ready = plan?.ready?.length | 0
  const blocked = plan?.blocked?.length | 0
  if (!ready && !blocked) return ''
  if (!ready) return `None of the ${plural(blocked, 'selected run')} can be deleted right now.`
  return `Delete ${plural(ready, 'run')}`
    + (blocked ? `; ${blocked} cannot be deleted right now.` : '.')
}

/** Live progress while the queue drains. Names the run in flight — a bare count during a slow
 *  deletion is indistinguishable from a stall. */
export function bulkProgressLabel(state) {
  if (!state || !state.running) return ''
  const at = (state.done?.length | 0) + 1
  const total = state.total | 0
  return `Deleting ${at} of ${total}${state.current ? ` — ${state.current}` : ''}…`
}

/** How many run ids a notice spells out before it starts counting. The dialog lists the whole plan;
 *  this sentence also travels OUTSIDE the dialog, where "3 runs deleted" and "which three" are
 *  different answers and only one of them tells the operator what to do next. */
const NAMED_RUN_CAP = 5

const namedRuns = ids => {
  const shown = ids.slice(0, NAMED_RUN_CAP).map(id => `“${id}”`).join(', ')
  const rest = ids.length - Math.min(ids.length, NAMED_RUN_CAP)
  return rest ? `${shown} and ${rest} more` : shown
}

/**
 * The result. Four outcomes, kept apart on purpose:
 *   everything went   -> a plain confirmation, NAMING the runs
 *   nothing went      -> the reason, with the run it stopped on
 *   some went         -> BOTH, because a batch that half-succeeded and reports only its failure
 *                        leaves the operator believing runs still exist that do not.
 *   nothing CONFIRMED -> neither of the two above. The run that stopped the batch may itself have
 *                        been deleted: `unknown` is what the transaction returns when the receipt
 *                        said `succeeded` and the tab could not finish reading the refreshed list,
 *                        or could not clear the recovery record. "Nothing was deleted" is a claim
 *                        about the filesystem and this branch has no evidence for it.
 *
 * STOPPED AND NOTHING-DELETED ARE DIFFERENT FACTS. Until 2026-08-14 they read as one, because
 * `runDeletionRequest` never returned its verdict: `done` stayed empty for every batch, so the
 * partial branch below was unreachable and every stop — including one that followed a completed
 * deletion — printed "Nothing was deleted." The operator was told nothing had happened about a run
 * whose receipt read `phase: succeeded` and whose directory was already gone.
 */
export function bulkOutcomeNotice(state) {
  if (!state) return null
  const done = state.done?.length | 0
  const blocked = state.blocked?.length | 0
  const stopped = state.stoppedAt
  const tail = blocked ? ` ${plural(blocked, 'selected run')} could not be deleted.` : ''
  // A half-purged memory store outranks everything else here. The runs are gone, their cards have
  // left the list, and this notice is the only surface left that can carry a retry — so it takes
  // the first unfinished purge's run id, and `retryRunId` is what renders the button.
  const unfinished = (state.memoryFailures || [])[0]
  // The identity gate is shared with `cascadeOutcome`, so the REASON has to be shared too: refusing
  // the dead button in both places while only one of them says why leaves the batch notice telling
  // the operator a store still holds a run's rows, with neither an affordance nor an explanation.
  const retryableMemory = retryableCascadeIdentity(unfinished?.memory)
  const memoryTail = unfinished
    ? ` Cross-run memory was only partly removed for ${plural(state.memoryFailures.length, 'run')}`
      + ` (first: “${unfinished.runId}”).`
      + (retryableMemory ? '' : ' That run\u2019s identity was not recorded, so the purge cannot be'
        + ' finished from here.')
    : ''
  // The identity travels with the handle, for the same reason `cascadeOutcome` carries one: the run
  // is already deleted, so the server cannot read `run_uid`/`memory_dir` back and refuses to guess
  // them. A button wired to the run id alone cannot finish the purge it offers — and one wired to
  // an EMPTY identity cannot either: the server 400s `memory_purge_identity_required`, the catch
  // re-offers the same empty identity, and the operator presses it forever. The shared gate is
  // `retryableCascadeIdentity`, so both notices refuse the dead button by the same rule.
  // Both keys are ALWAYS present so a consumer reads one shape; what the gate decides is the
  // HANDLE, which is what renders the button.
  const retryable = retryableMemory
  const retryIdentity = retryable || { run_uid: '', memory_dir: '' }
  const retryRunId = unfinished && retryable ? String(unfinished.runId || '') : ''
  const deleted = (state.done || []).map(id => String(id || ''))
  if (!stopped) {
    if (!done) return blocked ? { kind: 'error', retryRunId: '', text: tail.trim() } : null
    return { kind: unfinished ? 'error' : 'status', retryRunId,
      retryIdentity,
      text: `${plural(done, 'run')} permanently deleted: ${namedRuns(deleted)}.${tail}${memoryTail}` }
  }
  const why = String(stopped.reason || 'the deletion did not complete')
  // The stopping run's OWN outcome. `unknown` is the one value that forbids a claim about it in
  // either direction — see the block comment above.
  const unsettled = stopped.outcome === 'unknown'
  if (!done) {
    const opening = unsettled
      ? `No deletion is confirmed. “${stopped.runId}” stopped the batch: ${why}.`
        + ` Its own outcome is not established — check that run before assuming it still exists.`
      : `Nothing was deleted. “${stopped.runId}” stopped the batch: ${why}.`
    return { kind: 'error', retryRunId, retryIdentity, text: `${opening}${tail}${memoryTail}` }
  }
  const untouched = Math.max(0, (state.total | 0) - done - 1)
  return { kind: 'error', retryRunId, retryIdentity, text:
    `${plural(done, 'run')} deleted (${namedRuns(deleted)}), then the batch stopped at `
    + `“${stopped.runId}”: ${why}.`
    + (unsettled ? ' Its own outcome is not established — check that run before assuming it still'
      + ' exists.' : '')
    + (untouched ? ` The remaining ${plural(untouched, 'run')} were not touched.` : '')
    + `${tail}${memoryTail}` }
}
