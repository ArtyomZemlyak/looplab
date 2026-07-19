// Portfolio stores contain untrusted model-authored text and may be very large. Normalize
// every shape and enforce hard render caps before React sees it; make every truncation explicit in the view.
export const ATLAS_RENDER_LIMITS = Object.freeze({
  concepts: 24,
  conceptRuns: 6,
  thin: 24,
  contradictions: 12,
  claims: 40,
  evidence: 6,
  curation: 20,
})
export const ATLAS_SOURCE_KEYS = Object.freeze([
  'atlas', 'claims', 'conceptCuration', 'claimCuration',
])

const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}
const list = value => Array.isArray(value) ? value : []
const count = (value, fallback = 0) => {
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : fallback
}

const thinProjectionTotal = (atlas, included, fallback) => {
  const total = atlas.thin_coverage_total
  // A malformed/missing additive receipt cannot inflate the new thin-observation omission badge.
  // Equality to the difference of two safe non-negative integers also proves `omitted` is one.
  return Number.isSafeInteger(total) && total >= included
    && atlas.thin_coverage_omitted === total - included ? total : fallback
}

const normalizeConceptSource = atlas => {
  const receipt = record(Object.hasOwn(atlas, 'concept_source')
    ? atlas.concept_source : record(atlas.context_pack).coverage)
  const keys = ['partial_capsules', 'source_unknown_capsules',
    'source_concepts_omitted', 'source_outcomes_omitted']
  const counts = keys.map(key => Object.hasOwn(receipt, key)
    && Number.isSafeInteger(receipt[key]) && receipt[key] >= 0 ? receipt[key] : -1)
  const storeKeys = ['source_rows_total', 'source_rows_quarantined',
    'source_malformed_rows', 'source_invalid_capsule_rows', 'source_duplicate_run_rows']
  const storeFieldCount = ['source_store_complete', ...storeKeys]
    .filter(key => Object.hasOwn(receipt, key)).length
  const hasStoreReceipt = storeFieldCount > 0
  const storeCounts = storeKeys.map(key => Object.hasOwn(receipt, key)
    && Number.isSafeInteger(receipt[key]) && receipt[key] >= 0 ? receipt[key] : -1)
  // CODEX AGENT: old v2 receipts remain readable, while the additive durable-store extension must be
  // present and coherent as one unit. Quarantine is known partial evidence, not an unknown receipt.
  const storeValid = !hasStoreReceipt || (storeFieldCount === storeKeys.length + 1
    && typeof receipt.source_store_complete === 'boolean'
    && storeCounts.every(value => value >= 0)
    && storeCounts[1] <= storeCounts[0]
    && storeCounts[1] === storeCounts[2] + storeCounts[3] + storeCounts[4]
    && receipt.source_store_complete === (storeCounts[1] === 0))
  const complete = receipt.source_complete
  const valid = Object.hasOwn(receipt, 'source_complete')
    && typeof complete === 'boolean' && counts.every(value => value >= 0)
    && storeValid && complete === (counts[0] === 0
      && (!hasStoreReceipt || receipt.source_store_complete)) && counts[1] <= counts[0]
    && !(complete && counts.some(Boolean))
  // CODEX AGENT: missing, malformed, and internally contradictory receipts are UNKNOWN. A fresh HTTP
  // response is not proof that its bounded capsule source was complete.
  if (!valid) return { status: 'unknown' }
  const normalized = { status: complete ? 'complete' : 'partial', counts }
  if (hasStoreReceipt) {
    normalized.store = {
      total: storeCounts[0], quarantined: storeCounts[1], malformed: storeCounts[2],
      invalid: storeCounts[3], duplicates: storeCounts[4],
    }
  }
  return normalized
}

const normalizeResearchSource = value => {
  const receipt = record(value)
  const boolKeys = ['source_complete', 'producer_receipt_known', 'producer_complete']
  const intKeys = ['producer_runs', 'producer_partial_runs', 'producer_unknown_runs',
    'producer_claims_total', 'producer_claims_retained', 'producer_claims_omitted']
  const boolsValid = boolKeys.every(key => typeof receipt[key] === 'boolean')
  const counts = Object.fromEntries(intKeys.map(key => [key,
    Number.isSafeInteger(receipt[key]) && receipt[key] >= 0 ? receipt[key] : -1]))
  const countsValid = intKeys.every(key => counts[key] >= 0)
  const known = receipt.producer_receipt_known === true
  const consistent = boolsValid && countsValid
    && counts.producer_partial_runs + counts.producer_unknown_runs <= counts.producer_runs
    // CODEX AGENT: a producer receipt cannot be both known and count an unknown run. Reject the whole
    // aggregate instead of rendering a contradictory exact/complete state from an untrusted response.
    && known === (counts.producer_unknown_runs === 0)
    && receipt.producer_complete === (known && counts.producer_partial_runs === 0)
    && receipt.source_complete === receipt.producer_complete
    && (!known || (counts.producer_claims_total >= counts.producer_claims_retained
      && counts.producer_claims_omitted
        === counts.producer_claims_total - counts.producer_claims_retained))
  if (!consistent) return { status: 'unknown', counts: null }
  return {
    status: receipt.source_complete ? 'complete'
      : counts.producer_partial_runs > 0 ? 'partial' : 'unknown',
    counts,
  }
}

const unknownResearchSource = () => ({ status: 'unknown', counts: null })
const sameResearchSource = (left, right) => {
  if (left.status !== right.status) return false
  if (left.counts === null || right.counts === null) return left.counts === right.counts
  return Object.keys(left.counts).every(key => left.counts[key] === right.counts[key])
    && Object.keys(right.counts).every(key => left.counts[key] === right.counts[key])
}

const sectionResearchSource = (envelope, rows, hasRows = rows.length > 0) => {
  const hasEnvelope = Object.hasOwn(envelope, 'research_source')
  const source = normalizeResearchSource(hasEnvelope
    ? envelope.research_source : record(rows[0]).research_source)
  // CODEX AGENT: every row and its endpoint envelope describe one frozen D8 denominator. A single
  // mismatching/missing row receipt makes the whole visible section unknown; otherwise a forged card could
  // restore `supported` underneath a partial top-level warning.
  const coherent = rows.every(row => sameResearchSource(
    normalizeResearchSource(record(row).research_source), source))
  return {
    present: hasEnvelope || hasRows,
    source: coherent ? source : unknownResearchSource(),
  }
}

const combinedResearchSource = (...sections) => {
  const visible = sections.filter(section => section.present)
  if (visible.length === 0) return unknownResearchSource()
  const first = visible[0].source
  return visible.every(section => sameResearchSource(section.source, first))
    ? first : unknownResearchSource()
}

const sourceRevision = (key, value) => {
  const envelope = record(value)
  if (key === 'atlas') {
    const revisions = record(envelope.revisions)
    const concept = count(revisions.concept_governance, -1)
    const claims = count(revisions.claims, -1)
    const parts = []
    if (concept >= 0) parts.push(`concept ${concept}`)
    if (claims >= 0) parts.push(`claims ${claims}`)
    return parts.join(' · ')
  }
  if (key === 'claims') {
    const revision = count(envelope.revision, -1)
    return revision >= 0 ? String(revision) : ''
  }
  // The raw envelope is outside the render boundary. Inspect only the bounded visible slice so a
  // mis-limited/corrupt ledger cannot turn a watermark calculation into O(raw entries) work.
  const entries = list(envelope.entries)
  const length = Math.min(entries.length, ATLAS_RENDER_LIMITS.curation)
  let maximum = -1
  for (let index = 0; index < length; index++) {
    const revision = count(record(entries[index]).revision, -1)
    if (revision > maximum) maximum = revision
  }
  return maximum >= 0 ? String(maximum) : ''
}

export function isValidAtlasSourceEnvelope(key, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  if (key === 'atlas') {
    return Array.isArray(value.explored)
      && Array.isArray(value.thin_coverage)
      && Array.isArray(value.contradictions)
  }
  if (key === 'claims') return Array.isArray(value.claims)
  if (key === 'conceptCuration' || key === 'claimCuration') return Array.isArray(value.entries)
  return false
}

// Four Atlas endpoints settle independently. Attempted successes become current; attempted failures
// retain last-good data as stale (or stay failed when none exists); unattempted slices stay unchanged.
export function reconcileAtlasSourceStatuses(previousValue, successfulValue, loadedAt,
  attemptedKeys = ATLAS_SOURCE_KEYS) {
  const previous = record(previousValue)
  const successful = record(successfulValue)
  const attempted = list(attemptedKeys).slice(0, ATLAS_SOURCE_KEYS.length)
  const timestamp = boundedAtlasText(loadedAt, 80)
  const statuses = {}
  const failed = { state: 'failed', loadedAt: '', revision: '' }
  for (const key of ATLAS_SOURCE_KEYS) {
    const prior = record(previous[key])
    if (!attempted.includes(key)) {
      statuses[key] = prior.state ? prior : failed
      continue
    }
    if (Object.hasOwn(successful, key)) {
      statuses[key] = {
        state: 'current', loadedAt: timestamp,
        revision: sourceRevision(key, successful[key]),
      }
      continue
    }
    statuses[key] = ['current', 'retained-stale'].includes(prior.state)
      ? { ...prior, state: 'retained-stale' }
      : failed
  }
  return statuses
}

export function boundedAtlasText(value, max = 360) {
  if (!['string', 'number', 'boolean'].includes(typeof value)) return ''
  const limit = Number.isSafeInteger(max) ? Math.max(0, Math.min(2000, max)) : 360
  // Slice before normalization so one model-authored field cannot force an unbounded regex pass.
  const text = String(value).slice(0, limit)
  // Directional formatting controls can visually reorder an otherwise escaped task/scope and move a
  // disclosure away from its value. Remove them together with ASCII controls before rendering.
  return text.replace(/[\u0000-\u001f\u007f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, ' ')
    .replace(/\s+/gu, ' ').trim().slice(0, limit)
}

const take = (value, max) => list(value).slice(0, max)
const textList = (value, max, textMax = 180) => take(value, max)
  .map(item => boundedAtlasText(item, textMax)).filter(Boolean)
const comparisonContextText = (value, max) => typeof value === 'string'
  ? boundedAtlasText(value, max)
  : ''
const normalizeRun = value => {
  const row = record(value)
  const runId = boundedAtlasText(row.run_id, 160)
  // A raw objective value is meaningful inside its own run, but it is not a cross-run comparison
  // without at least a recorded task/scope and optimization orientation. Fail closed before React:
  // legacy Atlas rows normally lack that context, so their metric value is deliberately suppressed.
  const task = comparisonContextText(row.task_id, 180)
    || comparisonContextText(row.task, 220)
  const scope = comparisonContextText(row.scope, 220)
    || comparisonContextText(row.task_scope, 220)
  const rawMetric = typeof row.metric === 'number' && Number.isFinite(row.metric) ? row.metric : null
  const rawOrientation = row.direction === 'max' || row.direction === 'min' ? row.direction : ''
  const discloseMetric = rawMetric !== null && !!rawOrientation && !!(task || scope)
  return {
    runId,
    task,
    scope,
    metric: discloseMetric ? rawMetric : null,
    optimizationOrientation: discloseMetric
      ? (rawOrientation === 'max' ? 'maximize' : 'minimize')
      : '',
    metricSuppressed: rawMetric !== null && !discloseMetric,
  }
}

const normalizeConcept = value => {
  const row = record(value)
  const concept = boundedAtlasText(row.concept, 220)
  if (!concept) return null
  const runs = take(row.runs, ATLAS_RENDER_LIMITS.conceptRuns).map(normalizeRun)
    .filter(run => run.runId)
  return {
    concept,
    nRuns: Math.max(count(row.n_runs), runs.length),
    runs,
  }
}

const normalizeClaim = (value, sectionSource = unknownResearchSource()) => {
  const row = record(value)
  const statement = boundedAtlasText(row.statement, 500)
  if (!statement) return null
  const maturity = ['machine-proposed', 'operator-ratified', 'operator-rejected', 'operator-pinned']
    .includes(row.maturity) ? row.maturity : 'machine-proposed'
  // Every portfolio string is model-authored/untrusted and every collection is capped here. Components
  // may render this projection directly, but must never bypass it with raw API payloads.
  const support = textList(row.support, ATLAS_RENDER_LIMITS.evidence)
  const oppose = textList(row.oppose, ATLAS_RENDER_LIMITS.evidence)
  const unverified = textList(row.unverified, ATLAS_RENDER_LIMITS.evidence)
  // Structured claims represent opposite-polarity assertions separately from attempt-level `oppose`
  // refs. Preserve that bounded counter-evidence or a mixed row can misleadingly look support-only.
  const rawContradicts = list(row.contradicts)
  const contradicts = textList(rawContradicts, ATLAS_RENDER_LIMITS.evidence, 300)
  const nSupport = Math.max(count(row.n_support), support.length)
  const nOppose = Math.max(count(row.n_oppose), oppose.length)
  const rowSource = normalizeResearchSource(row.research_source)
  const researchSource = sameResearchSource(rowSource, sectionSource)
    ? rowSource : unknownResearchSource()
  // The backend's structured `mixed` state can encode assertion-level counter-evidence that is not
  // counted in n_oppose. Honor it only with supporting evidence, matching the backend rule, while
  // still deriving malformed or contradictory support/refute labels from sanitized counts.
  const mixed = nSupport > 0
    && (contradicts.length > 0 || row.epistemic === 'mixed' || nOppose > 0)
  const evidenceEpistemic = mixed
    ? 'mixed'
    : (nSupport > 0 ? 'supported' : (nOppose > 0 ? 'refuted' : 'inconclusive'))
  // CODEX AGENT: retained counts are lower bounds when the D8 producer capped/forgot its denominator.
  // Never reconstruct either backend-withheld one-sided state merely from visible counts.
  const epistemic = ['supported', 'refuted'].includes(evidenceEpistemic)
    && researchSource.status !== 'complete'
    ? 'inconclusive' : evidenceEpistemic
  return {
    uid: boundedAtlasText(row.claim_uid, 180),
    statement,
    epistemic,
    maturity,
    decisionFresh: typeof row.decision_fresh === 'boolean' ? row.decision_fresh : null,
    nSupport,
    nOppose,
    nUnverified: Math.max(count(row.n_unverified), unverified.length),
    nContradicts: Math.max(count(row.n_contradicts), rawContradicts.length, contradicts.length),
    support,
    oppose,
    unverified,
    contradicts,
    researchSource,
    scopes: textList(row.scopes, ATLAS_RENDER_LIMITS.evidence),
    runs: textList(row.runs, ATLAS_RENDER_LIMITS.evidence, 160),
  }
}

const itemCount = value => Array.isArray(value) ? value.length : count(value)
const normalizeCuration = value => {
  const row = record(value)
  const proposals = record(row.proposals)
  const receipt = record(row.receipt)
  const merges = itemCount(proposals.merges)
  const splits = itemCount(proposals.splits)
  const purges = itemCount(proposals.purges)
  const decisions = itemCount(proposals.decisions)
  const applied = itemCount(receipt.applied)
  const skipped = itemCount(receipt.skipped)
  const knownOutcome = ['proposed', 'empty', 'unavailable', 'error', 'already-governed']
    .includes(row.outcome) ? row.outcome : ''
  const normalized = {
    kind: row.kind === 'claim' ? 'claim' : 'concept',
    runId: boundedAtlasText(row.run_id, 160),
    at: boundedAtlasText(row.at, 80),
    revision: count(row.revision),
    outcome: knownOutcome || (merges + splits + purges + decisions > 0 ? 'proposed' : 'unknown'),
    autoRequested: row.auto_requested === true,
    merges,
    splits,
    purges,
    decisions,
    applied,
    skipped,
  }
  const activity = merges + splits + purges + decisions + applied + skipped
  return normalized.runId || normalized.at || knownOutcome || activity > 0 ? normalized : null
}

export function mergeCurationLogs(conceptValue, claimValue) {
  const sources = [
    { kind: 'concept', envelope: record(conceptValue) },
    { kind: 'claim', envelope: record(claimValue) },
  ]
  const entries = sources.flatMap(({ kind, envelope }) => take(envelope.entries, ATLAS_RENDER_LIMITS.curation)
    .map((entry, sourceIndex) => ({ ...record(entry), kind, _sourceIndex: sourceIndex })))
  entries.sort((a, b) => {
    const aTime = Date.parse(boundedAtlasText(a.at, 80))
    const bTime = Date.parse(boundedAtlasText(b.at, 80))
    const timeDelta = (Number.isFinite(bTime) ? bTime : -1) - (Number.isFinite(aTime) ? aTime : -1)
    if (timeDelta) return timeDelta
    const revisionDelta = count(b.revision) - count(a.revision)
    if (revisionDelta) return revisionDelta
    const kindDelta = a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0
    return kindDelta || a._sourceIndex - b._sourceIndex
  })
  const total = sources.reduce((sum, { envelope }) =>
    sum + Math.max(count(envelope.n), take(envelope.entries, ATLAS_RENDER_LIMITS.curation).length), 0)
  return {
    entries: entries.map(({ _sourceIndex, ...entry }) => entry),
    n: total,
  }
}

export function mergeResearchAtlasPayload(previousValue, successfulValue) {
  const previous = record(previousValue)
  const successful = record(successfulValue)
  const source = key => Object.hasOwn(successful, key)
    ? record(successful[key])
    : record(previous[key])
  return {
    atlas: source('atlas'),
    claims: source('claims'),
    conceptCuration: source('conceptCuration'),
    claimCuration: source('claimCuration'),
  }
}

export function buildResearchAtlasView(atlasValue, claimsValue, curationValue) {
  const atlas = record(atlasValue)
  const claimsEnvelope = record(claimsValue)
  const curationEnvelope = record(curationValue)
  const rawConcepts = list(atlas.explored)
  const rawThin = list(atlas.thin_coverage)
  const rawContradictions = list(atlas.contradictions)
  const rawClaims = list(claimsEnvelope.claims)
  const rawCuration = list(curationEnvelope.entries)
  const conceptRows = take(rawConcepts, ATLAS_RENDER_LIMITS.concepts)
  const thinRows = take(rawThin, ATLAS_RENDER_LIMITS.thin)
  const contradictionRows = take(rawContradictions, ATLAS_RENDER_LIMITS.contradictions)
  const claimRows = take(rawClaims, ATLAS_RENDER_LIMITS.claims)
  const curationRows = take(rawCuration, ATLAS_RENDER_LIMITS.curation)
  // Receipt coherence is needed only for rows that can cross the render boundary. Keep the validation
  // work under the same hard caps as normalization even when a hostile response contains millions of rows.
  const atlasResearch = sectionResearchSource(
    atlas, contradictionRows, rawContradictions.length > 0)
  const claimsResearch = sectionResearchSource(
    claimsEnvelope, claimRows, rawClaims.length > 0)
  const researchSource = combinedResearchSource(atlasResearch, claimsResearch)
  const concepts = conceptRows.map(normalizeConcept).filter(Boolean)
  const thin = textList(thinRows, ATLAS_RENDER_LIMITS.thin, 220)
  const contradictions = contradictionRows
    .map(row => normalizeClaim(row, atlasResearch.source)).filter(Boolean)
  const claims = claimRows
    .map(row => normalizeClaim(row, claimsResearch.source)).filter(Boolean)
  const curation = curationRows.map(normalizeCuration).filter(Boolean)
  const thinTotal = thinProjectionTotal(atlas, rawThin.length,
    rawThin.length > ATLAS_RENDER_LIMITS.thin ? rawThin.length : thin.length)
  const invalidRows = {
    concepts: conceptRows.length - concepts.length,
    thin: thinRows.length - thin.length,
    contradictions: contradictionRows.length - contradictions.length,
    claims: claimRows.length - claims.length,
    curation: curationRows.length - curation.length,
  }
  invalidRows.total = Object.values(invalidRows).reduce((sum, value) => sum + value, 0)
  const knownRunIds = new Set([
    ...concepts.flatMap(concept => concept.runs.map(run => run.runId)),
    ...claims.flatMap(claim => claim.runs),
    ...contradictions.flatMap(claim => claim.runs),
  ].filter(Boolean))
  const inferredRuns = Math.max(knownRunIds.size, ...concepts.map(concept => concept.nRuns), 0)
  // Server totals describe the full ledger, but they can lag a projection during a concurrent append.
  // Never let a stale/smaller total hide valid rows already present in this response. Malformed rows do
  // not become evidence merely because an array contained them.
  const totals = {
    runs: Math.max(count(atlas.n_runs), inferredRuns),
    concepts: Math.max(count(atlas.n_concepts), concepts.length),
    claims: Math.max(count(claimsEnvelope.n), count(atlas.n_claims), claims.length),
    contested: Math.max(count(atlas.n_contested), contradictions.length),
    curation: Math.max(count(curationEnvelope.n), curation.length),
  }
  return {
    totals,
    conceptSource: normalizeConceptSource(atlas),
    researchSource,
    concepts,
    thin,
    contradictions,
    claims,
    curation,
    invalidRows,
    hiddenConcepts: Math.max(0, totals.concepts - concepts.length,
      rawConcepts.length - ATLAS_RENDER_LIMITS.concepts),
    hiddenThin: Math.max(0, thinTotal - thin.length,
      rawThin.length - ATLAS_RENDER_LIMITS.thin),
    hiddenContradictions: Math.max(0, totals.contested - contradictions.length,
      rawContradictions.length - ATLAS_RENDER_LIMITS.contradictions),
    hiddenClaims: Math.max(0, totals.claims - claims.length,
      rawClaims.length - ATLAS_RENDER_LIMITS.claims),
    hiddenCuration: Math.max(0, totals.curation - curation.length,
      rawCuration.length - ATLAS_RENDER_LIMITS.curation),
    empty: totals.runs === 0 && totals.concepts === 0 && totals.claims === 0
      && totals.contested === 0 && totals.curation === 0
      && concepts.length === 0 && thin.length === 0 && contradictions.length === 0
      && claims.length === 0 && curation.length === 0,
  }
}
