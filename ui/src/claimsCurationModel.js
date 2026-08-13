import { boundedLedgerText, projectLedgerSource } from './api.js'

export { boundedLedgerText, projectLedgerSource }

// The pure model behind the Claims & Curation surface (the former Research Atlas — doc 29 F7).
// Portfolio stores contain untrusted model-authored text and may be very large. Normalize
// every shape and enforce hard render caps before React sees it; make every truncation explicit in the view.
export const LEDGER_RENDER_LIMITS = Object.freeze({
  contradictions: 12,
  claims: 40,
  evidence: 6,
  curation: 20,
})
// The four independently fetched slices. `atlas` keeps the ENDPOINT's own spelling
// (`GET /api/cross-run/atlas`) on purpose: F7 renamed the operator's destination, not the HTTP
// contract, and a key that no longer names its route is how a reader starts hunting for a route that
// does not exist. What changed is what this client reads OUT of that slice: the concepts section it
// used to render moved to the run list's Concepts view, so `explored`/`thin_coverage` are no longer
// projected and the slice is now read for exactly one thing — the mixed-evidence claim records.
export const LEDGER_SOURCE_KEYS = Object.freeze([
  'atlas', 'claims', 'conceptCuration', 'claimCuration',
])
const PORTFOLIO_ID_RE = /^portfolio-sha256:[0-9a-f]{64}$/
// A transport cap is the limit THIS CLIENT ASKED FOR, never the render cap: `getCrossRunAtlas(24)`
// may legitimately answer with 24 mixed-evidence rows while only 12 are rendered, so validating the
// envelope against the render cap would reject a well-formed response.
const LEDGER_TRANSPORT_LIMITS = Object.freeze({
  atlas: 24,
  claims: LEDGER_RENDER_LIMITS.claims,
  curation: LEDGER_RENDER_LIMITS.curation,
})

const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {}
const list = value => Array.isArray(value) ? value : []
// JSON booleans and numeric strings are not receipt counts. Number-coercing them would
// fabricate evidence (true -> 1) or let an untyped total hide rows omitted by the client projection.
const count = (value, fallback = 0) => Number.isSafeInteger(value) && value >= 0 ? value : fallback

export const sourcePortfolioId = value => {
  const identity = record(value).portfolio_id
  return typeof identity === 'string' && PORTFOLIO_ID_RE.test(identity) ? identity : ''
}

export const ledgerPayloadPortfolioId = value => {
  const payload = record(value)
  const identities = new Set(LEDGER_SOURCE_KEYS.map(key => sourcePortfolioId(payload[key])).filter(Boolean))
  return identities.size === 1 ? [...identities][0] : ''
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

// the server's validated combined claim-source receipt is the one UI authority. Requiring
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
  const length = Math.min(entries.length, LEDGER_RENDER_LIMITS.curation)
  let maximum = -1
  for (let index = 0; index < length; index++) {
    const revision = count(record(entries[index]).revision, -1)
    if (revision > maximum) maximum = revision
  }
  return maximum >= 0 ? String(maximum) : ''
}

export function isValidLedgerSourceEnvelope(key, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  // All four independently fetched slices must name the same configured portfolio. Without this opaque
  // replacement fence, equal ledger revisions can make a settings switch look like one coherent preview.
  if (!sourcePortfolioId(value)) return false
  if (key === 'atlas') {
    // Only `contradictions` crosses the render boundary now (F7). The envelope check fences what this
    // surface RENDERS; requiring `explored`/`thin_coverage` would fence a section it no longer has,
    // and would refuse a future server that stops sending sections nobody reads.
    return Array.isArray(value.contradictions)
      && value.contradictions.length <= LEDGER_TRANSPORT_LIMITS.atlas
  }
  if (key === 'claims') {
    return Array.isArray(value.claims)
      && value.claims.length <= LEDGER_TRANSPORT_LIMITS.claims
  }
  if (key === 'conceptCuration' || key === 'claimCuration') {
    if (!Array.isArray(value.entries)
      || value.entries.length > LEDGER_TRANSPORT_LIMITS.curation
      || !value.entries.every(entry => entry && typeof entry === 'object' && !Array.isArray(entry))) {
      return false
    }
    // current UI and server ship together; require the complete v1 health envelope so a
    // legacy/torn 200 response retains last-good state exactly like an unavailable read.
    return value.v === 1 && value.status === 'complete' && value.complete === true
      && Number.isSafeInteger(value.n) && value.n >= value.entries.length
      && Number.isSafeInteger(value.limit) && value.limit >= 1 && value.limit <= 200
      && value.entries.length <= value.limit
  }
  return false
}

// Four endpoints settle independently. Attempted successes become current; attempted failures
// retain last-good data as stale (or stay failed when none exists); unattempted slices stay unchanged.
export function reconcileLedgerSourceStatuses(previousValue, successfulValue, loadedAt,
  attemptedKeys = LEDGER_SOURCE_KEYS) {
  const previous = record(previousValue)
  const successful = record(successfulValue)
  const attempted = list(attemptedKeys).slice(0, LEDGER_SOURCE_KEYS.length)
  const timestamp = boundedLedgerText(loadedAt, 80)
  const statuses = {}
  const failed = { state: 'failed', loadedAt: '', revision: '' }
  for (const key of LEDGER_SOURCE_KEYS) {
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

const take = (value, max) => list(value).slice(0, max)
const textList = (value, max, textMax = 180) => take(value, max)
  .map(item => boundedLedgerText(item, textMax)).filter(Boolean)

const normalizeClaim = (value, source = unknownClaimSource()) => {
  const row = record(value)
  const statement = boundedLedgerText(row.statement, 500)
  if (!statement) return null
  const maturity = ['machine-proposed', 'operator-ratified', 'operator-rejected', 'operator-pinned']
    .includes(row.maturity) ? row.maturity : 'machine-proposed'
  // Every portfolio string is model-authored/untrusted and every collection is capped here. Components
  // may render this projection directly, but must never bypass it with raw API payloads.
  const support = textList(row.support, LEDGER_RENDER_LIMITS.evidence)
  const oppose = textList(row.oppose, LEDGER_RENDER_LIMITS.evidence)
  const unverified = textList(row.unverified, LEDGER_RENDER_LIMITS.evidence)
  // Structured claims represent opposite-polarity assertions separately from attempt-level `oppose`
  // refs. Preserve that bounded counter-evidence or a mixed row can misleadingly look support-only.
  const rawContradicts = list(row.contradicts)
  const contradicts = textList(rawContradicts, LEDGER_RENDER_LIMITS.evidence, 300)
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
  // D8 is only one input. A quarantined lesson row can contain the missing opposite side even
  // when D8 is complete, so only the combined claim-source authority may unlock a one-sided state.
  const epistemic = ['supported', 'refuted'].includes(evidenceEpistemic)
    && source.status !== 'complete'
    ? 'inconclusive' : evidenceEpistemic
  const decision = record(row.decision)
  const normalizedDecision = ['ratified', 'rejected', 'pinned', 'clear'].includes(decision.decision)
    ? {
        action: decision.decision,
        note: boundedLedgerText(decision.note, 500),
        by: boundedLedgerText(decision.by, 160),
        at: boundedLedgerText(decision.at, 160),
      } : null
  return {
    uid: boundedLedgerText(row.claim_uid, 180),
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
    sources: textList(row.sources, LEDGER_RENDER_LIMITS.evidence, 2000),
    verification: textList(row.verification, LEDGER_RENDER_LIMITS.evidence, 500),
    polarity: row.polarity === -1 || row.polarity === 1 ? row.polarity : null,
    metric: boundedLedgerText(row.metric, 200),
    evidenceDigest: boundedLedgerText(row.evidence_digest, 180),
    decision: normalizedDecision,
    scopes: textList(row.scopes, LEDGER_RENDER_LIMITS.evidence),
    runs: textList(row.runs, LEDGER_RENDER_LIMITS.evidence, 160),
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
  // recreating a cross-ledger ordering that the independent snapshots cannot prove.
  const entries = sources.flatMap(({ kind, envelope }) => list(envelope.entries).length
    ? [{ ...record(envelope.entries[0]), kind }] : [])
  const total = sources.reduce((sum, { envelope }) =>
    sum + Math.max(count(envelope.n), list(envelope.entries).length), 0)
  return { entries, n: total }
}

export function mergeLedgerPayload(previousValue, successfulValue,
  { allowPortfolioSwitch = false } = {}) {
  let previous = record(previousValue)
  const successful = record(successfulValue)
  const incomingIdentities = new Set(
    LEDGER_SOURCE_KEYS.filter(key => Object.hasOwn(successful, key))
      .map(key => sourcePortfolioId(successful[key])).filter(Boolean))
  const previousIdentity = ledgerPayloadPortfolioId(previous)
  if (incomingIdentities.size > 1) return previous
  const incomingIdentity = incomingIdentities.size === 1 ? [...incomingIdentities][0] : ''
  if (previousIdentity && incomingIdentity && previousIdentity !== incomingIdentity) {
    if (!allowPortfolioSwitch) return previous
    // A full refresh may intentionally move to newly configured storage. Drop every old slice before
    // accepting the first replacement response; later responses in that request batch are identity-fenced.
    previous = {}
  }
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

export function buildClaimsCurationView(atlasValue, claimsValue, curationValue) {
  const atlas = record(atlasValue)
  const claimsEnvelope = record(claimsValue)
  const curationEnvelope = record(curationValue)
  const rawContradictions = list(atlas.contradictions)
  const rawClaims = list(claimsEnvelope.claims)
  const rawCuration = list(curationEnvelope.entries)
  const contradictionRows = take(rawContradictions, LEDGER_RENDER_LIMITS.contradictions)
  const claimRows = take(rawClaims, LEDGER_RENDER_LIMITS.claims)
  const curationRows = take(rawCuration, LEDGER_RENDER_LIMITS.curation)
  // Receipt coherence is needed only for rows that can cross the render boundary. Keep the validation
  // work under the same hard caps as normalization even when a hostile response contains millions of rows.
  const claimReceipt = combinedClaimSource([
    claimSection(atlas, contradictionRows, rawContradictions.length > 0),
    claimSection(claimsEnvelope, claimRows, rawClaims.length > 0),
  ])
  const claimSource = claimSourceView(claimReceipt)
  const contradictions = contradictionRows.map(row => normalizeClaim(row, claimSource)).filter(Boolean)
  const claims = claimRows.map(row => normalizeClaim(row, claimSource)).filter(Boolean)
  const curation = curationRows.map(normalizeCuration).filter(Boolean)
  const invalidRows = {
    contradictions: contradictionRows.length - contradictions.length,
    claims: claimRows.length - claims.length,
    curation: curationRows.length - curation.length,
  }
  invalidRows.total = Object.values(invalidRows).reduce((sum, value) => sum + value, 0)
  // Referenced runs are now inferred from the CLAIM rows alone. Until F7 the concept rows carried
  // their own run references and dominated this count; dropping that section is exactly why the
  // server's own `n_runs` stays the floor rather than being derivable from what is rendered.
  const knownRunIds = new Set([
    ...claims.flatMap(claim => claim.runs),
    ...contradictions.flatMap(claim => claim.runs),
  ].filter(Boolean))
  // Server totals describe the full ledger, but they can lag a projection during a concurrent append.
  // Never let a stale/smaller total hide valid rows already present in this response. Malformed rows do
  // not become evidence merely because an array contained them.
  const totals = {
    runs: Math.max(count(atlas.n_runs), knownRunIds.size),
    // Atlas and Claims are independently refreshed projections. Do not combine their adjacent totals
    // into one apparently atomic claim count; this panel renders the Claims endpoint's rows.
    claims: Math.max(count(claimsEnvelope.n), claims.length),
    contested: Math.max(count(atlas.n_contested), contradictions.length),
    curation: Math.max(count(curationEnvelope.n), curation.length),
  }
  return {
    totals,
    claimSource,
    contradictions,
    claims,
    curation,
    invalidRows,
    hiddenContradictions: Math.max(0, totals.contested - contradictions.length,
      rawContradictions.length - LEDGER_RENDER_LIMITS.contradictions),
    hiddenClaims: Math.max(0, totals.claims - claims.length,
      rawClaims.length - LEDGER_RENDER_LIMITS.claims),
    hiddenCuration: Math.max(0, totals.curation - curation.length,
      rawCuration.length - LEDGER_RENDER_LIMITS.curation),
    empty: totals.runs === 0 && totals.claims === 0
      && totals.contested === 0 && totals.curation === 0
      && contradictions.length === 0 && claims.length === 0 && curation.length === 0,
  }
}
