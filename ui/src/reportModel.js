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
  result.trigger = boundedAdvisoryText(field(value, 'trigger'), TRIGGER_CHARS, budget, true)
  return result
}
