export const ALL_RUNS = '__all__'
export const UNASSIGNED_RUNS = '__unassigned__'

const stopFinishedWithError = run => String(run?.stop_reason || '').toLowerCase() === 'error'

// One lifecycle truth for the run list, workspace header, and transport. A run_finished(error)
// written while an explicit finalize is pending is not a completed finalization, and a process that
// is still alive after run_finished is still doing terminal write-out (report/lessons/cost). Neither
// state may expose Resume/Replay or auto-open an incomplete report.
export function finalizationIncomplete(run = {}) {
  if (run.phase === 'finalizing') return true
  if (!run.stop_requested) return false
  return !run.finished || stopFinishedWithError(run) || run.engine_running === true
}

export function terminalReady(run = {}) {
  return !!run.finished && !finalizationIncomplete(run) && run.engine_running !== true
}

export function runLifecycle(run = {}) {
  const incomplete = finalizationIncomplete(run)
  if (incomplete) return {
    finalizationIncomplete: true,
    terminalReady: false,
    mode: run.engine_running === false ? 'finalization-stalled' : 'finalizing',
  }
  // A natural finish and an explicit successful finalize both have a short process write-out window.
  if (run.finished && run.engine_running === true) return {
    finalizationIncomplete: false, terminalReady: false, mode: 'finishing',
  }
  if (run.finished) return { finalizationIncomplete: false, terminalReady: true, mode: 'finished' }
  if (run.paused || run.phase === 'paused') return {
    finalizationIncomplete: false, terminalReady: false, mode: 'paused',
  }
  if (run.phase === 'approval' || run.phase === 'spec_approval') return {
    finalizationIncomplete: false, terminalReady: false, mode: 'approval',
  }
  if (run.engine_running === false) return {
    finalizationIncomplete: false, terminalReady: false, mode: 'stalled',
  }
  return { finalizationIncomplete: false, terminalReady: false, mode: 'running' }
}

export function lifecyclePhaseLabel(run = {}) {
  const mode = runLifecycle(run).mode
  if (mode === 'finalization-stalled') return 'finalization stalled'
  if (mode === 'finalizing' || mode === 'finishing' || mode === 'finished') return mode
  return run.phase || mode || '—'
}

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
  const mode = runLifecycle(run).mode
  // The list has one operator-facing "finalizing" filter. Fold its stalled and terminal-write-out
  // sub-phases into it while the workspace transport keeps their more precise copy/actions.
  if (mode === 'finalization-stalled' || mode === 'finishing') return 'finalizing'
  return mode
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
