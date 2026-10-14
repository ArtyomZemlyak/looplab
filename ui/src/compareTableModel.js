// Experiments × parameters × metrics (UI review 2026-10-14 section 7.7) — the pure half of
// `CompareTable.jsx`. The two-node Compare shows one pair; this is the MLflow-style table over ANY
// number of experiments of the run: one row per experiment, one column per declared parameter, per
// effective-config key (what the attempt actually ran with, doc 79 §6) and per metric.
//
// The rules the table keeps: a value an experiment does not have is ABSENT (shown empty), never
// zero; an idea parameter and an effective-config value are different columns even when they name
// the same thing (one is what was asked, the other what ran); a metric column carries the
// direction only when the run recorded one; and "only differing" hides a column only when every
// SELECTED row has the same value — absence counts as a value.

export const COMPARE_TABLE_MAX_ROWS = 200
export const COMPARE_TABLE_MAX_COLUMNS = 120

const scalar = value => value === null || ['string', 'number', 'boolean'].includes(typeof value)
const key = value => (value === undefined ? '\u0000absent' : JSON.stringify(value))

export function compareRows(state, { nodeIds = null, effectiveConfigs = null } = {}) {
  const nodes = state?.nodes && typeof state.nodes === 'object' ? state.nodes : {}
  const ids = (nodeIds || Object.keys(nodes)).map(Number).filter(id => Number.isSafeInteger(id) && nodes[id])
    .sort((a, b) => a - b).slice(0, COMPARE_TABLE_MAX_ROWS)
  return ids.map(id => {
    const node = nodes[id]
    const params = node.idea?.params && typeof node.idea.params === 'object' ? node.idea.params : {}
    const extras = node.extra_metrics && typeof node.extra_metrics === 'object' ? node.extra_metrics : {}
    const attempt = Number.isSafeInteger(node.attempt) ? node.attempt : 0
    const effective = effectiveConfigs?.get?.(`${id}:${attempt}`)?.effective || null
    return {
      id, attempt,
      status: typeof node.status === 'string' ? node.status : '',
      operator: typeof node.operator === 'string' ? node.operator : '',
      metric: typeof node.metric === 'number' && Number.isFinite(node.metric) ? node.metric : undefined,
      params: Object.fromEntries(Object.entries(params).filter(([, v]) => scalar(v))),
      config: effective && typeof effective === 'object'
        ? Object.fromEntries(Object.entries(effective).filter(([, v]) => scalar(v))) : {},
      metrics: Object.fromEntries(Object.entries(extras)
        .filter(([, v]) => typeof v === 'number' && Number.isFinite(v))),
    }
  })
}

export function compareColumns(rows, { onlyDiffering = false, metricFilter = '' } = {}) {
  const groups = { params: new Set(), config: new Set(), metrics: new Set() }
  for (const row of rows) for (const group of Object.keys(groups)) {
    for (const name of Object.keys(row[group])) groups[group].add(name)
  }
  const needle = metricFilter.trim().toLowerCase()
  const columns = []
  for (const [group, names] of Object.entries(groups)) {
    for (const name of [...names].sort()) {
      if (group === 'metrics' && needle && !name.toLowerCase().includes(needle)) continue
      if (onlyDiffering && new Set(rows.map(row => key(row[group][name]))).size <= 1) continue
      columns.push({ group, name })
    }
  }
  return { columns: columns.slice(0, COMPARE_TABLE_MAX_COLUMNS),
    hidden: Math.max(0, columns.length - COMPARE_TABLE_MAX_COLUMNS) }
}

export function sortRows(rows, column, descending = false) {
  if (!column) return rows
  const value = row => column.group === 'metric' ? row.metric
    : column.group === 'id' ? row.id : row[column.group]?.[column.name]
  return [...rows].sort((a, b) => {
    const x = value(a), y = value(b)
    if (x === undefined && y === undefined) return a.id - b.id
    if (x === undefined) return 1                   // absent sorts last, in either direction
    if (y === undefined) return -1
    const order = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))
    return (descending ? -order : order) || a.id - b.id
  })
}

const csvCell = value => {
  if (value === undefined || value === null) return ''
  const text = String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

export function compareCsv(rows, columns) {
  const header = ['experiment', 'attempt', 'status', 'metric',
    ...columns.map(column => `${column.group}:${column.name}`)]
  const lines = [header.map(csvCell).join(',')]
  for (const row of rows) {
    lines.push([row.id, row.attempt, row.status, row.metric,
      ...columns.map(column => row[column.group][column.name])].map(csvCell).join(','))
  }
  return lines.join('\n') + '\n'
}
