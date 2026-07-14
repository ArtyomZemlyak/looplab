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
  return !run.finished || stopFinishedWithError(run) || run.engine_running !== false
}

export function terminalReady(run = {}) {
  return !!run.finished && !finalizationIncomplete(run) && run.engine_running === false
}

export function runLifecycle(run = {}) {
  const incomplete = finalizationIncomplete(run)
  if (incomplete) return {
    finalizationIncomplete: true,
    terminalReady: false,
    mode: run.engine_running === false ? 'finalization-stalled' : 'finalizing',
  }
  // A natural finish and an explicit successful finalize both have a short process write-out window.
  if (run.finished && run.engine_running !== false) return {
    finalizationIncomplete: false, terminalReady: false, mode: 'finishing',
  }
  if (run.finished) return { finalizationIncomplete: false, terminalReady: true, mode: 'finished' }
  if (run.paused || run.phase === 'paused') return {
    finalizationIncomplete: false, terminalReady: false, mode: 'paused',
  }
  if (run.phase === 'approval' || run.phase === 'spec_approval') return {
    finalizationIncomplete: false, terminalReady: false, mode: 'approval',
  }
  if (run.engine_running == null) return {
    finalizationIncomplete: false, terminalReady: false, mode: 'unknown',
  }
  if (run.engine_running === false) return {
    finalizationIncomplete: false, terminalReady: false, mode: 'stalled',
  }
  return { finalizationIncomplete: false, terminalReady: false, mode: 'running' }
}

// Approval is a node-LIFECYCLE decision, not a synonym for "approve whichever node is best now".
// A reset can reuse the same node id with a new attempt, and the best can change while a request is
// pending. First-party shortcuts therefore exist only when the folded request still names an exact
// subject + generation that matches the visible node. Missing/legacy context fails closed to Events.
export function pendingApprovalTarget(state = {}) {
  if (state.phase !== 'approval' || state.awaiting_approval !== true) return null
  const nodeId = state.approval_subject
  const nodeGeneration = state.approval_generation
  if (!Number.isSafeInteger(nodeId) || nodeId < 0
      || !Number.isSafeInteger(nodeGeneration) || nodeGeneration < 0) return null
  const node = state.nodes?.[nodeId]
  if (!node || node.tombstoned || node.status === 'aborted'
      || node.attempt !== nodeGeneration) return null
  return { nodeId, nodeGeneration }
}

export function approvalCommandFor(state = {}) {
  if (state.phase === 'spec_approval' && state.spec_approval_requested && !state.spec_confirmed) {
    return '/ratify'
  }
  const target = pendingApprovalTarget(state)
  return target ? `/approve #${target.nodeId}` : null
}

// A blank graph is not one state. It can mean an early historical snapshot, an intentionally
// read-only review, setup in progress, a paused/stalled engine, incomplete finalization, or a truly
// terminal run with no experiments. Keep that distinction in one pure model so the canvas never
// exposes a live mutation while the user is looking at history/review, and never calls a recoverable
// run merely "empty". RunView maps these action ids onto its already-authoritative controls.
export function dagEmptyPresentation({
  displayed = {}, live = null, resourceStatus = 'ready', connected = true,
  historyActive = false, reviewMode = false, sequence = null,
} = {}) {
  if (resourceStatus !== 'ready' || Object.keys(displayed?.nodes || {}).length > 0) return null

  const connectionNote = connected || historyActive || reviewMode
    ? ''
    : ' The live connection is interrupted; this is the last received state.'
  const state = live || displayed || {}
  const mode = runLifecycle(state).mode
  const result = (kind, tone, title, body, actions = [], liveRegion = 'polite') => ({
    kind, tone, title, body: body + connectionNote, actions, liveRegion,
  })
  const action = (id, label, emphasis = 'secondary') => ({ id, label, emphasis })

  if (historyActive) return result(
    'history', 'neutral', `No experiments at snapshot${sequence == null ? '' : ` seq ${sequence}`}`,
    'This point in the timeline predates the first experiment. Return to the live run to continue.',
    [action('return-live', 'Return to live', 'primary')],
  )
  if (reviewMode) return result(
    'review', 'neutral', 'No experiments are available in this review',
    mode === 'finished'
      ? 'This run ended without an experiment card. The report may still explain why.'
      : 'The owner has not produced or shared an experiment card yet.',
    mode === 'finished' ? [action('report', 'View report', 'primary')] : [],
  )
  if (mode === 'finalization-stalled') return result(
    'finalization-stalled', 'danger', 'Finalization stopped before wrap-up completed',
    'The engine stopped before the report, lessons, and final cost were safely written.',
    [action('finalize', 'Reattach finalization', 'primary'), action('events', 'Show events')], 'assertive',
  )
  if (mode === 'finalizing' || mode === 'finishing') return result(
    mode, 'progress', 'Wrapping up this run…',
    mode === 'finishing'
      ? 'The run is writing its terminal report, lessons, and cost. No recovery action is needed yet.'
      : 'Finalization is still active. The report will open only after terminal write-out completes.',
  )
  // Spec approval can legitimately precede node #0. It must outrank a folded paused flag so the
  // operator receives the exact ratification action instead of an unsafe generic Resume.
  if (state.phase === 'approval' || state.phase === 'spec_approval') {
    const command = approvalCommandFor(state)
    if (!command) return result(
      'approval-incomplete', 'danger', 'Approval state is incomplete',
      'The run requests human approval but does not identify an experiment. Inspect the timeline instead of sending a guessed command.',
      [action('events', 'Show events', 'primary')], 'assertive',
    )
    return result(
      'approval', 'attention', state.phase === 'spec_approval'
        ? 'Evaluation spec needs approval' : 'Human approval is required',
      `Review the pending decision, then continue with ${command} in Assistant.`,
      [action('assistant', `Open Assistant · ${command}`, 'primary')], 'assertive',
    )
  }
  if (mode === 'paused') return result(
    'paused', 'attention', 'Paused before the first experiment',
    'Resume to continue setup and create the first experiment, or finalize to wrap up without one.',
    [action('resume', 'Resume run', 'primary'), action('finalize', 'Finalize run', 'danger')],
  )
  if (mode === 'unknown') return result(
    'unknown', 'neutral', 'Engine ownership is unknown',
    'LoopLab cannot verify whether an engine owns this run. Inspect Events and storage locking before taking recovery action.',
    [action('events', 'Show events', 'primary'),
      ...(!connected ? [action('retry-connection', 'Retry connection')] : [])], 'assertive',
  )
  if (mode === 'stalled') return result(
    'stalled', 'danger', 'Engine stopped before the first experiment',
    'No process is advancing this run. Resume it to reattach safely, or finalize it without an experiment.',
    [action('resume', 'Resume run', 'primary'), action('finalize', 'Finalize run', 'danger'),
      action('events', 'Show events')], 'assertive',
  )
  if (mode === 'finished') return result(
    'finished', 'neutral', 'Run finished without producing an experiment',
    'Open the report for the terminal explanation, or resume the run to continue searching.',
    [action('report', 'View report', 'primary'), action('resume', 'Resume run')],
  )
  if (state.engine_running === true) return result(
    'preparing', 'progress', 'Preparing the first experiment…',
    state.phase && !['running', 'search'].includes(state.phase)
      ? `The run is in ${state.phase}. Setup and research events remain available in the timeline.`
      : 'The engine is active. Setup and research events remain available in the timeline.',
    [action('events', 'Show events')],
  )
  const actions = [action('events', 'Show events', 'primary')]
  if (!connected) actions.push(action('retry-connection', 'Retry connection'))
  return result(
    'empty', connected ? 'neutral' : 'attention', 'No experiments yet',
    connected
      ? 'The run has not produced its first experiment. Check the timeline for setup or research activity.'
      : 'The last received state contains no experiment cards.',
    actions, connected ? 'polite' : 'assertive',
  )
}

export function lifecyclePhaseLabel(run = {}) {
  const mode = runLifecycle(run).mode
  if (mode === 'finalization-stalled') return 'finalization stalled'
  if (mode === 'finalizing' || mode === 'finishing' || mode === 'finished') return mode
  if (mode === 'unknown') return 'engine ownership unknown'
  return run.phase || mode || '—'
}

export function indexProjects(projects = []) {
  const byParent = {}
  const byId = Object.fromEntries(projects.map(project => [project.id, project]))
  projects.forEach(project => { (byParent[project.parent_id || null] ||= []).push(project) })
  // Coerce the name before localeCompare (like every sibling comparator in this file): a project with
  // a null/undefined name would otherwise throw and break the entire run list / map render.
  Object.values(byParent).forEach(items => items.sort(
    (a, b) => String(a.name || '').localeCompare(String(b.name || ''))))
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
