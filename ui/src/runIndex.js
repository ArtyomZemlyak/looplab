import { activeNodeMap } from './nodeProjection.js'

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

// --- Event-log INTEGRITY -----------------------------------------------------------------------
// The server's `source_integrity` receipt (`looplab/events/eventstore.py::log_integrity`) says
// whether the fold behind every other field on this object saw the WHOLE log. It exists because a
// run whose log stops being readable part-way renders as a smaller, entirely plausible run: on the
// shipped corpus `rubertlite-dense-retrieval` drew a row reading `nodes: 2, best_metric: 0.8077`
// from a 20-record prefix of a 1,624-record log, with nothing anywhere on screen saying so.
//
// Both helpers take the run SUMMARY row (`/api/runs`) or the folded run state (`useRunState`'s
// `live`) — the server mirrors the same receipt onto both, so one rule serves the list and the
// workspace. An ABSENT receipt is `false`, deliberately: a legacy server that does not send the
// field must not paint every run with a corruption warning. An UNREADABLE log is incomplete, which
// is the direction that never renders "we could not look" as "we looked and it is fine".
export function sourceIncomplete(run = {}) {
  const receipt = run?.source_integrity
  if (!receipt || typeof receipt !== 'object') return false
  return receipt.complete !== true
}

// The one wording the browser prints, mirroring `eventstore.py::integrity_sentence`. Phrased so the
// operator cannot read a truncated view as a small run — it names what IS visible, what is not, and
// that the missing records are on disk rather than absent from history.
export function sourceIntegrityNotice(run = {}) {
  if (!sourceIncomplete(run)) return ''
  const receipt = run.source_integrity
  if (receipt.unreadable) {
    return 'This run’s event log could not be read at all. Nothing on this screen describes the run.'
  }
  const good = Number.isSafeInteger(receipt.good_records) ? receipt.good_records : null
  const dropped = Number.isSafeInteger(receipt.dropped_lines) ? receipt.dropped_lines : null
  const line = Number.isSafeInteger(receipt.corrupt_line) ? receipt.corrupt_line : null
  // good + the BOUNDARY row + dropped, mirroring `eventstore.py::integrity_sentence`: the boundary
  // line is a complete record on disk, so this total is the one the timeline pager also counts.
  const total = good != null && dropped != null ? good + dropped + 1 : null
  const where = line != null ? ` at line ${line}` : ''
  const scope = total != null ? `You are seeing ${good} of ${total} records; ${dropped} durable `
    + 'record(s) behind that boundary are NOT shown.' : 'Part of the log is not shown.'
  return `Incomplete record: this run’s event log stops being readable${where}. ${scope} `
    + 'Every number here describes the readable prefix only — it is not evidence that the rest did '
    + 'not happen.'
}

// --- What kind of number `best_metric` IS ---------------------------------------------------------
// The server's `best_metric_caveats` receipt (`looplab/engine/champion_caveats.py`) — the run's own
// recorded caveat about the number this row publishes, in a list the row could not previously carry.
// It exists because `/api/runs` publishes `best_metric` and nothing else about the champion: no
// `violations`, no `metric_provenance`, no node id. Two rungs put a caveated number there and the
// browser can derive NEITHER of them from the row — `metric_salvage: "select"` (a metric recovered
// from a FAILED eval, admitted into selection, and no violation row minted to notice it by) and
// `trust_gate: "audit"`, the shipped DEFAULT (a high-precision reward-hack/leakage signal recorded
// about the champion and enforced against nothing).
//
// The words are the SERVER's, mirrored here rather than re-derived, and `salvaged` is deliberately
// the same word `trustSemantics.js::OBJECTIVE_SALVAGED` prints for the node inside the run — the
// portfolio row and that node's Metrics tab must not describe one number two ways. An UNKNOWN slug
// is kept and rendered rather than dropped: a server that learns a third caveat must not read as a
// clean run to an older browser.
//
// The THIRD member is not of that pair and the difference is worth keeping straight: `salvaged` and
// `trust_flagged` qualify HOW the number was measured, while `params_overridden` qualifies WHAT it
// is a number FOR — the champion's own committed code assigns a different value to a parameter its
// Idea declares, so the run is publishing a result at coordinates it never occupied. It is the one
// member that is non-empty on this box's corpus today (v8 node 3, batch_size 8192 declared / 4096 in
// code, on the champion), which is why the row must be able to say it.
export const CHAMPION_CAVEAT_SALVAGED = 'salvaged'
export const CHAMPION_CAVEAT_TRUST_FLAGGED = 'trust_flagged'
export const CHAMPION_CAVEAT_PARAMS_OVERRIDDEN = 'params_overridden'

// ABSENT is `[]`, deliberately, and for the same reason `sourceIncomplete` defaults to false: a
// legacy server that does not send the field must not paint every run with a caveat. And an EMPTY
// list is not a certificate — it says the run recorded no such caveat, never that a detector ran
// (`reward_hack_detect` is off by default), which is `trustSemantics.js`' first rule.
export function bestMetricCaveats(run = {}) {
  const raw = run?.best_metric_caveats
  if (!Array.isArray(raw)) return []
  return raw.filter(slug => typeof slug === 'string' && slug.trim()).map(slug => slug.trim())
}

export const bestMetricCaveated = (run = {}) => bestMetricCaveats(run).length > 0

// The SHORT word for a table cell, and the one place it is spelled. An unrecognised slug renders as
// itself — the server said something this browser has no sentence for, and printing the raw word is
// how the operator finds out, where dropping it would republish the number as unqualified.
const CAVEAT_LABEL = {
  [CHAMPION_CAVEAT_SALVAGED]: 'salvaged',
  [CHAMPION_CAVEAT_TRUST_FLAGGED]: 'trust-flagged',
  [CHAMPION_CAVEAT_PARAMS_OVERRIDDEN]: 'params overridden',
}
export const bestMetricCaveatLabel = slug => CAVEAT_LABEL[slug] || String(slug || '')

// The one wording the browser prints about a caveated `best_metric`, mirroring
// `engine/champion_caveats.py`'s vocabulary comment. Every sentence says the same two things: what
// the number is, and that the run SELECTED on it anyway — because the operator's question here is
// never "is this run flagged", it is "may I reuse this configuration".
export function bestMetricCaveatNotice(run = {}) {
  const slugs = bestMetricCaveats(run)
  if (!slugs.length) return ''
  const sentences = slugs.map(slug => (
    slug === CHAMPION_CAVEAT_SALVAGED
      ? 'This run’s best metric was NOT measured: its evaluation failed and the run recovered the '
        + 'number with its own declared reader. metric_salvage is set to “select”, so it competes '
        + 'for champion like a measured result.'
      : slug === CHAMPION_CAVEAT_TRUST_FLAGGED
        ? 'The node this number comes from carries a high-precision reward-hacking or leakage '
          + 'signal. trust_gate is not enforcing, so it was selected as this run’s best anyway.'
        : slug === CHAMPION_CAVEAT_PARAMS_OVERRIDDEN
          ? 'The node this number comes from ships code that assigns a different value to a '
            + 'parameter its own experiment record declares, so the declared configuration is not '
            + 'the one this result was produced under. The run selected on it anyway — the metric '
            + 'itself was measured normally; what is in question is what it is a measurement of.'
          : `The server reports a caveat this view has no sentence for: “${slug}”.`))
  return sentences.join(' ')
}

export function terminalReady(run = {}) {
  return !!run.finished && !finalizationIncomplete(run) && run.engine_running === false
}

// --- What a STALLED finalization's remedy actually IS ------------------------------------------
// The transport's "finalize" action submits a durable `run_abort` command (`Dock.jsx`'s
// TRANSPORT_INTENTS, `commandModel.js`'s TRANSPORT_EVENT_BY_ACTION), and on a run whose finalization
// is already pending the server does NOT start one: `control_validation.py::_decide_run_abort`
// answers `attach`, and `run_commands.py::submit` then requires a `run_abort` ALREADY IN THE LOG to
// attach to (`_pending_finalize_intent`). There are two ways into `finalization-stalled` and only
// one of them has that record:
//
//   * an operator FINALIZE whose engine then died — the `run_abort` is on the log, `stop_requested`
//     folds to its reason, `attach` finds it, and the command spawns a driver that completes the
//     wrap-up. This is the state the button was written for and it works.
//   * a run that finished NATURALLY (`no_eligible_candidate`) and whose engine died between
//     `run_finished` and `finalization_finished`. `stop_requested` is unset because nothing ever
//     appended a `run_abort`, so the command is REJECTED `command_intent_missing` — whose server
//     remediation reads "inspect/repair the event log", advice about a log that is not damaged.
//     Measured on `runs/live-deps4-0804` (dead 2026-08-04, 8 of 13 finalize steps): the operator
//     read that, and the run's own Delete refused too. `looplab finalize <run_dir>` cleared it.
//
// `stop_requested === 'finalized'` is the exact client-side spelling of the server's own attach
// test, not an approximation of it: the fold sets `stop_requested` to the LAST `run_abort`'s reason
// (`replay.py::_on_run_abort`), every first-party client sends `reason: 'finalized'`, and
// `_attached_finalize_intact` re-checks `state.stop_requested == <that reason>` before the command
// may proceed. A superseded `run_abort` (resumed since) therefore reads as absent on both sides,
// and a finalize pending under a DIFFERENT reason would be rejected `finalize_payload_conflict`
// rather than attached — also inert, so it belongs on the same side of this predicate.
export const FINALIZE_INTENT_REASON = 'finalized'

export function pendingFinalizeIntent(run = {}) {
  return String(run?.stop_requested || '') === FINALIZE_INTENT_REASON
}

// The command the operator can actually run, spelled exactly as the server's own deletion refusal
// spells it (`deletion_service.py`'s `run_finalization_incomplete` remediation) so the two surfaces
// that describe this state name ONE remedy. `<runs>` stays a placeholder because the browser does
// not know the server's runs root; the run directory is the run id and that part is exact.
// A run id that is not a plain directory name degrades to `<run_dir>` rather than printing a command
// the operator would paste into a shell.
const RUN_DIR_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
export function finalizeRecoveryCommand(runId = '') {
  const name = String(runId || '').trim()
  return `looplab finalize <runs>/${RUN_DIR_NAME_RE.test(name) ? name : '<run_dir>'}`
}

/** The whole remedy for a stalled finalization no client control can perform, or null when the
 *  ordinary Reattach control still applies. One place decides it, because Dock's transport row and
 *  the empty-canvas card must not describe one state two ways. */
export function stalledFinalizationRemedy(run = {}, runId = '') {
  if (pendingFinalizeIntent(run)) return null
  // Which of the two inert shapes this is. A run with NO recorded stop finished by itself; one whose
  // recorded stop carries another reason has an intent, just not one this control may alias (the
  // server answers `finalize_payload_conflict` rather than `command_intent_missing`). Neither can be
  // acted on from the browser, and saying "finished on its own" about the second would be false.
  const why = String(run?.stop_requested || '')
    ? 'This run already carries a finalize request recorded under a different reason, which this '
      + 'control may not reattach to'
    : 'This run finished on its own, so there is no pending finalize request to reattach to'
  return {
    why,
    command: finalizeRecoveryCommand(runId),
    note: 'It writes the report, lessons and cost roll-up this run still owes, so it uses the '
      + 'configured model if one is reachable and names the artifacts it degraded if not. Until it '
      + 'runs, deleting this run is refused for the same reason.',
  }
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
  historyActive = false, reviewMode = false, sequence = null, runId = '',
} = {}) {
  // Tombstoned/aborted rows remain in replay state for audit, but Dag renders only
  // active nodes. Base the empty-state decision on the same projection or the canvas goes blank
  // when the last visible experiment is retired.
  if (resourceStatus !== 'ready'
      || Object.keys(activeNodeMap(displayed?.nodes || {}, displayed)).length > 0) return null

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
  // REATTACH is offered only where there is something to reattach TO (see `pendingFinalizeIntent`).
  // Without a pending finalize the same button submits a command the server rejects, so the card
  // states the remedy instead of offering a control that cannot act. It is deliberately TEXT and not
  // a second button: the paths that could act from here — `POST /api/runs/{id}/resume`, a
  // finalize-only route — all SPAWN AN ENGINE, which is paid work and a different promise from
  // "reattach". The command below re-enters wrap-up only (`cli/run_cmds.py::finalize`'s
  // crash-boundary repair), never the search.
  if (mode === 'finalization-stalled') {
    const remedy = stalledFinalizationRemedy(state, runId)
    if (!remedy) return result(
      'finalization-stalled', 'danger', 'Finalization stopped before wrap-up completed',
      'The engine stopped before the report, lessons, and final cost were safely written.',
      [action('finalize', 'Reattach finalization', 'primary'), action('events', 'Show events')], 'assertive',
    )
    return {
      ...result(
        'finalization-stalled', 'danger', 'Finalization stopped before wrap-up completed',
        `The engine stopped before the report, lessons, and final cost were safely written. ${remedy.why}, `
        + 'and nothing on this server will resume it. Complete the wrap-up already on disk from a '
        + 'shell — it is idempotent, and it re-enters the wrap-up only, never the search:',
        [action('events', 'Show events', 'primary')], 'assertive',
      ),
      command: remedy.command,
      commandNote: remedy.note,
    }
  }
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
      // Guard the push on `out` membership: a malformed/cyclic parent_id chain (a -> b -> a) would
      // otherwise re-push forever and FREEZE the run list / map render — the same "don't break the
      // whole render on bad data" invariant the name-coerce above protects. A well-formed tree is
      // unaffected (each node is reachable once).
      ;(byParent[current] || []).forEach(child => {
        if (!out.has(child.id)) { out.add(child.id); stack.push(child.id) }
      })
    }
    return out
  }
  return { byParent, byId, subtree }
}

// Ancestor walks over the same `parent_id` chain `indexProjects` indexes. They live here, beside
// that guarded descent, because BOTH the run list and the Map view walk this data and a cyclic chain
// (a -> b -> a) freezes whichever one forgot its visited set — the drift these helpers exist to end.
export function projectDepth(byId = {}, id) {
  let depth = 0, current = byId[id]
  const seen = new Set([id])
  while (current?.parent_id && !seen.has(current.parent_id)) {
    seen.add(current.parent_id)
    depth += 1
    current = byId[current.parent_id]
  }
  return depth
}

export function projectAncestorCollapsed(byId = {}, id, collapsed = new Set()) {
  let current = byId[id]?.parent_id
  const seen = new Set([id])
  while (current && !seen.has(current)) {
    if (collapsed.has(current)) return true
    seen.add(current)
    current = byId[current]?.parent_id
  }
  return false
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

// One O(runs × project-depth) pass replaces rebuilding/filtering the full run array once for every
// project row on each dashboard poll. Unknown project ids intentionally match scopeRuns(): they are
// neither a known subtree nor "unassigned" while the run still carries an assignment.
export function projectRunCounts(runs = [], projects = []) {
  const counts = new Map([[ALL_RUNS, runs.length]])
  const parents = new Map(projects.map(project => [project.id, project.parent_id]))
  for (const { project_id } of runs) {
    let id = project_id || UNASSIGNED_RUNS
    const seen = new Set()
    while (!seen.has(id) && (id === UNASSIGNED_RUNS || parents.has(id))) {
      seen.add(id)
      counts.set(id, (counts.get(id) || 0) + 1)
      id = parents.get(id)
    }
  }
  return counts
}

export function filterRuns(runs = [], {
  project = ALL_RUNS, projects = [], query = '', task = ALL_RUNS,
  taskExact = task !== ALL_RUNS, supertask = ALL_RUNS, status = 'all',
} = {}) {
  let result = scopeRuns(runs, project, projects)
  const q = query.trim().toLowerCase()
  if (q) result = result.filter(run => [run.label, run.run_id, run.task_id, run.goal]
    .some(value => String(value || '').toLowerCase().includes(q)))
  // `__all__` is a legal opaque task id as well as the legacy UI sentinel. Callers that preserve
  // selection identity pass taskExact so a real task carrying that id remains filterable.
  if (taskExact) result = result.filter(run => run.task_id === task)
  if (supertask === UNASSIGNED_RUNS) result = result.filter(run => !run.supertask_id)
  else if (supertask !== ALL_RUNS) result = result.filter(run => run.supertask_id === supertask)
  if (status !== 'all') result = result.filter(run => effectiveRunStatus(run) === status)
  return result
}

export function metricComparable(runs = []) {
  const tasks = new Set(runs.map(run => run.task_id))
  const directions = new Set(runs.map(run => run.direction))
  return runs.length > 0 && tasks.size === 1 && !!runs[0].task_id
    && directions.size === 1 && ['min', 'max'].includes(runs[0].direction)
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

// SELECTION IS NOT COMPARISON. The run list capped SELECTION at 8 because comparison is what it fed,
// so an operator who wanted to pick twenty runs — to compare the first few, or to act on all of them
// — was refused at the ninth checkbox with "Compare is limited to 8 runs". The limit is real, but it
// belongs to the comparison, not to the act of choosing.
//
// So: select as many as you like; the comparison takes the first `COMPARE_MAX` of them and says so
// out loud. A bound that silently drops the rest would be worse than the refusal it replaces.
export const COMPARE_MAX = 8
// Selection still needs SOME ceiling — it round-trips through the saved-view record and the URL — but
// one far above any real list rather than one shaped by a different feature's limit.
export const SELECTION_MAX = 500

/** The runs a comparison will actually draw, and what it had to leave out. */
export function comparisonScope(selectedRuns = [], max = COMPARE_MAX) {
  const rows = Array.isArray(selectedRuns) ? selectedRuns : []
  const shown = rows.slice(0, Math.max(0, max))
  return { shown, omitted: Math.max(0, rows.length - shown.length), total: rows.length }
}

/** One line for the selection bar: how many are picked, and what comparison will do with them. */
export function selectionNotice(scope) {
  if (!scope || !scope.total) return ''
  if (scope.total < 2) return 'Select one more run to compare.'
  if (scope.omitted) {
    return `Comparison shows the first ${scope.shown.length} of ${scope.total}; `
      + `${scope.omitted} more stay selected.`
  }
  return 'Ready to compare run details.'
}
