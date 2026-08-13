// What the operator reads before agreeing to delete a run's cross-run memory along with the run.
//
// The server decides WHAT is attributable to one run (`serve/memory_cascade.py`); this decides how
// that answer is stated. The distinction the wording has to carry is the whole feature:
//
//   * rows this run alone owns  -> these GO
//   * rows that merged evidence from runs that still exist -> these STAY, and the reason is named
//
// A dialog that showed only the first number would be asking for consent to a deletion whose real
// extent the operator cannot see, and a dialog that showed neither would be a checkbox that does
// something unknowable.

/** The store labels, in the order the dialog lists them, from a server survey. */
export function cascadeStores(report) {
  const stores = Array.isArray(report?.stores) ? report.stores : []
  return stores
    .filter(store => (store?.deletable | 0) > 0 || (store?.kept | 0) > 0)
    .map(store => ({
      store: String(store.store || store.file || 'memory'),
      deletable: store.deletable | 0,
      kept: store.kept | 0,
      reasons: (Array.isArray(store.reasons) ? store.reasons : [])
        .map(entry => ({ reason: String(entry?.reason || ''), rows: entry?.rows | 0 }))
        .filter(entry => entry.reason && entry.rows > 0),
    }))
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

/** The single line beside the checkbox: what it will delete, and what it will not. */
export function cascadeLabel(report) {
  if (!report) return 'Also delete this run’s own cross-run memory'
  if (report.available === false) {
    return 'Also delete this run’s own cross-run memory (no memory store is configured)'
  }
  const deletable = report.deletable | 0
  if (!deletable && !(report.kept | 0)) {
    return 'Also delete this run’s own cross-run memory (this run contributed none)'
  }
  return `Also delete this run’s own cross-run memory (${plural(deletable, 'row')})`
}

/**
 * The sentence that keeps the checkbox honest: what survives, and why.
 *
 * Empty when nothing is being held back — a reassurance nobody needed is noise, and noise beside a
 * destructive control is worse than noise anywhere else.
 */
export function cascadeKeptNotice(report) {
  const kept = report?.kept | 0
  if (!kept) return ''
  const reasons = cascadeStores(report).flatMap(store => store.reasons)
  const distinct = [...new Set(reasons.map(entry => entry.reason))]
  const because = distinct.length === 1 ? distinct[0]
    : 'they carry evidence or concepts shared with runs that still exist'
  return `${plural(kept, 'row')} stay: ${because}.`
}

/**
 * The batch has no survey behind it, on purpose: previewing N runs means N scans of every store
 * before the operator has agreed to anything, and the numbers would be stale by the second
 * deletion anyway. So the batch checkbox states the RULE instead of a count — which is the part
 * that governs what happens, and the part a count was only ever standing in for.
 */
export function bulkCascadeLabel(runCount = 0) {
  return `Also delete each run’s own cross-run memory — rows that merge evidence or concepts with `
    + `runs that still exist are kept${runCount ? ` (${runCount} run${runCount === 1 ? '' : 's'})` : ''}`
}

/**
 * The result after a cascade ran, from the deletion receipt's `memory` block.
 *
 * Returns `{kind, text, retryRunId}`. `retryRunId` is the whole reason this is a record and not a
 * string: when a store could not be rewritten the run is ALREADY gone, so its card — and with it
 * every affordance that could finish the job — has left the list. Without an explicit retry handle
 * the operator is told about a half-finished purge they have no way to complete.
 */
export function retryableCascadeIdentity(memory) {
  // The retry endpoint refuses a body with NEITHER `run_uid` nor `memory_dir`
  // (`memory_purge_identity_required`), because with both empty the purge would fall back to
  // matching a run's bare directory NAME — which the next run reuses. So a button offered with an
  // empty identity cannot ever succeed: every press 400s and the catch re-offers the same empty
  // identity, forever. Return null when there is nothing to retry WITH, so the caller can say so
  // instead of rendering a dead control.
  const run_uid = String(memory?.run_uid || '')
  const memory_dir = String(memory?.memory_dir || '')
  return run_uid || memory_dir ? { run_uid, memory_dir } : null
}

export function cascadeOutcome(memory, runId = '') {
  if (!memory) return null
  const deleted = memory.deleted | 0
  if (memory.ok === false) {
    const retryable = retryableCascadeIdentity(memory)
    const failed = Array.isArray(memory.failures) ? memory.failures : []
    const where = failed.map(entry => String(entry?.store || '')).filter(Boolean).join(', ')
    return {
      kind: 'error',
      // Never "deleted N rows" alone when part of it failed: this notice is the only place the
      // operator can learn that a store still holds this run's rows.
      text: 'The run was deleted. Its cross-run memory was only partly removed'
        + (where ? ` (${where} could not be rewritten).` : '.')
        + (retryable ? '' : ' This run\u2019s identity was not recorded, so the purge cannot be '
          + 'finished from here.'),
      // The retry's IDENTITY, carried beside the handle. The run is already gone, so the server
      // cannot read this back and refuses to guess it; the receipt is the only place it still
      // exists. `memory_dir` matters as much as `run_uid`: it is a per-RUN setting, so a retry that
      // omitted it opened the server's CURRENT global store, matched nothing, and reported success.
      // With NEITHER, there is no retry to offer — see `retryableCascadeIdentity`.
      retryRunId: retryable ? String(runId || memory.run_id || '') : '',
      retryIdentity: retryable || { run_uid: '', memory_dir: '' },
    }
  }
  if (!deleted) {
    return { kind: 'status', retryRunId: '',
      text: 'The run was deleted. It had contributed no cross-run memory of its own.' }
  }
  return { kind: 'status', retryRunId: '',
    text: `The run was deleted, along with ${plural(deleted, 'cross-run memory row')} `
      + 'only it owned.' }
}
