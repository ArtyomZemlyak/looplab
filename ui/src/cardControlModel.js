const seq = value => Number.isSafeInteger(value) && value >= 0 ? value : null

export function cardEditReflected(card, patch, baseline, expectedEventSeq) {
  const foldedEventSeq = seq(card?.statement_edit_seq)
  const expected = seq(expectedEventSeq)
  if (expected != null && foldedEventSeq != null && foldedEventSeq >= expected) return true
  if (typeof card?.statement !== 'string' || typeof patch?.statement !== 'string') return false
  if (card.statement === patch.statement) return true
  return typeof baseline === 'string'
    && card.statement.length > 0 && patch.statement.startsWith(card.statement)
    && !baseline.startsWith(card.statement)
}

export function cardControlSubmission(current, id, kind, patch, editBaseline, saving) {
  const entry = current[id] || {}
  return { ...current, [id]: {
    ...entry,
    editEventSeq: kind === 'edit' ? null : entry.editEventSeq,
    updates: { ...(entry.updates || {}), [kind]: patch },
    editBaseline: editBaseline ?? entry.editBaseline,
    pending: { kind, phase: 'submitting' },
    notice: { tone: 'pending', text: saving },
  } }
}

// The RECOVERY verdict for a card control the operator asked about again — "is my pause/edit/drop
// actually done, and may I retry it?" — read off the command record's status.
//
// A model rather than two inline `includes` lists in the component's setState updater, because the
// two lists are NOT the same list and the difference is the whole decision. `rejected` is terminal
// and must never offer a retry (the server refused the command; re-sending it refuses again), while
// `failed`/`timed_out` are exactly the states a retry can resolve. Swapping them, or dropping
// `rejected` from `FAILED`, is a one-token edit that turns a settled refusal into an infinite retry
// button — and it shipped green, because only a jsdom mount of the whole board could observe it.
const RECOVERY_FAILED = ['failed', 'timed_out', 'rejected']
const RECOVERY_RETRYABLE = ['failed', 'timed_out']

export function cardControlRecovery(record) {
  const status = record?.status
  const failed = RECOVERY_FAILED.includes(status)
  const retryable = RECOVERY_RETRYABLE.includes(status)
  return {
    failed,
    retryable,
    // `retryable` outranks `failed` because it is the more specific answer: a retryable command is
    // a failed one the operator can still act on, and the phase names the affordance.
    phase: retryable ? 'retryable' : failed ? 'failed' : 'waiting-for-fold',
    tone: failed ? 'error' : 'pending',
  }
}
