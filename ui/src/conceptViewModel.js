// Pure helpers for the in-run Concept view (View 1) — no React, unit-tested with `node --test`.
// The projection (tree) + per-concept metrics come from GET /api/runs/:id/concepts, single-sourced in
// Python (project_hierarchy / project_lens / concept_metrics). These helpers place experiments under
// concepts and drive the configurable, ClearML-style metric table.

// The configurable table columns, in stable order. `delta` marks a signed baseline-relative column
// (colored up/down). The UI picks a subset (add/remove); this list is the source of truth.
export const CONCEPT_COLUMNS = [
  { key: 'touched', label: 'exps' },
  { key: 'evaluated', label: 'eval' },
  { key: 'best', label: 'best' },
  { key: 'mean', label: 'mean' },
  { key: 'delta_best', label: 'Δ best', delta: true },
  { key: 'delta_mean', label: 'Δ mean', delta: true },
  { key: 'first_touch', label: 'first@' },
]
export const DEFAULT_COLUMNS = ['touched', 'best', 'delta_best']

// Invert node_concepts -> {conceptId: sorted-unique nodeIds}, canonicalized through the consolidation
// rename map. Many-to-many: a node appears under EVERY concept it carries (never divided); the tree
// nesting (from the endpoint) supplies ancestry, so a node is listed at its EXACT tagged ids only.
export function experimentsByConcept(nodeConcepts = {}, rename = {}) {
  const acc = {}
  for (const [nid, ids] of Object.entries(nodeConcepts || {})) {
    const id = Number(nid)
    for (const raw of (ids || [])) {
      const c = rename[raw] || raw
      if (!c) continue
      ;(acc[c] ||= new Set()).add(id)
    }
  }
  const out = {}
  for (const [c, set] of Object.entries(acc)) out[c] = [...set].sort((a, b) => a - b)
  return out
}

// signed-delta tone for cell coloring (null/zero-safe): 'up' better, 'down' worse, 'flat' neutral.
export function deltaTone(v) {
  if (v == null || v === 0) return 'flat'
  return v > 0 ? 'up' : 'down'
}

// format a metric cell (tabular): integer counts as-is, metrics to 3dp, null -> ·
export function fmtCell(v) {
  if (v == null) return '·'
  return Number.isInteger(v) ? String(v) : Number(v).toFixed(3)
}

// A stable DFS order of the tree's concept nodes honoring an `expanded` set (roots always shown; a
// node's children shown only when it is expanded). Returns [{id, depth, hasChildren}] for a flat,
// virtualizable render. Pure over (tree, expanded).
export function visibleConceptRows(tree, expanded = new Set()) {
  const rows = []
  const nodes = (tree && tree.nodes) || {}
  const walk = (id, depth) => {
    const n = nodes[id]
    if (!n) return
    const kids = n.children || []
    rows.push({ id, depth, hasChildren: kids.length > 0 })
    if (expanded.has(id)) for (const k of kids) walk(k, depth + 1)
  }
  for (const r of (tree && tree.roots) || []) walk(r, 0)
  return rows
}

// The short leaf label for a concept id (last path segment) — the tree already conveys the ancestry.
export function conceptLeaf(id) {
  const s = String(id || '')
  const i = s.lastIndexOf('/')
  return i < 0 ? s : s.slice(i + 1)
}
