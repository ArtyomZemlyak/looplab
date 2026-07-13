// Shared helpers barrel. The old single-file grab-bag split into cohesive modules (mega-refactor
// P5.2): api.js (fetch client + every /api/* endpoint + the CONTROL action map), format.js (pure
// value formatters), layout.js (the dependency-free layered DAG layout). Everything re-exports
// through here so no importer changed; what remains below are the small run/node domain helpers
// that don't warrant a module of their own.
export * from './api.js'
export * from './format.js'
export * from './layout.js'

// Browser storage is optional infrastructure, not a render prerequisite. SecurityError is common in
// locked-down/private contexts; every preference read/write therefore degrades to an in-memory
// default instead of blanking the whole React tree before command recovery UI can render.
export function storageGet(key, fallback = null) {
  try { return window.localStorage.getItem(key) ?? fallback } catch { return fallback }
}
export function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); return true } catch { return false }
}
export function storageRemove(key) {
  try { window.localStorage.removeItem(key); return true } catch { return false }
}

// Assistant permission modes — shared by the docked assistant (AssistantBar) and the full-page view
// (AssistantChat) so the list stays defined once.
export const ASSISTANT_MODES = [
  { id: 'plan', label: 'Plan', hint: 'read-only — inspect & propose (safe)' },
  { id: 'default', label: 'Ask', hint: 'confirm every change' },
  { id: 'acceptEdits', label: 'Auto-edit', hint: 'edits apply; commands ask' },
  { id: 'auto', label: 'Auto', hint: 'runs everything without asking' },
]
// One streamed token's text: the SSE stream sends {text} objects, but some paths hand back a bare
// string — one reader so both assistant surfaces decode identically.
export const tokText = (tok) => (tok && tok.text != null) ? tok.text : (typeof tok === 'string' ? tok : '')

// Is this node the one currently being evaluated? (pending + the highest-id pending, and the run
// isn't paused/finished) — a good-enough "working" heuristic for the live pulse.
export function workingId(state) {
  // A stalled run (engine_running===false, not finished) has no live work — no "working" pulse on a
  // node whose dev session already died.
  if (!state || state.finished || state.paused || state.phase === 'finalizing' || state.stop_requested || state.engine_running === false) return null
  // A node mid-BUILD (dev session running) is the true "working" node — it precedes any pending eval.
  if (state.building && state.building.node_id != null) return state.building.node_id
  const pend = Object.values(state.nodes || {}).filter(n => n.status === 'pending')
  if (!pend.length) return null
  return Math.max(...pend.map(n => n.id))
}

export function nodeClass(node, state, workId) {
  const cls = ['node-card', `s-${node.status}`]
  if (node.id === state.best_node_id) cls.push('best')
  if (node.feasible === false) cls.push('infeasible')
  if (node.id === workId) cls.push('working')
  return cls.join(' ')
}

// Visual identity per operator (= the kind of task a node performs). A single monochrome SVG
// icon (see icons.jsx) makes the DAG readable at a glance — which nodes are baselines vs
// hill-climbs vs repairs vs merges vs ablations — WITHOUT adding hue (status owns colour).
const OPERATOR_META = {
  draft:        { icon: 'flag',      label: 'draft — initial baseline solution' },
  improve:      { icon: 'trending',  label: 'improve — hill-climb around best' },
  debug:        { icon: 'bug',       label: 'debug — repair a failed parent' },
  merge:        { icon: 'confluence', label: 'merge — combine multiple parents' },
  refine_block: { icon: 'target',    label: 'refine — ablation-driven tweak' },
  fork:         { icon: 'gitbranch', label: 'fork — operator-seeded branch' },
  random:       { icon: 'dot',       label: 'random — exploratory sample' },
  exploit:      { icon: 'trending',  label: 'exploit — refine the leader' },
  greedy:       { icon: 'trending',  label: 'greedy — best-first step' },
  ablate:       { icon: 'target',    label: 'ablate — sensitivity probe' },
  manual:       { icon: 'flag',      label: 'manual — operator-authored experiment' },
}

export function operatorMeta(op) {
  return OPERATOR_META[op] || { icon: 'dot', label: op || 'operator' }
}

// The operators worth showing in the legend (stable order, only the common ones).
export const OPERATOR_LEGEND = ['draft', 'improve', 'debug', 'merge', 'refine_block', 'fork', 'random']

export function parentMetric(node, state) {
  if (!node.parent_ids || !node.parent_ids.length) return null
  const p = state.nodes[node.parent_ids[0]]
  return p ? (p.confirmed_mean ?? p.metric) : null
}

export function delta(node, state) {
  const pm = parentMetric(node, state)
  const m = node.confirmed_mean ?? node.metric
  if (pm == null || m == null) return null
  const d = m - pm
  const improved = state.direction === 'min' ? d < 0 : d > 0
  return { d, improved }
}

// Intra-node sweep detection. A node is a sweep when it carries trials (per-node detail), a
// trials_summary (trimmed live state), or its idea declared a search `space` (even before it ran).
// The node's `operator` stays draft/improve (authoritative for ASHA/policy), so detection keys off
// these fields, never the operator label.
export function isSweep(node) {
  if (!node) return false
  if (node.trials_summary || (node.trials && node.trials.length)) return true
  const sp = node.idea?.space
  return !!(sp && Object.keys(sp).length)
}

// Compact sweep view for the card/hull header, from whichever shape is available (summary in live
// state, full trials in detail, or just the declared grid pre-eval). `best` may be undefined when
// only full trials are present — the card shows node.metric (already the best) anyway.
export function sweepInfo(node) {
  const ts = node?.trials_summary
  if (ts) return { count: ts.count || 0, best: ts.best, ok: ts.ok || 0, failed: ts.failed || 0, series: ts.series || [] }
  const tr = node?.trials || []
  if (tr.length) {
    const series = tr.map(t => t.metric).filter(v => v != null)
    return { count: tr.length, best: undefined, ok: series.length, failed: tr.length - series.length, series }
  }
  const sp = node?.idea?.space || {}
  const keys = Object.keys(sp)
  const count = keys.length ? keys.reduce((acc, k) => acc * (sp[k]?.length || 1), 1) : 0
  return { count, best: undefined, ok: 0, failed: 0, series: [] }
}

export function phaseLabel(state) {
  return state?.phase || (state?.finished ? 'finished' : '—')
}
