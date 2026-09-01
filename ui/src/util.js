// Shared helpers barrel. The old single-file grab-bag split into cohesive modules (mega-refactor
// P5.2): api.js (fetch client + every /api/* endpoint + the CONTROL action map), format.js (pure
// value formatters), layout.js (the dependency-free layered DAG layout). Everything re-exports
// through here so no importer changed; what remains below are the small run/node domain helpers
// that don't warrant a module of their own.
export * from './api.js'
export * from './format.js'
export * from './layout.js'
export * from './nodeActivity.js'

import { nodeActivityStatus, runCanWork, workingNodeIds, NODE_ACTIVITY } from './nodeActivity.js'

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
  { id: 'acceptEdits', label: 'Auto-edit', hint: 'edits apply; commands and risky actions ask' },
  { id: 'auto', label: 'Auto', hint: 'routine changes run; high-risk actions ask' },
]
// One streamed token's text: the SSE stream sends {text} objects, but some paths hand back a bare
// string — one reader so both assistant surfaces decode identically.
export const tokText = (tok) => (tok && tok.text != null) ? tok.text : (typeof tok === 'string' ? tok : '')

// The primary live node retained for compatibility with one-subject consumers (auto-collapse).
// Visual surfaces use `workingNodeIds` directly so parallel builds/evaluations all remain visible.
export function workingId(state) {
  const ids = workingNodeIds(state)
  if (!ids.size) return null
  const building = [...ids].filter(id => nodeActivityStatus(state?.nodes?.[id], state) === NODE_ACTIVITY.BUILDING)
  return Math.max(...(building.length ? building : [...ids]))
}

export function nodeClass(node, state, workIds) {
  const cls = ['node-card', `s-${node.status}`]
  // THE ACTIVITY CLASS, beside the terminal-status one rather than instead of it. `s-${status}` is
  // the LIFECYCLE (pending | evaluated | failed | the synthetic building) and it is what the card's
  // body colour has always keyed on — which is why a node waiting for a slot and a node three hours
  // into training were the SAME slate `s-pending` wash, distinguished only by a 10px text chip. The
  // two are different questions and now have different classes: `.working` still says "this run is
  // spending something on this node", while `a-building` / `a-evaluating` / `a-queued` say WHICH
  // lane is spending it. A queued node is deliberately NOT `.working` — nothing is running for it,
  // and pulsing it amber is the "the box looks busy" lie `narration.js::pendingWork` was written
  // about.
  // Only while the run CAN be doing work. The `a-evaluating` rail is styled as "a process of ours
  // is running", and the generation-scoped activity row it keys on survives an engine crash — so a
  // dead, paused or stopped run kept the amber running rail while the same card's own chip (via
  // `nodeActivityView`, which consults this exact predicate) said "Evaluation interrupted · engine
  // stopped". One card, two opposite claims. Without the class the card keeps its lifecycle
  // `s-${status}` wash — the pre-cursor appearance for a run nothing is running. The lane CENSUSES
  // (group dots, MiniMap) deliberately keep counting by raw activity: composition is a fact about
  // the members either way, and it is the styled "running now" claim that must not outlive the run.
  if (runCanWork(state)) cls.push(`a-${nodeActivityStatus(node, state)}`)
  if (node.id === state.best_node_id) cls.push('best')
  if (node.feasible === false) cls.push('infeasible')
  const working = workIds instanceof Set ? workIds.has(Number(node.id)) : node.id === workIds
  if (working) cls.push('working')
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
