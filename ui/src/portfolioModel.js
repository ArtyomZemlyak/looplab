const CONTROL = /[\u0000-\u001f\u007f]/
const CONTROL_GLOBAL = /[\u0000-\u001f\u007f]/g
const MAX_VIEWS = 12
const MAX_COMPARE_RUNS = 8

export const COMPARE_COLUMNS = Object.freeze([
  ['status', 'Status'],
  ['task', 'Task'],
  ['best', 'Best metric'],
  ['objective', 'Objective'],
  ['nodes', 'Nodes'],
  ['eval', 'Eval time'],
  ['cost', 'LLM cost'],
  ['trust', 'Trust gate'],
  ['champion', 'Champion'],
  ['project', 'Project'],
  ['supertask', 'Super-task'],
  ['updated', 'Updated'],
])

export const DEFAULT_COMPARE_COLUMNS = Object.freeze([
  'status', 'task', 'best', 'objective', 'nodes', 'eval', 'cost', 'trust', 'champion',
])

const COLUMN_IDS = new Set(COMPARE_COLUMNS.map(([id]) => id))
const SORTS = new Set(['time', 'name', 'metric', 'task', 'nodes', 'phase'])
const STATUSES = new Set([
  'all', 'running', 'finalizing', 'paused', 'approval', 'stalled', 'unknown', 'finished',
])
const scalarText = (value, fallback, maximum = 200) =>
  typeof value === 'string' && value.length <= maximum && !CONTROL.test(value) ? value : fallback

export function normalizeCompareColumns(value) {
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch { value = null }
  }
  if (!Array.isArray(value)) return [...DEFAULT_COMPARE_COLUMNS]
  return [...new Set(value.filter(id => typeof id === 'string' && COLUMN_IDS.has(id)))]
}

function normalizeView(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const name = scalarText(typeof value.name === 'string' ? value.name.trim() : '', '', 48)
  if (!name) return null
  const compare = Array.isArray(value.compare)
    ? [...new Set(value.compare.map(id => scalarText(id, '')).filter(Boolean))].slice(0, MAX_COMPARE_RUNS)
    : []
  return {
    name,
    project: scalarText(value.project, '__all__'),
    query: scalarText(value.query, '', 240),
    task: scalarText(value.task, '__all__'),
    status: STATUSES.has(value.status) ? value.status : 'all',
    supertask: scalarText(value.supertask, '__all__'),
    sort: SORTS.has(value.sort) ? value.sort : 'time',
    direction: value.direction === 'asc' ? 'asc' : 'desc',
    view: value.view === 'map' || (value.view === 'compare' && compare.length > 1)
      ? value.view : 'list',
    compare,
    columns: normalizeCompareColumns(value.columns),
  }
}

export function decodePortfolioViews(raw) {
  try {
    const value = typeof raw === 'string' ? JSON.parse(raw) : raw
    if (!Array.isArray(value)) return []
    const names = new Set()
    return value.slice(0, MAX_VIEWS * 2).map(normalizeView).filter(view => {
      if (!view || names.has(view.name)) return false
      names.add(view.name); return true
    }).slice(0, MAX_VIEWS)
  } catch { return [] }
}

export function upsertPortfolioView(views, name, state) {
  const view = normalizeView({ ...state, name })
  if (!view) return decodePortfolioViews(views)
  return [view, ...decodePortfolioViews(views).filter(item => item.name !== view.name)]
    .slice(0, MAX_VIEWS)
}

export const portfolioViewSignature = view => {
  const normalized = normalizeView({ ...view, name: 'view' })
  if (!normalized) return ''
  delete normalized.name
  return JSON.stringify(normalized)
}

const configSignature = (config, key) => {
  if (!Object.prototype.hasOwnProperty.call(config, key)) return 'missing'
  try { return `value:${JSON.stringify(config[key])}` } catch { return 'invalid' }
}

const displayConfigValue = (config, key) => {
  if (!Object.prototype.hasOwnProperty.call(config, key)) return '—'
  const value = config[key]
  if (value == null) return String(value)
  let text
  try { text = typeof value === 'string' ? value : JSON.stringify(value) } catch { return 'unavailable' }
  text = text.replace(CONTROL_GLOBAL, ' ').replace(/\s+/g, ' ')
  return text.length > 160 ? `${text.slice(0, 157)}…` : text
}

export function configDifferences(items, maximum = 160) {
  const available = items.filter(item => item?.config && typeof item.config === 'object')
  if (available.length < 2) return { total: 0, rows: [] }
  const keys = [...new Set(available.flatMap(item => Object.keys(item.config)))]
    .filter(key => key !== '_looplab_config_meta').sort()
  const different = keys.filter(key =>
    new Set(available.map(item => configSignature(item.config, key))).size > 1)
  return {
    total: different.length,
    rows: different.slice(0, Math.max(0, maximum)).map(key => ({
      key,
      values: items.map(item => item?.config ? displayConfigValue(item.config, key) : 'unavailable'),
    })),
  }
}

const metricObservation = run => {
  if (run?.best_confirmed != null) return {
    runId: run.run_id,
    phase: typeof run.best_confirmed === 'number' && Number.isFinite(run.best_confirmed)
      ? 'confirmed' : 'invalid',
    value: typeof run.best_confirmed === 'number' && Number.isFinite(run.best_confirmed)
      ? run.best_confirmed : null,
  }
  if (run?.best_metric != null) return {
    runId: run.run_id,
    phase: typeof run.best_metric === 'number' && Number.isFinite(run.best_metric)
      ? 'raw' : 'invalid',
    value: typeof run.best_metric === 'number' && Number.isFinite(run.best_metric)
      ? run.best_metric : null,
  }
  return { runId: run?.run_id, phase: 'missing', value: null }
}

export function comparableRunRanking(runs = []) {
  const task = runs[0]?.task_id
  const direction = runs[0]?.direction
  const observations = runs.map(metricObservation)
  const result = (status, extra = {}) => ({
    status,
    taskId: task || null,
    direction: ['min', 'max'].includes(direction) ? direction : null,
    phase: null,
    observations,
    bestValue: null,
    bestRunIds: [],
    ...extra,
  })
  if (runs.length < 2) return result('insufficient-population')
  if (!task || !['min', 'max'].includes(direction)
      || runs.some(run => run.task_id !== task || run.direction !== direction)) {
    return result('incompatible')
  }
  if (observations.some(item => item.phase === 'missing' || item.phase === 'invalid')) {
    return result('missing-metric')
  }
  const phases = new Set(observations.map(item => item.phase))
  if (phases.size !== 1) return result('mixed-phase')
  const phase = observations[0].phase
  const values = observations.map(item => item.value)
  const bestValue = direction === 'max' ? Math.max(...values) : Math.min(...values)
  return result('ranked', {
    phase,
    bestValue,
    bestRunIds: observations.filter(item => item.value === bestValue).map(item => item.runId),
  })
}

export function bestComparableRuns(runs = []) {
  const ids = new Set(comparableRunRanking(runs).bestRunIds)
  return runs.filter(run => ids.has(run.run_id))
}

// Compatibility helper for callers that explicitly need one representative. UI ranking must use
// comparableRunRanking/bestComparableRuns so selection order never turns an exact tie into a winner.
export function bestComparableRun(runs = []) {
  return bestComparableRuns(runs)[0] || null
}
