// Match the original source. Lowercasing a second copy can expand Unicode characters and
// move offsets, or create characters absent from the source. Queries remain literal.
export function codeSearchRows(rows, query) {
  const pattern = query
    ? new RegExp(query.replace(/[$.*+?^{}()|[\]\\]/g, '\\$&'), 'giu') : null
  let matches = 0
  const searched = rows.map(row => {
    const text = String(row.line ?? row.l ?? '')
    const ranges = pattern ? [...text.matchAll(pattern)].map(match => ({
      from: match.index, to: match.index + match[0].length,
    })) : []
    if (ranges.length) matches += 1
    return { row, text, ranges }
  })
  return { rows: searched, matches }
}
