// Pure helpers for the in-run Concept view (View 1) -- no React, unit-tested with `node --test`.
// The projection (tree) + per-concept metrics come from GET /api/runs/:id/concepts, single-sourced in
// Python (project_hierarchy / project_lens / concept_metrics). These helpers place experiments under
// concepts and drive the configurable, ClearML-style metric table.
import { canonicalId, conceptMap } from './conceptId.js'

// The configurable table columns, in stable order. `delta` marks a signed baseline-relative column
// (colored up/down). The UI picks a subset (add/remove); this list is the source of truth.
export const CONCEPT_COLUMNS = [
  { key: 'touched', label: 'exps' },
  { key: 'evaluated', label: 'eval' },
  { key: 'best', label: 'best' },
  { key: 'mean', label: 'mean' },
  { key: 'delta_best', label: '\u0394 best', delta: true },
  { key: 'delta_mean', label: '\u0394 mean', delta: true },
  { key: 'first_touch', label: 'first@' },
]
export const DEFAULT_COLUMNS = ['touched', 'best', 'delta_best']

// CODEX AGENT: `co_occurs` is materialized from the CURRENT membership projection, while the other
// relationship edges are persisted claims. Use neutral "projected" wording for the shared edge view
// and disclose co-occurrence derivation explicitly instead of calling every displayed link recorded.
export function relationshipProjectionCopy(rels = []) {
  const source = Array.isArray(rels) ? rels : []
  const relationTypes = source.map(rel => rel === 'co_occurs' ? 'co-occurrence' : rel).join(', ')
    || 'relationship'
  return {
    relationTypes,
    linkDescription: `projected ${relationTypes} links`,
    derivationNote: source.includes('co_occurs')
      ? 'Co-occurrence links are derived from the concept memberships in this frame; they are not recorded edge claims.'
      : '',
  }
}

const MAX_VISIBLE_ROWS = 10_000
const MAX_TREE_DEPTH = 256
const MAX_TREE_REFERENCES = 50_000

function isRecord(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

function isConceptId(value) {
  return typeof value === 'string' && value.length > 0
}

function finishRows(rows, reasons, unavailable = false) {
  const reasonList = [...reasons].sort()
  const state = unavailable ? 'unavailable' : (reasonList.length ? 'partial' : 'current')
  Object.defineProperty(rows, 'projectionStatus', {
    value: Object.freeze({ state, reasons: Object.freeze(reasonList) }),
    enumerable: false,
  })
  return rows
}

// Invert node_concepts -> {conceptId: sorted-unique nodeIds}, canonicalized through the consolidation
// rename map. Many-to-many: a node appears under EVERY concept it carries (never divided); the tree
// nesting (from the endpoint) supplies ancestry, so a node is listed at its EXACT tagged ids only.
export function experimentsByConcept(nodeConcepts = {}, rename = {}) {
  const out = conceptMap()
  if (!isRecord(nodeConcepts)) return out
  const renameMap = isRecord(rename) ? rename : {}
  const acc = new Map()
  for (const [nid, ids] of Object.entries(nodeConcepts)) {
    if (!/^(0|[1-9]\d*)$/.test(nid) || !Array.isArray(ids)) continue
    const id = Number(nid)
    if (!Number.isSafeInteger(id)) continue
    for (const raw of ids) {
      if (!isConceptId(raw)) continue
      const conceptId = canonicalId(raw, renameMap)
      if (!isConceptId(conceptId)) continue
      // Keep untrusted concept ids in Map/null-prototype storage so prototype names remain data.
      let nodeIds = acc.get(conceptId)
      if (!nodeIds) {
        nodeIds = new Set()
        acc.set(conceptId, nodeIds)
      }
      nodeIds.add(id)
    }
  }
  for (const [conceptId, nodeIds] of acc) {
    out[conceptId] = [...nodeIds].sort((a, b) => a - b)
  }
  return out
}

// signed-delta tone for cell coloring (null/zero-safe): 'up' better, 'down' worse, 'flat' neutral.
export function deltaTone(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value === 0) return 'flat'
  return value > 0 ? 'up' : 'down'
}

// Format a metric cell (tabular): integer counts as-is, metrics to 3dp, unavailable -> middle dot.
export function fmtCell(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '\u00b7'
  return Number.isInteger(value) ? String(value) : value.toFixed(3)
}

// A stable DFS order of the tree's concept nodes honoring an `expanded` set (roots always shown; a
// node's children shown only when it is expanded). Returns [{id, depth, hasChildren}] for a flat,
// virtualizable render. `rows.projectionStatus` is non-enumerable so the array contract stays intact:
// current means complete, partial means malformed/truncated input, unavailable means no safe rows.
export function visibleConceptRows(tree, expanded = new Set()) {
  const rows = []
  const reasons = new Set()
  if (!isRecord(tree)) return finishRows(rows, new Set(['invalid-tree']), true)
  if (!isRecord(tree.nodes)) return finishRows(rows, new Set(['invalid-nodes']), true)
  if (!Array.isArray(tree.roots)) return finishRows(rows, new Set(['invalid-roots']), true)

  const nodes = tree.nodes
  const open = expanded instanceof Set ? expanded : new Set()
  if (!(expanded instanceof Set)) reasons.add('invalid-expanded')
  const seen = new Set()
  let referencesLeft = MAX_TREE_REFERENCES

  const validReferences = (value, kind) => {
    if (!Array.isArray(value)) {
      reasons.add(`invalid-${kind}`)
      return []
    }
    const valid = []
    for (const id of value) {
      if (referencesLeft <= 0) {
        reasons.add('reference-limit')
        break
      }
      referencesLeft -= 1
      if (!isConceptId(id)) {
        reasons.add(`invalid-${kind}-id`)
      } else if (!Object.hasOwn(nodes, id) || !isRecord(nodes[id])) {
        // Own-property checks also prevent inherited "constructor" roots from becoming phantom rows.
        reasons.add('missing-node')
      } else {
        valid.push(id)
      }
    }
    return valid
  }

  const roots = validReferences(tree.roots, 'root')
  const stack = roots.slice().reverse().map(id => ({ id, depth: 0, path: null }))
  while (stack.length) {
    if (rows.length >= MAX_VISIBLE_ROWS) {
      reasons.add('row-limit')
      break
    }
    const { id, depth, path } = stack.pop()
    let ancestor = path
    let cyclic = false
    while (ancestor) {
      if (ancestor.id === id) {
        cyclic = true
        break
      }
      ancestor = ancestor.parent
    }
    if (cyclic) {
      reasons.add('cycle')
      continue
    }
    if (seen.has(id)) {
      reasons.add('duplicate-reference')
      continue
    }

    const node = nodes[id]
    const children = validReferences(node.children, 'children')
    seen.add(id)
    rows.push({ id, depth, hasChildren: children.length > 0 })
    if (!open.has(id) || children.length === 0) continue
    if (depth >= MAX_TREE_DEPTH) {
      reasons.add('depth-limit')
      continue
    }

    // Iterate within hard depth/reference budgets so a malformed projection cannot block rendering.
    const nextPath = { id, parent: path }
    for (let i = children.length - 1; i >= 0; i -= 1) {
      stack.push({ id: children[i], depth: depth + 1, path: nextPath })
    }
  }
  return finishRows(rows, reasons, rows.length === 0 && reasons.size > 0)
}

// The short leaf label for a concept id (last path segment) -- the tree already conveys the ancestry.
export function conceptLeaf(id) {
  return String(id || '').split('/').pop()
}
