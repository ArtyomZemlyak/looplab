// The run's ROOT operations, as the operator reads them — the pure half of the Operations panel.
//
// WHY THIS SURFACE EXISTS. Every trace view LoopLab shipped projects by node, and a RUN-level agent
// has no node: the Researcher's `propose` span is a root with no `node_id`, because the idea exists
// before the node built from it does. So "show me what the Researcher was thinking" had no answer
// anywhere in the product. Measured on `runs/rubert-dr-0807` (2026-08-11): 15 `propose` operations
// holding 493 generations and 944 tool calls, none of them reachable from any screen. The one bucket
// that held them server-side (`traceview.build_trace_view`'s `unscoped`) was tail-capped to the last
// 1,024 spans — 3 of the 15 survived — and no UI module read the field at all.
//
// The rule this model encodes: an operation with no node is a FACT about that operation ("this is
// run-level work"), never missing data. It gets its own group and its own label; it is never hidden,
// never folded into a node, and never rendered as "unknown".

// Root span name -> what the operator calls it. Names are the engine's own span names
// (`core/tracing` call sites); an unlisted name renders verbatim rather than being dropped, because
// a new engine operation must show up here the day it ships, not the day someone updates a table.
export const OPERATION_LABELS = Object.freeze({
  propose: 'Researcher · propose',
  plan: 'Researcher · plan',
  stages: 'Developer · stages',
  create_node: 'Developer · build node',
  materialize_node: 'Developer · materialize',
  card_build: 'Card build',
  evaluate: 'Evaluate',
  deep_research: 'Deep research',
  strategist_consult: 'Strategist',
  foresight_rank: 'Foresight ranking',
  hyp_prioritize: 'Hypothesis prioritisation',
  hypothesis_merge: 'Hypothesis merge',
  concept_coverage: 'Concept coverage',
  lessons_distill: 'Lessons · distill',
  lessons_refresh: 'Lessons · refresh',
  lessons_reconcile: 'Lessons · reconcile',
  report: 'Report',
  setup: 'Run setup',
})

// Operations whose work IS an agent thinking — the ones worth opening for their reasoning rather
// than for their outcome. Drives the default filter, so the Researcher is one click from the panel
// opening instead of buried under 99 bookkeeping rows (the `lessons_*` family alone is 99 of the
// 176 operations on the reference run).
export const AGENTIC_OPERATIONS = Object.freeze(new Set([
  'propose', 'plan', 'create_node', 'deep_research', 'strategist_consult',
  'hypothesis_merge', 'hyp_prioritize', 'foresight_rank', 'card_build', 'evaluate', 'report',
]))

export const operationLabel = name => OPERATION_LABELS[name] || String(name || 'operation')

/** Run-level (no node) or node-scoped. The whole point of the panel is that both are visible. */
export const isRunLevel = op => op?.node_id === null || op?.node_id === undefined

/**
 * Rows ready to render: label, scope, and whether opening the trace is worth it.
 *
 * `agenticOnly` keeps only operations that carry agent reasoning. It is a FILTER over rows the
 * server already sent, never a second request — the listing is light by construction, so the count
 * of what the filter hides can always be stated honestly.
 */
export function operationRows(payload, { agenticOnly = true, nodeId = null } = {}) {
  const all = Array.isArray(payload?.operations) ? payload.operations : []
  const scoped = nodeId === null || nodeId === undefined
    ? all
    : all.filter(op => String(op?.node_id) === String(nodeId))
  const kept = agenticOnly ? scoped.filter(op => AGENTIC_OPERATIONS.has(op?.name)) : scoped
  const rows = kept.map(op => ({
    ...op,
    label: operationLabel(op?.name),
    runLevel: isRunLevel(op),
    // An operation with no spans of its own carries no trace to open. Saying so up front is what
    // stops a click that lands on an empty tree and reads as a broken view.
    openable: Number(op?.spans || 0) > 1 && !!op?.trace_id,
  }))
  return { rows, hiddenByFilter: Math.max(0, scoped.length - kept.length), total: all.length }
}

/** Run-level first, then by node — the Researcher is what the operator came here for. */
export function groupOperations(rows = []) {
  const runLevel = rows.filter(row => row.runLevel)
  const byNode = new Map()
  for (const row of rows) {
    if (row.runLevel) continue
    const key = String(row.node_id)
    if (!byNode.has(key)) byNode.set(key, [])
    byNode.get(key).push(row)
  }
  const nodes = [...byNode.entries()]
    .sort((a, b) => Number(b[0]) - Number(a[0]))
    .map(([node_id, ops]) => ({ node_id, ops }))
  return { runLevel, nodes }
}

/**
 * The omission receipt, in words. Absent when nothing was dropped — a receipt that always prints
 * teaches the reader to ignore it, and this one has to be believed the day it matters.
 */
export function operationsNotice(payload, hiddenByFilter = 0) {
  const projection = payload?.projection || {}
  const parts = []
  if (projection.unavailable === true) return 'Operation listing unavailable for this run.'
  const omitted = Number(projection.omitted_operations || 0)
  if (omitted > 0) parts.push(`${omitted} older operation${omitted === 1 ? '' : 's'} not listed`)
  if (hiddenByFilter > 0) parts.push(`${hiddenByFilter} bookkeeping operation${hiddenByFilter === 1 ? '' : 's'} hidden`)
  return parts.length ? `${parts.join('; ')}.` : ''
}
