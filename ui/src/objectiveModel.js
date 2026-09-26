// THE RUN'S OBJECTIVE — the browser half of an operator `metric_retarget` (doc 68 68.2,
// `looplab/events/replay.py::_on_metric_retarget`). The fold makes a DECLARED extra metric the one
// every node is ranked by; this module answers what the Metrics tab needs to say about it and which
// keys it may offer, with the same rules the server refuses by
// (`looplab/serve/control_validation.py::_normalize_metric_retarget`), so a button is never offered
// for a command the server would reject for a reason the record already shows. (A withheld scorer
// the task declares is the one refusal the state cannot show; the server names it.)
import { extraMetricIsDeclared } from './extraMetrics.js'

// The key in force, or null — the task's own metric.
export function objectiveKey(state) {
  const key = state?.objective_key
  return typeof key === 'string' && key ? key : null
}

// The ★ row's label: which number it is, once a retarget made it something other than the task's.
export function objectiveLabel(state) {
  const key = objectiveKey(state)
  return key ? `objective · ${key}` : 'objective'
}

// Why this run cannot be retargeted at all, or null. A holdout is scored on the TASK's own metric,
// so a retargeted objective would be ranked against another scale.
export function retargetBlocked(state) {
  if (!state || typeof state !== 'object') return 'no run'
  if (state.host_grading) return 'host-graded'
  if (Array.isArray(state.holdout_evaluated_ids) && state.holdout_evaluated_ids.length) {
    return 'holdout scored'
  }
  return null
}

const evaluatedNodes = state => Object.values(state?.nodes || {})
  .filter(n => n && n.status === 'evaluated' && !n.tombstoned)

// The keys an operator may make the objective: recorded on the DECLARED channel by at least one
// evaluated node with a finite value, never oriented against the run by any node's declaration (a
// retarget keeps the direction), and not the objective already in force. Sorted, so the buttons
// keep their order across renders.
export function retargetableKeys(state) {
  if (retargetBlocked(state)) return []
  const current = objectiveKey(state)
  const against = new Set()
  const declared = new Set()
  for (const n of evaluatedNodes(state)) {
    for (const [key, way] of Object.entries(n.extra_metrics_direction || {})) {
      if ((way === 'min' || way === 'max') && way !== state.direction) against.add(key)
    }
    for (const [key, value] of Object.entries(n.extra_metrics || {})) {
      if (typeof value !== 'number' || !Number.isFinite(value)) continue
      if (extraMetricIsDeclared(n, key)) declared.add(key)
    }
  }
  return [...declared].filter(key => key !== current && !against.has(key)).sort()
}

// The retarget that put the current objective in force, for the tab's one-line account of it:
// `{seq, key, previous}` or null when the task's own metric ranks the run.
export function currentRetarget(state) {
  const history = Array.isArray(state?.objective_history) ? state.objective_history : []
  const last = history.length ? history[history.length - 1] : null
  return last && objectiveKey(state) != null && last.key === objectiveKey(state) ? last : null
}
