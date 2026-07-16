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
  claimChars: 1_600,
  verificationVerdicts: 64,
})

const {
  memos: MAX_MEMOS, collectionChars: COLLECTION_CHARS, memoChars: MEMO_CHARS,
  summaryChars: SUMMARY_CHARS, reasoningChars: REASONING_CHARS,
  findings: MAX_FINDINGS, findingChars: FINDING_CHARS,
  directions: MAX_DIRECTIONS, directionChars: DIRECTION_CHARS,
  sources: MAX_SOURCES, sourceTitleChars: SOURCE_TITLE_CHARS,
  sourceUrlChars: SOURCE_URL_CHARS, sourceSnippetChars: SOURCE_SNIPPET_CHARS,
  claimChars: CLAIM_CHARS, verificationVerdicts: MAX_VERDICTS,
} = RESEARCH_MEMO_LIMITS

const field = (value, key) => record(value) ? value[key] : undefined
const item = (value, index) => value[index]
const makeBudget = remaining => ({ remaining: Math.max(0, remaining) })

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
  const length = Math.min(arrayLength(rawVerdicts), MAX_VERDICTS)
  const verdicts = []
  const allowed = new Set(['supported', 'unsupported', 'unclear', 'cited'])
  for (let index = 0; index < length && budget.remaining > 0; index++) {
    const raw = item(rawVerdicts, index)
    if (!record(raw)) continue
    const candidate = text(field(raw, 'verdict'), 32, budget, true).toLowerCase()
    verdicts.push({
      statement: text(field(raw, 'statement'), CLAIM_CHARS, budget),
      verdict: allowed.has(candidate) ? candidate : 'unclear',
      note: text(field(raw, 'note'), 200, budget),
    })
  }
  if (!verdicts.length) return null
  return {
    verdicts,
    method: text(field(value, 'method'), 64, budget, true) || 'unknown',
    // Never trust an arbitrary aggregate that can disagree with the bounded visible verdicts.
    unsupported: verdicts.filter(verdict => verdict.verdict === 'unsupported').length,
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
    recommended_directions: directions,
    sources,
    at_node: Number.isSafeInteger(atNode) && atNode >= 0 ? atNode : null,
    trigger: '',
    verification: null,
  }
}

function normalizeResearchMemoFrom(value, budget) {
  const src = record(value) ? value : {}
  const summary = text(field(src, 'summary'), SUMMARY_CHARS, budget)
  const reasoning = text(field(src, 'reasoning'), REASONING_CHARS, budget)
  const trigger = text(field(src, 'trigger'), 64, budget, true)
  // Match the writer boundary's priority: retain conclusion/debug text before optional collections.
  const normalized = normalizeCollections(src, budget)
  normalized.summary = summary
  normalized.reasoning = reasoning
  normalized.trigger = trigger
  normalized.verification = normalizeVerification(field(src, 'verification'), budget)
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
    newestFirst.push({ ...normalizeResearchMemoFrom(raw, budget), sourceIndex: index })
  }
  const memos = newestFirst.reverse()
  return { memos, total, omitted: Math.max(0, total - memos.length) }
}
