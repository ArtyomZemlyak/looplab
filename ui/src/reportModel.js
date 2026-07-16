// Pure boundary for agent-authored report narratives. Deterministic report analysis still reads the
// folded run state; only this optional, untrusted narrative is projected into a bounded render shape.

export const REPORT_LIMITS = Object.freeze({
  totalChars: 64_000,
  headlineChars: 800,
  paragraphChars: 4_000,
  listItems: 32,
  listItemChars: 1_200,
  triggerChars: 64,
})

export const REPORT_LIST_FIELDS = Object.freeze([
  'what_worked', 'learnings', 'what_didnt', 'next_directions', 'caveats',
])

const {
  totalChars: TOTAL_CHARS, headlineChars: HEADLINE_CHARS, paragraphChars: PARAGRAPH_CHARS,
  listItems: MAX_LIST_ITEMS, listItemChars: LIST_ITEM_CHARS, triggerChars: TRIGGER_CHARS,
} = REPORT_LIMITS

const CONTROL_CHARS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g
const SINGLE_LINE_SPACE = /[\t\r\n]+/g

export const isAdvisoryRecord = value => value != null && typeof value === 'object' && !Array.isArray(value)
export const advisoryArrayLength = value => Array.isArray(value) ? value.length : 0

// A report can download and render source code, so a merely successful node-detail response is not
// enough: it must prove that it belongs to the champion currently shown. Historical reads also have
// to echo the exact snapshot fence or live code could be presented as old evidence.
export function normalizeReportNodeDetail(value, { nodeId, historySeq = null,
  expectedGeneration = null } = {}) {
  if (!isAdvisoryRecord(value) || value.id !== nodeId || typeof value.code !== 'string') return null
  if (historySeq != null && (value.historical_seq !== historySeq
      || value.historical_generation !== expectedGeneration)) return null
  return { code: value.code }
}

const field = (value, key) => isAdvisoryRecord(value) ? value[key] : undefined
const primitiveText = value => {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (typeof value === 'boolean') return String(value)
  return ''
}

export function boundedAdvisoryText(value, maximum, budget, singleLine = false) {
  const room = Math.min(Math.max(0, maximum), budget.remaining)
  if (!room) return ''
  // Work only over the prefix we can retain; regex-scanning a discarded megabyte is avoidable work.
  let clean = primitiveText(value).slice(0, room).replace(CONTROL_CHARS, '')
  if (singleLine) clean = clean.replace(SINGLE_LINE_SPACE, ' ')
  clean = clean.trim()
  budget.remaining = Math.max(0, budget.remaining - clean.length)
  return clean
}

function boundedList(value, budget) {
  // Some pre-structured reports persisted a list as one string. Keep that text as ONE item; treating
  // it as an iterable would render one <li> per character and can freeze the page.
  if (typeof value === 'string') {
    const legacy = boundedAdvisoryText(value, LIST_ITEM_CHARS, budget, true)
    return legacy ? [legacy] : []
  }
  if (!Array.isArray(value)) return []
  const length = Math.min(value.length, MAX_LIST_ITEMS)
  const out = []
  for (let index = 0; index < length && budget.remaining > 0; index++) {
    const clean = boundedAdvisoryText(value[index], LIST_ITEM_CHARS, budget, true)
    if (clean) out.push(clean)
  }
  return out
}

export function normalizeRunReport(value) {
  if (!isAdvisoryRecord(value)) return null
  const budget = { remaining: TOTAL_CHARS }
  const result = {
    headline: boundedAdvisoryText(field(value, 'headline'), HEADLINE_CHARS, budget, true),
    // `summary` is retained for old report events that predate verdict/champion_summary.
    summary: boundedAdvisoryText(field(value, 'summary'), PARAGRAPH_CHARS, budget),
    verdict: boundedAdvisoryText(field(value, 'verdict'), PARAGRAPH_CHARS, budget),
    champion_summary: boundedAdvisoryText(field(value, 'champion_summary'), PARAGRAPH_CHARS, budget),
  }
  // Trust-significant caveats get budget before ordinary narrative lists. Otherwise a valid, saturated
  // what_worked/learnings payload can erase the warning while its positive prose remains.
  result.caveats = boundedList(field(value, 'caveats'), budget)
  for (const key of REPORT_LIST_FIELDS) {
    if (key !== 'caveats') result[key] = boundedList(field(value, key), budget)
  }
  const atNode = field(value, 'at_node')
  result.at_node = Number.isSafeInteger(atNode) && atNode >= 0 ? atNode : null
  // Replay owns this publication metadata. Advisory prose exhausting its shared text budget must
  // never erase the authoritative trigger from the provenance strip or exports.
  result.trigger = boundedAdvisoryText(field(value, 'trigger'), TRIGGER_CHARS,
    { remaining: TRIGGER_CHARS }, true)
  const publishedSeq = field(value, 'published_seq')
  result.published_seq = Number.isSafeInteger(publishedSeq) && publishedSeq >= 0 ? publishedSeq : null
  const publishedAt = field(value, 'published_at')
  result.published_at = typeof publishedAt === 'number' && Number.isFinite(publishedAt)
    && publishedAt > 0 && publishedAt <= 253_402_300_799 ? publishedAt : null
  return result
}

// at_node is a bounded node-count watermark, not a full state-revision watermark. Equal counts prove
// node coverage only: confirmations, trust checks, resets, and other events may still have changed.
export function reportNarrativeCoverage(report, currentNodeCount) {
  const total = Number.isSafeInteger(currentNodeCount) && currentNodeCount >= 0
    ? currentNodeCount : null
  const atNode = report && Number.isSafeInteger(report.at_node) && report.at_node >= 0
    ? report.at_node : null
  const base = { status: report ? 'unknown' : 'absent', atNode, currentNodeCount: total, staleBy: null }
  if (!report || atNode == null || total == null) return base
  if (atNode < total) return { ...base, status: 'stale', staleBy: total - atNode }
  if (atNode > total) return { ...base, status: 'inconsistent' }
  // Equal counts prove only that every currently visible node was in scope. They do not prove
  // freshness for confirmations, trust checks, resets, or any other same-count event.
  return { ...base, status: 'node_count_matched' }
}

export function reportCoverageText(coverage) {
  if (!coverage || coverage.status === 'absent') return 'No agent narrative is published.'
  if (coverage.status === 'stale') {
    return `Covers ${coverage.atNode} of ${coverage.currentNodeCount} nodes · stale by ${coverage.staleBy} node${coverage.staleBy === 1 ? '' : 's'}.`
  }
  if (coverage.status === 'inconsistent') {
    return `Claims ${coverage.atNode} nodes, but this view has ${coverage.currentNodeCount} · inconsistent provenance.`
  }
  if (coverage.status === 'node_count_matched') {
    return `Covers ${coverage.atNode} of ${coverage.currentNodeCount} nodes · node coverage complete, not full state freshness.`
  }
  return coverage.currentNodeCount == null
    ? 'Node coverage unknown.'
    : `Node coverage unknown for ${coverage.currentNodeCount} current nodes.`
}
