// The research-memo questions the board does NOT hold — the browser half of the server's derived
// read model (`looplab/serve/memo_questions.py`, published on the state payload as
// `memo_questions`). The server decides WHICH questions are missing (the board's own admission
// key); this module only reads that answer defensively, so a frame from an older server (no key),
// a junk row or a truncated list never throws and never invents a question.
//
// Nothing here files anything. Filing is the explicit `hypothesis_added` command the banner sends,
// which is why every row keeps the memo-authored concepts verbatim: the filed card must carry the
// same scope the memo gave the question.

const _text = (value, max = 500) => (typeof value === 'string' ? value.trim().slice(0, max) : '')
const _count = value => (Number.isInteger(value) && value >= 0 ? value : 0)

export const MEMO_QUESTION_BANNER_ROWS = 64

export function memoQuestionRows(projection) {
  const rows = projection && typeof projection === 'object' && Array.isArray(projection.rows)
    ? projection.rows : []
  const out = []
  const seen = new Set()
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const statement = _text(row.statement)
    if (!statement) continue
    const key = statement.toLowerCase().split(/\s+/).join(' ')
    if (seen.has(key)) continue
    seen.add(key)
    const concepts = Array.isArray(row.concepts)
      ? [...new Set(row.concepts.map(tag => _text(tag, 160)).filter(Boolean))].slice(0, 16) : []
    out.push({
      id: _text(row.id, 256) || key,
      statement,
      concepts,
      timesAsked: Math.max(1, _count(row.times_asked)),
      memos: Array.isArray(row.memo_ids) ? row.memo_ids.filter(id => typeof id === 'string').length : 0,
      parentStatement: _text(row.parent_statement),
      parentOnBoard: row.parent_on_board === true,
      lastAtNode: Number.isInteger(row.last_at_node) ? row.last_at_node : null,
    })
    if (out.length >= MEMO_QUESTION_BANNER_ROWS) break
  }
  return out
}

// The banner's headline numbers. `missing` is the server's exact count, which may exceed the rows
// it published (`truncated`); the banner says so rather than letting the list read as complete.
export function memoQuestionSummary(projection) {
  const rows = memoQuestionRows(projection)
  const missing = projection && typeof projection === 'object'
    ? Math.max(_count(projection.not_on_board), rows.length) : rows.length
  return {
    rows,
    missing,
    asked: projection && typeof projection === 'object' ? _count(projection.asked) : 0,
    truncated: missing > rows.length,
  }
}

// The exact command payload for one row: the memo's own concepts ride along, an empty list is
// left out (absent means "not stated", an empty list would claim "no concepts").
export function memoQuestionCommand(row) {
  return {
    statement: row.statement,
    source: 'research_memo',
    ...(row.concepts.length ? { concepts: row.concepts } : {}),
  }
}
