import {
  advisoryArrayLength as arrayLength,
  boundedAdvisoryText as text,
  isAdvisoryRecord as record,
} from './reportModel.js'

// Pure, defensive projection for deep-research sidecars. The event fold normally canonicalizes
// these values, but old snapshots, partial upgrades, and mocked providers can still hand the UI a
// wrong-shaped or enormous payload. Keep iteration and rendered text bounded before React sees it.

export const RESEARCH_MEMO_LIMITS = Object.freeze({
  memos: 32,
  collectionChars: 128_000,
  memoChars: 64_000,
  summaryChars: 4_000,
  reasoningChars: 12_000,
  findings: 32,
  findingChars: 1_200,
  directions: 16,
  directionChars: 1_200,
  sources: 64,
  sourceTitleChars: 400,
  sourceUrlChars: 1_600,
  sourceSnippetChars: 200,
  claims: 64,
  claimChars: 1_600,
  claimNodeRefs: 8,
  claimUrlRefs: 4,
  advisoryIdChars: 160,
  verificationVerdicts: 64,
})

const {
  memos: MAX_MEMOS, collectionChars: COLLECTION_CHARS, memoChars: MEMO_CHARS,
  summaryChars: SUMMARY_CHARS, reasoningChars: REASONING_CHARS,
  findings: MAX_FINDINGS, findingChars: FINDING_CHARS,
  directions: MAX_DIRECTIONS, directionChars: DIRECTION_CHARS,
  sources: MAX_SOURCES, sourceTitleChars: SOURCE_TITLE_CHARS,
  sourceUrlChars: SOURCE_URL_CHARS, sourceSnippetChars: SOURCE_SNIPPET_CHARS,
  claims: MAX_CLAIMS, claimChars: CLAIM_CHARS, claimNodeRefs: MAX_CLAIM_NODES,
  claimUrlRefs: MAX_CLAIM_URLS, advisoryIdChars: ADVISORY_ID_CHARS,
  verificationVerdicts: MAX_VERDICTS,
} = RESEARCH_MEMO_LIMITS

const field = (value, key) => record(value) ? value[key] : undefined
const item = (value, index) => value[index]
const makeBudget = remaining => ({ remaining: Math.max(0, remaining) })
const SOURCE_IDENTITY_RE = /^http-sha256:[0-9a-f]{64}$/
// Text is not the only retained cost: each verdict also gains a bounded evidence envelope and
// alignment receipt. Charge that fixed shape to the shared budget so a memo full of maximum-length
// verifier rows stays near `memoChars` instead of exceeding it by one metadata object per row.
const VERDICT_METADATA_CHARS = 256
const VERDICT_NODE_REF_CHARS = 64
const VERDICT_URL_IDENTITY_CHARS = 80

function textList(value, maximum, textMaximum, budget) {
  const length = Math.min(arrayLength(value), maximum)
  const out = []
  for (let index = 0; index < length && budget.remaining > 0; index++) {
    const clean = text(item(value, index), textMaximum, budget, true)
    if (clean) out.push(clean)
  }
  return out
}

function normalizeVerification(value, budget) {
  if (!record(value)) return null
  const rawVerdicts = field(value, 'verdicts')
  const rawTotal = arrayLength(rawVerdicts)
  const declaredTotal = field(value, 'total_verdicts')
  const declaredOmitted = field(value, 'omitted_verdicts')
  // Backend writer and replay sanitizers may already have capped the rows. Carry their omission
  // receipt only when both counters are internally consistent; otherwise derive from visible input.
  const receiptKnown = Number.isSafeInteger(declaredTotal) && declaredTotal >= rawTotal
    && declaredOmitted === declaredTotal - rawTotal
  const totalVerdicts = receiptKnown ? declaredTotal : rawTotal
  const length = Math.min(rawTotal, MAX_VERDICTS)
  const verdicts = []
  const allowed = new Set(['supported', 'unsupported', 'unclear', 'cited'])
  for (let index = 0; index < length && budget.remaining > 0; index++) {
    const raw = item(rawVerdicts, index)
    if (!record(raw)) continue
    const candidate = text(field(raw, 'verdict'), 32, budget, true).toLowerCase()
    const rawEvidence = field(raw, 'evidence')
    const rawNodeRefs = field(rawEvidence, 'node_refs')
    const rawUrlIdentities = field(rawEvidence, 'url_identities')
    const nodeRefs = []
    const seenNodes = new Set()
    const rawNodeTotal = arrayLength(rawNodeRefs)
    const nodeLength = Math.min(rawNodeTotal, MAX_CLAIM_NODES)
    let nodeRefsValid = Array.isArray(rawNodeRefs) && rawNodeTotal <= MAX_CLAIM_NODES
    for (let nodeIndex = 0; nodeIndex < nodeLength; nodeIndex++) {
      const ref = item(rawNodeRefs, nodeIndex)
      const nodeId = field(ref, 'node_id')
      const generation = field(ref, 'generation')
      const identity = `${nodeId}:${generation}`
      if (!Number.isSafeInteger(nodeId) || nodeId < 0
          || !Number.isSafeInteger(generation) || generation < 0 || seenNodes.has(identity)) {
        nodeRefsValid = false
        continue
      }
      seenNodes.add(identity)
      nodeRefs.push({ node_id: nodeId, generation })
    }
    const urlIdentities = []
    const seenUrls = new Set()
    const rawUrlTotal = arrayLength(rawUrlIdentities)
    const urlLength = Math.min(rawUrlTotal, MAX_CLAIM_URLS)
    let urlIdentitiesValid = Array.isArray(rawUrlIdentities) && rawUrlTotal <= MAX_CLAIM_URLS
    for (let urlIndex = 0; urlIndex < urlLength; urlIndex++) {
      const identity = item(rawUrlIdentities, urlIndex)
      if (typeof identity !== 'string' || !SOURCE_IDENTITY_RE.test(identity)
          || seenUrls.has(identity)) {
        urlIdentitiesValid = false
        continue
      }
      seenUrls.add(identity)
      urlIdentities.push(identity)
    }
    const evidenceComplete = record(rawEvidence) && field(rawEvidence, 'v') === 1
      && field(rawEvidence, 'complete') === true
      && nodeRefsValid && nodeRefs.length === rawNodeTotal
      && urlIdentitiesValid && urlIdentities.length === rawUrlTotal
    verdicts.push({
      statement: text(field(raw, 'statement'), CLAIM_CHARS, budget),
      verdict: allowed.has(candidate) ? candidate : 'unclear',
      note: text(field(raw, 'note'), 200, budget),
      evidence: { node_refs: nodeRefs, url_identities: urlIdentities, complete: evidenceComplete },
    })
    budget.remaining = Math.max(0, budget.remaining - VERDICT_METADATA_CHARS
      - nodeRefs.length * VERDICT_NODE_REF_CHARS
      - urlIdentities.length * VERDICT_URL_IDENTITY_CHARS)
  }
  const omittedVerdicts = Math.max(0, totalVerdicts - verdicts.length)
  if (!verdicts.length && !omittedVerdicts) return null
  return {
    verdicts,
    method: text(field(value, 'method'), 64, budget, true) || 'unknown',
    // Never trust an arbitrary aggregate that can disagree with the bounded visible verdicts.
    unsupported: verdicts.filter(verdict => verdict.verdict === 'unsupported').length,
    totalVerdicts,
    omittedVerdicts,
    receiptKnown,
  }
}

function receiptCount(value, visibleTotal, prefix = '') {
  if (!record(value) || field(value, 'v') !== 1) {
    return { total: visibleTotal, omitted: 0, valid: false }
  }
  const total = field(value, `${prefix}total`)
  const retained = field(value, `${prefix}retained`)
  const omitted = field(value, `${prefix}omitted`)
  if (!Number.isSafeInteger(total) || total < visibleTotal || retained !== visibleTotal
      || omitted !== total - retained) {
    return { total: visibleTotal, omitted: 0, valid: false }
  }
  return { total, omitted, valid: true }
}

function normalizeClaims(value, receipt, budget) {
  const rawTotal = arrayLength(value)
  const claimsShapeKnown = value == null || Array.isArray(value)
  const claimsReceipt = receiptCount(receipt, rawTotal)
  const total = claimsReceipt.total
  const length = Math.min(rawTotal, MAX_CLAIMS)
  const claims = []
  for (let index = 0; index < length && budget.remaining > 0; index++) {
    const raw = item(value, index)
    if (!record(raw)) continue
    const statement = text(field(raw, 'statement'), CLAIM_CHARS, budget)
    const rawNodes = field(raw, 'node_ids')
    const nodesLength = Math.min(arrayLength(rawNodes), MAX_CLAIM_NODES)
    const nodeIds = []
    for (let nodeIndex = 0; nodeIndex < nodesLength; nodeIndex++) {
      const nodeId = item(rawNodes, nodeIndex)
      if (Number.isSafeInteger(nodeId) && nodeId >= 0) nodeIds.push(nodeId)
    }
    const rawUrls = field(raw, 'urls')
    const rawUrlTotal = arrayLength(rawUrls)
    const urlsLength = Math.min(rawUrlTotal, MAX_CLAIM_URLS)
    const urls = []
    for (let urlIndex = 0; urlIndex < urlsLength && budget.remaining > 0; urlIndex++) {
      const url = text(item(rawUrls, urlIndex), SOURCE_URL_CHARS, budget, true)
      if (url) urls.push(url)
    }
    const rawUrlIdentities = field(raw, 'url_identities')
    const rawUrlIdentityTotal = arrayLength(rawUrlIdentities)
    const urlIdentityLength = Math.min(rawUrlIdentityTotal, MAX_CLAIM_URLS)
    const urlIdentities = []
    let sourceIdentitiesComplete = rawUrlTotal === 0
      ? rawUrlIdentities == null || (Array.isArray(rawUrlIdentities) && rawUrlIdentityTotal === 0)
      : Array.isArray(rawUrlIdentities) && rawUrlIdentityTotal === rawUrlTotal
        && rawUrlIdentityTotal <= MAX_CLAIM_URLS
    for (let identityIndex = 0; identityIndex < urlIdentityLength; identityIndex++) {
      const identity = item(rawUrlIdentities, identityIndex)
      if (typeof identity !== 'string' || !SOURCE_IDENTITY_RE.test(identity)) {
        sourceIdentitiesComplete = false
        continue
      }
      urlIdentities.push(identity)
    }
    sourceIdentitiesComplete = sourceIdentitiesComplete
      && urlIdentities.length === rawUrlIdentityTotal && urls.length === rawUrlTotal
    const evidenceReceipt = field(raw, 'evidence_receipt')
    const nodeReceipt = receiptCount(evidenceReceipt, arrayLength(rawNodes), 'node_refs_')
    const urlReceipt = receiptCount(evidenceReceipt, arrayLength(rawUrls), 'url_refs_')
    const nodeTotal = nodeReceipt.total
    const urlTotal = urlReceipt.total
    const claimId = text(field(raw, 'claim_id'), ADVISORY_ID_CHARS, budget, true)
    if (statement || nodeIds.length || urls.length) claims.push({
      statement,
      node_ids: nodeIds,
      urls,
      url_identities: urlIdentities,
      claim_id: claimId,
      evidence: {
        nodeTotal,
        urlTotal,
        omitted: Math.max(0, nodeTotal - nodeIds.length) + Math.max(0, urlTotal - urls.length),
        complete: nodeReceipt.valid && urlReceipt.valid
          && field(evidenceReceipt, 'complete') === true
          && nodeReceipt.omitted === 0 && urlReceipt.omitted === 0
          && nodeTotal === nodeIds.length && urlTotal === urls.length,
        sourceIdentitiesComplete,
      },
    })
  }
  return {
    claims,
    total,
    omitted: Math.max(0, total - claims.length),
    receiptKnown: claimsReceipt.valid,
    complete: claimsShapeKnown && claimsReceipt.valid && claimsReceipt.omitted === 0
      && total === claims.length && field(receipt, 'complete') === true,
  }
}

function uniqueValues(values) {
  const seen = new Set()
  const out = []
  for (const value of values) {
    if (seen.has(value)) continue
    seen.add(value)
    out.push(value)
  }
  return out
}

function sameValues(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

function alignVerification(verification, claimProjection) {
  if (!verification) return null
  const claims = claimProjection.claims
  const totalsMatch = claimProjection.receiptKnown && verification.receiptKnown
    && claimProjection.total === verification.totalVerdicts
  const rowsMatch = claims.length === verification.verdicts.length
  const verdicts = verification.verdicts.map((verdict, index) => {
    const claim = claims[index]
    const statementMatches = !!claim && claim.statement === verdict.statement
    const expectedNodeIds = claim ? uniqueValues(claim.node_ids) : []
    const verifierNodeIds = verdict.evidence.node_refs.map(ref => ref.node_id)
    const nodeIdsMatch = !!claim && sameValues(expectedNodeIds, verifierNodeIds)
    const expectedSourceIdentities = claim?.evidence?.sourceIdentitiesComplete
      ? uniqueValues(claim.url_identities) : null
    const sourceIdentitiesMatch = expectedSourceIdentities != null
      && sameValues(expectedSourceIdentities, verdict.evidence.url_identities)
    const supportedEvidencePresent = verdict.verdict !== 'supported'
      || verdict.evidence.node_refs.length > 0 || verdict.evidence.url_identities.length > 0
    return {
      ...verdict,
      alignment: {
        statementMatches,
        nodeIdsMatch,
        sourceIdentitiesMatch,
        supportedEvidencePresent,
        complete: statementMatches && nodeIdsMatch && sourceIdentitiesMatch
          && supportedEvidencePresent,
      },
    }
  })
  const complete = totalsMatch && rowsMatch
    && verdicts.every(verdict => verdict.alignment.complete)
  return {
    ...verification,
    verdicts,
    alignment: { complete, totalsMatch, rowsMatch },
  }
}

function normalizeCollections(value, budget) {
  const src = record(value) ? value : {}
  const findings = textList(field(src, 'findings'), MAX_FINDINGS, FINDING_CHARS, budget)
  const directions = textList(field(src, 'recommended_directions'), MAX_DIRECTIONS,
    DIRECTION_CHARS, budget)

  const sources = []
  const rawSources = field(src, 'sources')
  const sourceLength = Math.min(arrayLength(rawSources), MAX_SOURCES)
  for (let index = 0; index < sourceLength && budget.remaining > 0; index++) {
    const raw = item(rawSources, index)
    if (!record(raw)) continue
    const source = {
      title: text(field(raw, 'title'), SOURCE_TITLE_CHARS, budget, true),
      url: text(field(raw, 'url'), SOURCE_URL_CHARS, budget, true),
      snippet: text(field(raw, 'snippet'), SOURCE_SNIPPET_CHARS, budget),
    }
    if (source.title || source.url || source.snippet) sources.push(source)
  }

  const atNode = field(src, 'at_node')
  return {
    summary: '',
    reasoning: '',
    findings,
    claims: [],
    claimsTotal: 0,
    claimsOmitted: 0,
    claimsComplete: true,
    recommended_directions: directions,
    sources,
    at_node: Number.isSafeInteger(atNode) && atNode >= 0 ? atNode : null,
    trigger: '',
    verification: null,
    memo_id: '',
  }
}

function normalizeResearchMemoFrom(value, budget) {
  const src = record(value) ? value : {}
  const summary = text(field(src, 'summary'), SUMMARY_CHARS, budget)
  const trigger = text(field(src, 'trigger'), 64, budget, true)
  // Project verifier evidence before debug prose and optional collections. A saturated memo must
  // never retain recommendations while silently dropping its unsupported-claim warning.
  const verification = normalizeVerification(field(src, 'verification'), budget)
  // Claims are the memo's evidence-bearing assertions. Retain them before ordinary narrative so the
  // UI can connect verifier verdicts to cited experiments/sources. Debug reasoning is deliberately
  // last: it must never erase findings, next actions, or the consulted-source ledger.
  const claimProjection = normalizeClaims(field(src, 'claims'), field(src, 'claims_receipt'), budget)
  const normalized = normalizeCollections(src, budget)
  const reasoning = text(field(src, 'reasoning'), REASONING_CHARS, budget)
  normalized.summary = summary
  normalized.reasoning = reasoning
  normalized.trigger = trigger
  normalized.verification = alignVerification(verification, claimProjection)
  normalized.claims = claimProjection.claims
  normalized.claimsTotal = claimProjection.total
  normalized.claimsOmitted = claimProjection.omitted
  normalized.claimsComplete = claimProjection.complete
  normalized.memo_id = text(field(src, 'memo_id'), ADVISORY_ID_CHARS, budget, true)
  return normalized
}

export function normalizeResearchMemo(value) {
  return normalizeResearchMemoFrom(value, makeBudget(MEMO_CHARS))
}

export function normalizeResearchMemos(value) {
  const total = arrayLength(value)
  if (!total) return { memos: [], total: 0, omitted: 0 }
  const start = Math.max(0, total - MAX_MEMOS)
  const budget = makeBudget(COLLECTION_CHARS)
  const newestFirst = []
  // Allocate the shared budget newest-first so an old huge memo cannot hide the latest conclusion.
  for (let index = total - 1; index >= start && budget.remaining > 0; index--) {
    const raw = item(value, index)
    if (!record(raw)) continue
    const allowance = Math.min(MEMO_CHARS, budget.remaining)
    const memoBudget = makeBudget(allowance)
    const memo = normalizeResearchMemoFrom(raw, memoBudget)
    budget.remaining -= allowance - memoBudget.remaining
    newestFirst.push({ ...memo, sourceIndex: index })
  }
  const memos = newestFirst.reverse()
  return { memos, total, omitted: Math.max(0, total - memos.length) }
}


// "Format the deep research properly, it's a wall of text." It is — and precisely one element is
// responsible. The memo card's COLLAPSED header renders `summary` verbatim in a single span, and a
// real memo's summary is ~1,600 characters (measured on the live run, 2026-08-12). So the list of
// memos is a stack of paragraphs with no scannable line, while everything else on the card is
// already structured: findings as a list, claims with verdicts, next actions with steer buttons,
// sources and reasoning behind disclosures.
//
// The header gets a LEAD — the first sentence, bounded — and the full text moves into the body's
// Conclusion section, where a paragraph belongs. Nothing is dropped; it stops being the headline.
export const MEMO_LEAD_CHARS = 180

export function memoLead(summary, { limit = MEMO_LEAD_CHARS } = {}) {
  const text = String(summary || '').replace(/\s+/g, ' ').trim()
  if (!text) return ''
  // First sentence, when there is one worth calling a sentence. `.` followed by a space and a
  // capital — not any period, or "0.8835 recall" and "vs. the baseline" each end the headline.
  const sentence = /^(.{20,}?[.!?])\s+[A-Z(“"']/.exec(text)
  const lead = sentence ? sentence[1] : text
  if (lead.length <= limit) return lead
  // Cut on a word boundary so the ellipsis never lands mid-token.
  const cut = lead.slice(0, limit)
  const space = cut.lastIndexOf(' ')
  return `${(space > limit * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`
}

/** Did the header have to shorten anything? Drives the "full conclusion below" affordance. */
export const memoLeadIsPartial = (summary) => {
  const text = String(summary || '').replace(/\s+/g, ' ').trim()
  return !!text && memoLead(text) !== text
}
