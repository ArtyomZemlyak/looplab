export const ALL_RUNS = '__all__'
export const UNASSIGNED_RUNS = '__unassigned__'

export function indexProjects(projects = []) {
  const byParent = {}
  const byId = Object.fromEntries(projects.map(project => [project.id, project]))
  projects.forEach(project => { (byParent[project.parent_id || null] ||= []).push(project) })
  Object.values(byParent).forEach(items => items.sort((a, b) => a.name.localeCompare(b.name)))
  const subtree = (id) => {
    const out = new Set([id]); const stack = [id]
    while (stack.length) {
      const current = stack.pop()
      ;(byParent[current] || []).forEach(child => { out.add(child.id); stack.push(child.id) })
    }
    return out
  }
  return { byParent, byId, subtree }
}

export function effectiveRunStatus(run) {
  if (run.finished) return 'finished'
  if (run.paused || run.phase === 'paused') return 'paused'
  if (run.phase === 'approval' || run.phase === 'spec_approval') return 'approval'
  if (run.engine_running === false) return 'stalled'
  return 'running'
}

export function scopeRuns(runs = [], project = ALL_RUNS, projects = []) {
  if (project === ALL_RUNS) return runs
  if (project === UNASSIGNED_RUNS) return runs.filter(run => !run.project_id)
  const ids = indexProjects(projects).subtree(project)
  return runs.filter(run => ids.has(run.project_id))
}

export function filterRuns(runs = [], {
  project = ALL_RUNS, projects = [], query = '', task = ALL_RUNS,
  supertask = ALL_RUNS, status = 'all',
} = {}) {
  let result = scopeRuns(runs, project, projects)
  const q = query.trim().toLowerCase()
  if (q) result = result.filter(run => [run.label, run.run_id, run.task_id, run.goal]
    .some(value => String(value || '').toLowerCase().includes(q)))
  if (task !== ALL_RUNS) result = result.filter(run => run.task_id === task)
  if (supertask === UNASSIGNED_RUNS) result = result.filter(run => !run.supertask_id)
  else if (supertask !== ALL_RUNS) result = result.filter(run => run.supertask_id === supertask)
  if (status !== 'all') result = result.filter(run => effectiveRunStatus(run) === status)
  return result
}

export function metricComparable(runs = []) {
  const tasks = new Set(runs.map(run => run.task_id).filter(Boolean))
  const directions = new Set(runs.map(run => run.direction).filter(Boolean))
  return tasks.size === 1 && directions.size <= 1
}

export function sortRuns(runs = [], key = 'time', order = 'desc') {
  const result = [...runs]
  const name = run => String(run.label || run.run_id || '').toLowerCase()
  const metric = run => run.best_confirmed ?? run.best_metric
  const mul = order === 'asc' ? 1 : -1
  const ordinary = {
    time: (a, b) => mul * ((a.mtime || 0) - (b.mtime || 0)),
    name: (a, b) => mul * name(a).localeCompare(name(b)),
    task: (a, b) => mul * ((a.task_id || '').localeCompare(b.task_id || '') || name(a).localeCompare(name(b))),
    nodes: (a, b) => mul * ((a.nodes || 0) - (b.nodes || 0)),
    phase: (a, b) => mul * ((a.phase || '').localeCompare(b.phase || '')),
  }
  if (key !== 'metric') return result.sort(ordinary[key] || (() => 0))
  if (!metricComparable(result)) return result

  // For metric sorting, asc/desc mean best/worst rather than raw numeric direction. Missing values
  // always stay last. A max objective therefore reverses the ordinary numeric comparison.
  const direction = result.find(run => run.direction)?.direction || 'min'
  const bestFirst = order === 'asc'
  return result.sort((a, b) => {
    const av = metric(a), bv = metric(b)
    if (av == null || bv == null) return (av == null ? 1 : 0) - (bv == null ? 1 : 0)
    const objective = direction === 'max' ? (bv - av) : (av - bv)
    return bestFirst ? objective : -objective
  })
}
