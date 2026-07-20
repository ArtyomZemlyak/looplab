import { normalizeConceptId } from './conceptId.js'

const canonical = value => {
  if (!Array.isArray(value) || value.length === 0 || value.length > 64) return []
  return new Set(value).size === value.length
    && value.every(item => normalizeConceptId(item) === item) ? value : []
}

export function crossRunPriorNarration(data) {
  const matched = canonical(data?.matched_concepts)
  const runCount = Array.isArray(data?.prior_runs)
    ? data.prior_runs.slice(0, 8).filter(run => run && typeof run === 'object' && !Array.isArray(run)).length
    : 0
  const labels = matched.slice(0, 3).join(', ').slice(0, 240)
  // # CODEX AGENT: the live event feed is a bounded audit preview, not an evidence authority. It never
  // turns a run-wide best into a concept outcome or claims coverage from independently retained rows.
  const history = runCount
    ? `${runCount} retained run${runCount === 1 ? '' : 's'}`
    : 'match recorded'
  return `cross-run prior${labels ? ': ' + labels : ''} — ${history} · evidence completeness unknown`
}
