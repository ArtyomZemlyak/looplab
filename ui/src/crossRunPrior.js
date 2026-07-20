import { fmt } from './util.js'
import { normalizeConceptId } from './conceptId.js'

const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const count = value => Number.isSafeInteger(value) && value >= 0
const bool = value => typeof value === 'boolean'
const metric = value => typeof value === 'number' && Number.isFinite(value) ? value : null
const array = value => Array.isArray(value) ? value : []
const labels = value => array(value).slice(0, 64).filter(item => typeof item === 'string')
  .map(item => item.trim().slice(0, 80)).filter(Boolean)
// CODEX AGENT: v2 receipt identities are producer-canonical concept ids. Validate with the same
// grammar as replay/UI concepts so whitespace/case aliases and bidi/control garbage cannot form a
// second "unique" match or be echoed from a corrupted event log.
const canonicalLabels = value => {
  if (!Array.isArray(value) || value.length === 0 || value.length > 64) return null
  const normalized = value.map(normalizeConceptId)
  return normalized.every((label, index) => label && label === value[index])
    && new Set(normalized).size === normalized.length ? normalized : null
}

export function crossRunPriorNarration(data) {
  const rawRuns = array(data?.prior_runs)
  const runs = rawRuns.slice(0, 8).filter(record)
  const rawMatched = array(data?.matched_concepts)
  const canonicalMatched = canonicalLabels(rawMatched)
  const matched = labels(canonicalMatched)
  const first = runs[0]
  const v2 = data?.v === 2
  const source = record(data?.concept_source) ? data.concept_source : null
  const storeReceiptKeys = ['source_rows_total', 'source_rows_quarantined',
    'source_malformed_rows', 'source_invalid_capsule_rows', 'source_duplicate_run_rows']
  const storeReceiptFields = source
    ? ['source_store_complete', ...storeReceiptKeys].filter(key => Object.hasOwn(source, key)).length
    : 0
  const hasStoreReceipt = storeReceiptFields > 0
  // CODEX AGENT: the additive store-health receipt is optional for historical v2 events, but atomic for
  // new events. Never let a partial/malformed extension launder quarantined rows into exact evidence.
  const storeKnown = !hasStoreReceipt || (storeReceiptFields === storeReceiptKeys.length + 1
    && bool(source.source_store_complete) && storeReceiptKeys.every(key => count(source[key]))
    && source.source_rows_quarantined <= source.source_rows_total
    && source.source_rows_quarantined === source.source_malformed_rows
      + source.source_invalid_capsule_rows + source.source_duplicate_run_rows
    && source.source_store_complete === (source.source_rows_quarantined === 0))
  const sourceKnown = v2 && source && bool(source.source_complete)
    && ['partial_capsules', 'source_unknown_capsules', 'source_concepts_omitted',
      'source_outcomes_omitted'].every(key => count(source[key]))
    && source.source_unknown_capsules <= source.partial_capsules
    && (source.partial_capsules > 0
      || source.source_concepts_omitted + source.source_outcomes_omitted === 0)
    && storeKnown
    && source.source_complete === (source.partial_capsules === 0
      && (!hasStoreReceipt || source.source_store_complete))
  const listKnown = v2 && canonicalMatched
    && count(data?.prior_runs_total) && count(data?.prior_runs_omitted)
    && rawRuns.length > 0 && rawRuns.length <= 8 && data.prior_runs_total > 0
    && data.prior_runs_total === rawRuns.length + data.prior_runs_omitted
    && bool(data.prior_runs_complete)
    && data.prior_runs_complete === (data.prior_runs_omitted === 0)
    && runs.length === rawRuns.length
  const evidenceReceiptPart = receipt => count(receipt?.concept_evidence_nodes_total)
    && count(receipt.concept_evidence_nodes_incomplete)
    && receipt.concept_evidence_nodes_incomplete <= receipt.concept_evidence_nodes_total
    && bool(receipt.concept_evidence_complete)
    && receipt.concept_evidence_complete === (receipt.concept_evidence_nodes_incomplete === 0)
  const receiptPart = (receipt, name) => count(receipt?.[`${name}_total`])
    && count(receipt[`${name}_omitted`]) && receipt[`${name}_omitted`] <= receipt[`${name}_total`]
    && bool(receipt[`${name}_complete`])
    && receipt[`${name}_complete`] === (receipt[`${name}_omitted`] === 0
      && receipt.concept_evidence_complete)
  const runReceiptKnown = run => {
    const concepts = run?.matched_concepts
    const rows = run?.matched_concept_outcomes
    const canonical = canonicalLabels(concepts)
    if (!canonical || !Array.isArray(rows) || rows.length !== canonical.length
        || !rows.every(row => record(row) && concepts.includes(row.concept)
          && normalizeConceptId(row.concept) === row.concept && bool(row.outcome_retained))
        || new Set(rows.map(row => row.concept)).size !== rows.length) return false
    const receipt = run.source_receipt
    // CODEX AGENT: collection bounds alone cannot prove that every active classifier assignment was
    // materialized. Historical v2 events without the additive producer denominator remain visible but
    // are explicitly unknown; a known-partial receipt is valid and must not be rejected as malformed.
    return record(receipt) && evidenceReceiptPart(receipt) && receiptPart(receipt, 'concepts')
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
      && metric(row.outcome) !== null && canonicalMatched.includes(row.concept)) : null
  const concept = labels([matchedOutcome?.concept])[0]
  const runBest = metric(first?.run_best_metric) ?? metric(first?.best_metric)
  const outcome = matchedOutcome ? `; closest run matched outcome ${concept}=${fmt(matchedOutcome.outcome)}`
    : runBest !== null ? `; closest run best ${fmt(runBest)}` : ''
  let warning = receiptsKnown ? '' : ' · evidence completeness unknown'
  if (receiptsKnown && !source.source_complete) {
    const reasons = []
    if (source.source_unknown_capsules) reasons.push('some capsule receipts unknown')
    if (hasStoreReceipt && source.source_rows_quarantined) {
      reasons.push(`${source.source_rows_quarantined} durable row${source.source_rows_quarantined === 1 ? '' : 's'} quarantined`)
    }
    warning = ` · PARTIAL source${reasons.length ? ` (${reasons.join('; ')})` : ''}`
  }
  if (receiptsKnown && !data.prior_runs_complete) warning += ' · PARTIAL run list'
  return `cross-run prior${matched.length ? ': ' + matched.slice(0, 3).join(', ') : ''} — ${history}${outcome}${warning}`
}
