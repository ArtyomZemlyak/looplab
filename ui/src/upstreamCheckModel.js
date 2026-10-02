// A bounded folded history reports recorded work, never current CAS authority.
export function upstreamCheckSummary(history) {
  const rows = Array.isArray(history) ? history : []
  const unknown = { status: 'unknown' }
  if (rows.some(row => !row || typeof row !== 'object')) return unknown
  const starts = rows.filter(row => row.type === 'upstream_gate_started')
  const finishes = rows.filter(row => row.type === 'upstream_gate_finished')
  if (!starts.length && !finishes.length) return null
  const start = starts.at(-1)
  if (!start || !['action_id', 'proposal_id', 'request_hash', 'input_identity']
      .every(key => typeof start[key] === 'string' && start[key])
      || !Number.isSafeInteger(start.seq) || start.seq < 0
      || starts.filter(row => row.action_id === start.action_id).length !== 1) return unknown
  // A newer completion whose start fell out of (or is missing from) this view
  // cannot make the preceding, fully retained passing claim look current.
  if (finishes.some(row => row.seq > start.seq
      && !starts.some(claim => claim.action_id === row.action_id && claim.seq < row.seq))) return unknown
  if (rows.some(row => row.type === 'upstream_gate_abandoned'
      && row.claim_action_id === start.action_id)) return { status: 'abandoned' }
  const completed = finishes.filter(row => row.action_id === start.action_id)
  if (!completed.length) return { status: 'unfinished' }
  const finish = completed[0]
  if (completed.length !== 1 || !Number.isSafeInteger(finish.seq) || finish.seq <= start.seq
      || finish.proposal_id !== start.proposal_id || finish.request_hash !== start.request_hash
      || finish.result?.input_identity !== start.input_identity
      || typeof finish.result?.passed !== 'boolean') return unknown
  const result = finish.result
  return { status: result.passed ? 'passed' : 'failed',
    executions: Array.isArray(result.executions) ? result.executions.length : null,
    seconds: Number.isFinite(result.eval_seconds)
      && result.eval_seconds >= 0 ? result.eval_seconds : null }
}
