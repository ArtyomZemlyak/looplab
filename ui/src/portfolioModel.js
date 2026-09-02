const CONTROL = /[\u0000-\u001f\u007f]/
const CONTROL_GLOBAL = /[\u0000-\u001f\u007f]/g
export const MAX_PORTFOLIO_VIEWS = 12
export const MAX_PORTFOLIO_VIEW_NAME_LENGTH = 48
export const MAX_PORTFOLIO_QUERY_LENGTH = 240
export const MAX_PORTFOLIO_SCOPE_ID_LENGTH = 500
export const MAX_PORTFOLIO_RUN_ID_LENGTH = 255
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
// Python's producer-side identity bounds count Unicode code points; `[...value]` keeps the browser
// validator on the same metric instead of rejecting an otherwise valid astral-character id.
const scalarText = (value, fallback, maximum = MAX_PORTFOLIO_SCOPE_ID_LENGTH) =>
  typeof value === 'string' && [...value].length <= maximum && !CONTROL.test(value) ? value : fallback

export function normalizeCompareColumns(value) {
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch { value = null }
  }
  if (!Array.isArray(value)) return [...DEFAULT_COMPARE_COLUMNS]
  return [...new Set(value.filter(id => typeof id === 'string' && COLUMN_IDS.has(id)))]
}

function normalizeView(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const name = scalarText(typeof value.name === 'string' ? value.name.trim() : '', '',
    MAX_PORTFOLIO_VIEW_NAME_LENGTH)
  if (!name) return null
  const task = scalarText(value.task, '__all__', MAX_PORTFOLIO_SCOPE_ID_LENGTH)
  const exactAllTask = task === '__all__' && value.task === '__all__' && value.taskExact === true
  const compare = Array.isArray(value.compare)
    ? [...new Set(value.compare.map(id => scalarText(id, '', MAX_PORTFOLIO_RUN_ID_LENGTH))
      .filter(Boolean))].slice(0, MAX_COMPARE_RUNS)
    : []
  return {
    name,
    project: scalarText(value.project, '__all__', MAX_PORTFOLIO_SCOPE_ID_LENGTH),
    query: scalarText(value.query, '', MAX_PORTFOLIO_QUERY_LENGTH),
    task,
    // Existing views omit this flag: `__all__` therefore keeps its legacy All meaning. Persist the
    // flag only for the one ambiguous real task id so normal saved-view object shapes stay stable.
    ...(exactAllTask ? { taskExact: true } : {}),
    status: STATUSES.has(value.status) ? value.status : 'all',
    supertask: scalarText(value.supertask, '__all__', MAX_PORTFOLIO_SCOPE_ID_LENGTH),
    sort: SORTS.has(value.sort) ? value.sort : 'time',
    direction: value.direction === 'asc' ? 'asc' : 'desc',
    // `map` (labelled Lineage) and `concepts` restore unconditionally: both are representations of
    // whatever the saved scope resolves to, so neither can be saved into a state it cannot show.
    // `compare` is the exception — it names specific runs, and one that no longer resolves to two
    // of them must fall back rather than restore an empty comparison.
    //
    // `concepts` HAS to be here rather than falling through to `list`: authoring is strict
    // (`preparePortfolioViewSave` refuses when the normalized view and the live state disagree), so
    // an unlisted value does not degrade quietly — it makes Save view REFUSE with `code: 'state'`
    // for anyone who happens to be looking at that view. An omission here reads to the operator as
    // a broken Save button, which is why the check is a list and not a truthiness test.
    view: value.view === 'map' || value.view === 'concepts'
      || (value.view === 'compare' && compare.length > 1)
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
    return value.slice(0, MAX_PORTFOLIO_VIEWS * 2).map(normalizeView).filter(view => {
      if (!view || names.has(view.name)) return false
      names.add(view.name); return true
    }).slice(0, MAX_PORTFOLIO_VIEWS)
  } catch { return [] }
}

const exactViewState = value => ({
  project: value?.project,
  query: value?.query,
  task: value?.task,
  taskExact: value?.task === '__all__' && value?.taskExact === true,
  status: value?.status,
  supertask: value?.supertask,
  sort: value?.sort,
  direction: value?.direction,
  view: value?.view,
  compare: Array.isArray(value?.compare) ? [...value.compare] : value?.compare,
  columns: Array.isArray(value?.columns) ? [...value.columns] : value?.columns,
})

// Decoding is intentionally forgiving because storage can contain legacy or corrupt data. Authoring
// is strict: a saved view must restore the exact state the operator can currently see, never a
// fallback scope produced by normalizeView.
export const portfolioViewSignature = view => {
  if (!view || typeof view !== 'object' || Array.isArray(view)) return ''
  try { return JSON.stringify(exactViewState(view)) } catch { return '' }
}

export function preparePortfolioViewSave(views, name, state, { replaceName = '' } = {}) {
  const current = decodePortfolioViews(views)
  const requestedName = typeof name === 'string' ? name.trim() : ''
  const view = normalizeView({ ...state, name })
  if (!view || view.name !== requestedName) return { ok: false, code: 'name', views: current }
  if (portfolioViewSignature(view) !== portfolioViewSignature(state)) {
    return { ok: false, code: 'state', views: current }
  }
  const existing = current.find(item => item.name === view.name)
  if (existing && existing.name !== replaceName) {
    return { ok: false, code: 'exists', views: current }
  }
  if (!existing && current.length >= MAX_PORTFOLIO_VIEWS) {
    return { ok: false, code: 'capacity', views: current }
  }
  return {
    ok: true,
    view,
    views: [view, ...current.filter(item => item.name !== view.name)],
  }
}

export function upsertPortfolioView(views, name, state) {
  const replaceName = typeof name === 'string' ? name.trim() : ''
  return preparePortfolioViewSave(views, name, state, { replaceName }).views
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

// OPEN[compare-view-has-its-own-comparability-rule] this elects a best run from one task id and one
// direction by raw min/max, without the source-integrity and comparability rungs the cross-run panel
// documents as load-bearing (a run folded from a 20-of-1,624-record prefix holds NO rank there). Two
// screens can therefore crown two different winners, and this is the screen an operator picks a
// configuration to reuse from. One rule: read the grouping the ranking model already produces.
// proof:absent:metricComparable@ui/src/portfolioModel.js
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
