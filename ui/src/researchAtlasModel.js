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

// Four Atlas endpoints refresh independently. Preserve source-local provenance so a successful claims
// refresh can never make retained concept/log slices look current. `successful` contains only the
// slices fulfilled by this attempt: omitted last-good slices become retained-stale and never-loaded
// slices remain explicitly failed.
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
    // # CODEX AGENT: a source-local retry says nothing about the other ledgers. Their provenance
    // must survive byte-for-byte instead of being silently promoted to current or demoted to stale.
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
  return text.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/gu, ' ').trim().slice(0, limit)
}

const take = (value, max) => list(value).slice(0, max)
const textList = (value, max, textMax = 180) => take(value, max)
  .map(item => boundedAtlasText(item, textMax)).filter(Boolean)
const normalizeRun = value => {
  const row = record(value)
  return {
    runId: boundedAtlasText(row.run_id, 160),
    metric: typeof row.metric === 'number' && Number.isFinite(row.metric) ? row.metric : null,
    direction: row.direction === 'max' || row.direction === 'min' ? row.direction : 'unknown',
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

const normalizeClaim = value => {
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
  const epistemic = mixed
    ? 'mixed'
    : (nSupport > 0 ? 'supported' : (nOppose > 0 ? 'refuted' : 'inconclusive'))
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
  const concepts = conceptRows.map(normalizeConcept).filter(Boolean)
  const thin = textList(thinRows, ATLAS_RENDER_LIMITS.thin, 220)
  const contradictions = contradictionRows.map(normalizeClaim).filter(Boolean)
  const claims = claimRows.map(normalizeClaim).filter(Boolean)
  const curation = curationRows.map(normalizeCuration).filter(Boolean)
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
    concepts,
    thin,
    contradictions,
    claims,
    curation,
    invalidRows,
    hiddenConcepts: Math.max(0, totals.concepts - concepts.length,
      rawConcepts.length - ATLAS_RENDER_LIMITS.concepts),
    hiddenThin: Math.max(0, rawThin.length - ATLAS_RENDER_LIMITS.thin),
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
