// Pure projection for the virtual span tree. The server bounds how many spans may be transferred;
// this module keeps every one of those spans in the LOGICAL tree while the renderer mounts only a
// viewport-sized slice. Iterative traversal is deliberate: corrupt/custom traces can be much deeper
// than JavaScript's call stack even though JSON itself cannot contain an object cycle.

const list = value => Array.isArray(value) ? value : []
const text = value => value == null ? '' : String(value)
const SEARCH_KEYS = new Set(['name', 'kind', 'status', 'model', 'op', 'tool', 'phase', 'stage',
  'reason', 'error', 'error_reason', 'type', 'package', 'metric', 'ok', 'count', 'exit_code',
  'attempt', 'generation'])
const searchFields = value => Object.entries(value || {}).slice(0, 32).flatMap(([key, field]) =>
  SEARCH_KEYS.has(key.toLowerCase()) && (field == null || typeof field !== 'object')
    ? [key, text(field).slice(0, 128)] : [])

const searchText = span => {
  const events = list(span?.events).slice(0, 32).flatMap(event =>
    event && typeof event === 'object' ? searchFields(event) : [])
  return [span?.name, span?.kind, span?.status, ...searchFields(span?.attributes), ...events]
    .map(text).join('\n').slice(0, 2048).toLowerCase()
}

export function flattenSpanTree(roots) {
  const rows = []
  const used = new Set()
  const nextSuffix = new Map()
  const stack = []
  const push = (spans, parent, level, path) => {
    const children = list(spans).filter(span => span && typeof span === 'object')
    for (let index = children.length - 1; index >= 0; index -= 1) stack.push({
      span: children[index], parent, level, pos: index + 1, size: children.length,
      path: `${path}.${index}`,
    })
  }
  push(roots, -1, 1, 'root')
  while (stack.length) {
    const task = stack.pop(), span = task.span
    if (!span || typeof span !== 'object') continue
    const base = `span:${text(span.span_id) || task.path}`
    let key = base, suffix = nextSuffix.get(base) || 2
    while (used.has(key)) key = `${base}:${suffix++}`
    nextSuffix.set(base, suffix)
    used.add(key)
    const index = rows.length
    // `parent` is the exact logical row index; visual indentation follows aria-level rather than
    // pretending a sibling tool is a child of the preceding generation.
    rows.push({ key, span, parent: task.parent, level: task.level, pos: task.pos, size: task.size,
      search: searchText(span) })
    push(span.children, index, task.level + 1, task.path)
  }
  return rows
}

export const spanTreeMatches = (rows, query) => {
  const needle = text(query).trim().toLowerCase()
  if (!needle) return []
  const matches = []
  rows.forEach((row, index) => { if (row.search.includes(needle)) matches.push(index) })
  return matches
}
