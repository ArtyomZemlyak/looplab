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
// # CODEX AGENT: JSON booleans and numeric strings are not receipt counts. Number-coercing them would
// fabricate evidence (true -> 1) or let an untyped total hide rows omitted by the client projection.
const count = (value, fallback = 0) => Number.isSafeInteger(value) && value >= 0 ? value : fallback

const thinProjectionTotal = (atlas, included, fallback) => {
  const total = atlas.thin_coverage_total
  // A malformed/missing additive receipt cannot inflate the new thin-observation omission badge.
  // Equality to the difference of two safe non-negative integers also proves `omitted` is one.
  return Number.isSafeInteger(total) && total >= included
    && atlas.thin_coverage_omitted === total - included ? total : fallback
}

const normalizeConceptSource = atlas => {
  const receipt = record(atlas.concept_source)
  const keys = ['partial_capsules', 'source_unknown_capsules',
    'source_concepts_omitted', 'source_outcomes_omitted']
  const counts = keys.map(key => count(receipt[key], -1))
  const storeKeys = ['source_rows_total', 'source_rows_quarantined',
    'source_malformed_rows', 'source_invalid_capsule_rows', 'source_duplicate_run_rows']
  const store = storeKeys.map(key => count(receipt[key], -1))
  const complete = receipt.source_complete
  // # CODEX AGENT: the current Atlas API always emits one atomic capsule+store receipt. Legacy/missing
  // response shapes remain safely UNKNOWN instead of shipping a second client-side compatibility mode.
  const valid = typeof complete === 'boolean' && typeof receipt.source_store_complete === 'boolean'
    && counts.every(value => value >= 0) && store.every(value => value >= 0)
    && counts[1] <= counts[0] && store[1] <= store[0]
    && store[1] === store[2] + store[3] + store[4]
    && receipt.source_store_complete === (store[1] === 0)
    && complete === (counts[0] === 0 && receipt.source_store_complete)
    && !(complete && counts.some(Boolean))
  if (!valid) return { status: 'unknown' }
  return { status: complete ? 'complete' : 'partial', counts }
}

const READ_KEYS = ['rows_total', 'rows_retained', 'rows_quarantined',
  'malformed_rows', 'invalid_rows']
const normalizeReadSegment = value => {
  const segment = record(value)
  const values = READ_KEYS.map(key => count(segment[key], -1))
  const [total, retained, quarantined, malformed, invalid] = values
  if (typeof segment.read_complete !== 'boolean' || values.some(item => item < 0)
    || total !== retained + quarantined || quarantined !== malformed + invalid
    || segment.read_complete !== (quarantined === 0)) return null
  return [segment.read_complete, total, retained, quarantined, malformed, invalid]
}
// All inputs are first projected into small fixed-order records, so canonical JSON equality is a bounded,
// exact comparison without repeating a second field list for every receipt family.
const sameReceipt = (left, right) => JSON.stringify(left) === JSON.stringify(right)

const unknownClaimSource = () => ({ status: 'unknown' })

const normalizeClaimSource = value => {
  const receipt = record(value)
  const lessons = normalizeReadSegment(receipt.lessons)
  const research = normalizeReadSegment(receipt.research)
  const boolsValid = ['receipt_known', 'source_complete', 'read_complete',
    'research_source_complete'].every(key => typeof receipt[key] === 'boolean')
  if (receipt.v !== 1 || !boolsValid || !lessons || !research) return null
  const known = receipt.receipt_known
  const digest = receipt.snapshot_digest
  const digestValid = typeof digest === 'string'
    && (known ? /^[0-9a-f]{64}$/.test(digest) : digest === '')
  const readComplete = lessons[0] && research[0]
  const consistent = digestValid && (known
    ? receipt.read_complete === readComplete
      && (!receipt.research_source_complete || research[0])
      && receipt.source_complete === (lessons[0] && receipt.research_source_complete)
    : receipt.source_complete === false && receipt.read_complete === false
      && receipt.research_source_complete === false)
  return consistent ? receipt : null
}

// # CODEX AGENT: the server's validated combined claim-source receipt is the one UI authority. Requiring
// the same bounded receipt on both envelopes and every visible row keeps refresh races fail-closed without
// shipping a second, subtly different implementation of the server's producer-ledger validator.
const claimSection = (envelope, rows, hasRows = rows.length > 0) => {
  const hasEnvelope = Object.hasOwn(envelope, 'claim_source')
  const source = normalizeClaimSource(envelope.claim_source)
  const coherent = hasEnvelope && rows.every(row => {
    const item = record(row)
    return Object.hasOwn(item, 'claim_source')
      && sameReceipt(normalizeClaimSource(item.claim_source), source)
  })
  return { present: hasEnvelope || hasRows, source: coherent ? source : null }
}

const combinedClaimSource = sections => {
  const visible = sections.filter(section => section.present)
  if (visible.length === 0) return null
  const first = visible[0].source
  if (visible.length === 1) return first
  // Independent endpoint reads may race a store rewrite. A non-empty combined snapshot digest is the
  // cross-endpoint identity; matching counts/status alone cannot prove the rows came from one read epoch.
  return first?.snapshot_digest
    && visible.every(section => sameReceipt(section.source, first))
    ? first : null
}

const claimSourceView = receipt => {
  if (!receipt) return unknownClaimSource()
  return { status: receipt.receipt_known
    ? (receipt.source_complete ? 'complete' : 'partial') : 'unknown' }
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
  if (key === 'conceptCuration' || key === 'claimCuration') {
    if (!Array.isArray(value.entries)
      || !value.entries.every(entry => entry && typeof entry === 'object' && !Array.isArray(entry))) {
      return false
    }
    // # CODEX AGENT: current UI and server ship together; require the complete v1 health envelope so a
    // legacy/torn 200 response retains last-good state exactly like an unavailable read.
    return value.v === 1 && value.status === 'complete' && value.complete === true
      && Number.isSafeInteger(value.n) && value.n >= value.entries.length
      && Number.isSafeInteger(value.limit) && value.limit >= 1 && value.limit <= 200
      && value.entries.length <= value.limit
  }
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

const normalizeClaim = (value, source = unknownClaimSource()) => {
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
  // The backend's structured `mixed` state can encode assertion-level counter-evidence that is not
  // counted in n_oppose. Honor it only with supporting evidence, matching the backend rule, while
  // still deriving malformed or contradictory support/refute labels from sanitized counts.
  const mixed = nSupport > 0
    && (contradicts.length > 0 || row.epistemic === 'mixed' || nOppose > 0)
  const evidenceEpistemic = mixed
    ? 'mixed'
    : (nSupport > 0 ? 'supported' : (nOppose > 0 ? 'refuted' : 'inconclusive'))
  // # CODEX AGENT: D8 is only one input. A quarantined lesson row can contain the missing opposite side even
  // when D8 is complete, so only the combined claim-source authority may unlock a one-sided state.
  const epistemic = ['supported', 'refuted'].includes(evidenceEpistemic)
    && source.status !== 'complete'
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
    scopes: textList(row.scopes, ATLAS_RENDER_LIMITS.evidence),
    runs: textList(row.runs, ATLAS_RENDER_LIMITS.evidence, 160),
  }
}

const itemCount = value => Array.isArray(value) ? value.length : count(value)
const normalizeCuration = value => {
  const row = record(value)
  const proposals = record(row.proposals)
  const receipt = record(row.receipt)
  const proposed = ['merges', 'splits', 'purges', 'decisions']
    .reduce((sum, key) => sum + itemCount(proposals[key]), 0)
  const applied = itemCount(receipt.applied)
  const knownOutcome = ['proposed', 'empty', 'unavailable', 'error', 'already-governed']
    .includes(row.outcome) ? row.outcome : ''
  const normalized = {
    kind: row.kind === 'claim' ? 'claim' : 'concept',
    outcome: knownOutcome || (proposed > 0 ? 'proposed' : 'unknown'),
    proposals: proposed,
    applied,
  }
  return knownOutcome || proposed + applied > 0 ? normalized : null
}

export function mergeCurationLogs(conceptValue, claimValue) {
  const sources = [
    { kind: 'concept', envelope: record(conceptValue) },
    { kind: 'claim', envelope: record(claimValue) },
  ]
  // Each API log is newest-first. One entry per steward keeps this preview useful and bounded without
  // recreating a cross-ledger ordering that the independent snapshots cannot prove (CODEX AGENT).
  const entries = sources.flatMap(({ kind, envelope }) => list(envelope.entries).length
    ? [{ ...record(envelope.entries[0]), kind }] : [])
  const total = sources.reduce((sum, { envelope }) =>
    sum + Math.max(count(envelope.n), list(envelope.entries).length), 0)
  return { entries, n: total }
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
  const claimReceipt = combinedClaimSource([
    claimSection(atlas, contradictionRows, rawContradictions.length > 0),
    claimSection(claimsEnvelope, claimRows, rawClaims.length > 0),
  ])
  const claimSource = claimSourceView(claimReceipt)
  const concepts = conceptRows.map(normalizeConcept).filter(Boolean)
  const thin = textList(thinRows, ATLAS_RENDER_LIMITS.thin, 220)
  const contradictions = contradictionRows.map(row => normalizeClaim(row, claimSource)).filter(Boolean)
  const claims = claimRows.map(row => normalizeClaim(row, claimSource)).filter(Boolean)
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
    claimSource,
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
