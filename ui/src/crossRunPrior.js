import { fmt } from './util.js'

const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const count = value => Number.isSafeInteger(value) && value >= 0
const bool = value => typeof value === 'boolean'
const metric = value => typeof value === 'number' && Number.isFinite(value) ? value : null
const array = value => Array.isArray(value) ? value : []
const labels = value => array(value).slice(0, 64).filter(item => typeof item === 'string')
  .map(item => item.trim().slice(0, 80)).filter(Boolean)
const uniqueLabels = value => Array.isArray(value) && value.length > 0 && value.length <= 64
  && new Set(value).size === value.length

export function crossRunPriorNarration(data) {
  const rawRuns = array(data?.prior_runs)
  const runs = rawRuns.slice(0, 8).filter(record)
  const rawMatched = array(data?.matched_concepts)
  const matched = labels(rawMatched)
  const first = runs[0]
  const v2 = data?.v === 2
  const source = record(data?.concept_source) ? data.concept_source : null
  const sourceKnown = v2 && source && bool(source.source_complete)
    && ['partial_capsules', 'source_unknown_capsules', 'source_concepts_omitted',
      'source_outcomes_omitted'].every(key => count(source[key]))
    && source.source_unknown_capsules <= source.partial_capsules
    && (source.partial_capsules > 0
      || source.source_concepts_omitted + source.source_outcomes_omitted === 0)
    && source.source_complete === (source.partial_capsules === 0)
  const listKnown = v2 && uniqueLabels(rawMatched) && matched.length === rawMatched.length
    && count(data?.prior_runs_total) && count(data?.prior_runs_omitted)
    && rawRuns.length > 0 && rawRuns.length <= 8 && data.prior_runs_total > 0
    && data.prior_runs_total === rawRuns.length + data.prior_runs_omitted
    && bool(data.prior_runs_complete)
    && data.prior_runs_complete === (data.prior_runs_omitted === 0)
    && runs.length === rawRuns.length
  const receiptPart = (receipt, name) => count(receipt?.[`${name}_total`])
    && count(receipt[`${name}_omitted`]) && receipt[`${name}_omitted`] <= receipt[`${name}_total`]
    && bool(receipt[`${name}_complete`])
    && receipt[`${name}_complete`] === (receipt[`${name}_omitted`] === 0)
  const runReceiptKnown = run => {
    const concepts = run?.matched_concepts
    const rows = run?.matched_concept_outcomes
    if (!uniqueLabels(concepts) || !Array.isArray(rows) || rows.length !== concepts.length
        || !rows.every(row => record(row) && concepts.includes(row.concept)
          && typeof row.concept === 'string' && bool(row.outcome_retained))
        || new Set(rows.map(row => row.concept)).size !== rows.length) return false
    const receipt = run.source_receipt
    return record(receipt) && receiptPart(receipt, 'concepts')
      && receiptPart(receipt, 'concept_outcomes') && receipt.concepts_total >= concepts.length
      && receipt.concept_outcomes_total >= rows.filter(row => row.outcome_retained).length
  }
  const receiptsKnown = sourceKnown && listKnown && runs.every(run => runReceiptKnown(run)
    && (!source.source_complete
      || (run.source_receipt.concepts_complete && run.source_receipt.concept_outcomes_complete)))

  let history = 'prior match recorded; run details unavailable'
  if (listKnown) history = `tried in ${data.prior_runs_total} earlier run${data.prior_runs_total === 1 ? '' : 's'}`
    + (data.prior_runs_omitted ? ` (${data.prior_runs_omitted} run records omitted)` : '')
  else if (runs.length) history = `found in ${runs.length} retained run record${runs.length === 1 ? '' : 's'}`

  const matchedOutcome = listKnown && first && runReceiptKnown(first)
    ? first.matched_concept_outcomes.find(row => row.outcome_retained
      && metric(row.outcome) !== null && rawMatched.includes(row.concept)) : null
  const concept = labels([matchedOutcome?.concept])[0]
  const runBest = metric(first?.run_best_metric) ?? metric(first?.best_metric)
  const outcome = matchedOutcome ? `; closest run matched outcome ${concept}=${fmt(matchedOutcome.outcome)}`
    : runBest !== null ? `; closest run best ${fmt(runBest)}` : ''
  let warning = receiptsKnown ? '' : ' · evidence completeness unknown'
  if (receiptsKnown && !source.source_complete) warning = source.source_unknown_capsules
    ? ' · PARTIAL source (some capsule receipts unknown)' : ' · PARTIAL source'
  if (receiptsKnown && !data.prior_runs_complete) warning += ' · PARTIAL run list'
  return `cross-run prior${matched.length ? ': ' + matched.slice(0, 3).join(', ') : ''} — ${history}${outcome}${warning}`
}
