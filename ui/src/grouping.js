// Semantic grouping of DAG nodes (UI #7). Pure helpers: pick a group key per node under a chosen
// mode, build groups, compute a soft enclosing region (BubbleSets-lite hull) from member rects,
// and reroute edges when a group collapses into a super-node. No React, no side effects.
import { fmt } from './util.js'

export const GROUP_MODES = [
  ['theme', 'direction'], ['operator', 'operator'], ['metric', 'metric'], ['niche', 'niche'], ['none', 'none'],
]

// Per-mode context (e.g. metric tercile thresholds) computed once over the node set.
function makeCtx(nodes, mode) {
  if (mode === 'metric') {
    const ms = nodes.map(n => n.confirmed_mean ?? n.metric).filter(v => v != null).sort((a, b) => a - b)
    if (ms.length) {
      // index over [0, len-1] so the top tercile boundary never lands on the max (which would
      // leave the '> t2' bucket empty for sizes where floor(2/3·len) == len-1)
      const q = (p) => ms[Math.round(p * (ms.length - 1))]
      const t1 = q(1 / 3), t2 = q(2 / 3)
      return { bucketOf: (m) => m <= t1 ? `≤ ${fmt(t1, 2)}` : (m <= t2 ? `${fmt(t1, 2)} – ${fmt(t2, 2)}` : `> ${fmt(t2, 2)}`) }
    }
  }
  return {}
}

export function groupKey(node, mode, ctx) {
  if (mode === 'theme') return node.idea?.theme || null
  if (mode === 'operator') return node.operator || null
  if (mode === 'metric') { const m = node.confirmed_mean ?? node.metric; return (m == null || !ctx.bucketOf) ? null : ctx.bucketOf(m) }
  if (mode === 'niche') {
    const p = node.idea?.params || {}; const keys = Object.keys(p).sort()
    if (!keys.length) return null
    // Round numeric params, but keep a non-numeric param's raw value: Math.round(Number('adam')) is
    // NaN, which would collapse every distinct string value (optimizer=adam, =sgd) into one bucket.
    return keys.map(k => { const n = Number(p[k]); return `${k}=${Number.isFinite(n) ? Math.round(n) : p[k]}` }).join(' · ')
  }
  return null
}

// Map<key, nodeId[]> for the chosen mode. Nodes with no key (e.g. no theme) are left ungrouped.
export function computeGroups(nodesObj, mode) {
  const nodes = Object.values(nodesObj || {})
  const ctx = makeCtx(nodes, mode)
  const groups = new Map()
  nodes.forEach(n => {
    const k = groupKey(n, mode, ctx)
    if (k == null) return
    if (!groups.has(k)) groups.set(k, [])
    groups.get(k).push(n.id)
  })
  return groups
}

// id -> key index, for fast endpoint lookup during collapse.
export function nodeGroupMap(groups) {
  const m = new Map()
  groups.forEach((ids, key) => ids.forEach(id => m.set(id, key)))
  return m
}

// Low-chroma, deterministic tint per group key — identity is carried by the LABEL; colour is a
// faint, decorative wash (kept desaturated on purpose so many groups never become a rainbow).
const TINTS = ['#6f8bb0', '#8a7bb0', '#6fae97', '#b0936f', '#a87da8', '#6fa3b0', '#9aa06f']
// When the caller knows the group's ORDER (its index in the active grouping), assign tints in sequence
// so adjacent groups never collide on the same hue — a curated, distinct ramp instead of a hash that
// can land two neighbours on the same colour. Falls back to the stable hash when no index is given
// (e.g. the cross-run Map, which has no single ordering).
export function groupColor(key, idx) {
  if (typeof idx === 'number' && idx >= 0) return TINTS[idx % TINTS.length]
  let h = 0; const s = String(key)
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return TINTS[h % TINTS.length]
}

// --- geometry: a soft enclosing region for a set of node rectangles -----------------------------
function roundRectPath(x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2)
  return `M ${x + r} ${y} H ${x + w - r} Q ${x + w} ${y} ${x + w} ${y + r} V ${y + h - r} ` +
    `Q ${x + w} ${y + h} ${x + w - r} ${y + h} H ${x + r} Q ${x} ${y + h} ${x} ${y + h - r} V ${y + r} Q ${x} ${y} ${x + r} ${y} Z`
}

// rects: [{x,y,w,h}] in absolute (flow) coords. Returns the region node geometry: its absolute
// origin {x,y}, its {w,h}, and an SVG path string in LOCAL coords (relative to that origin).
// A clean rounded rectangle reads as a clear "block" even with dozens of members — far more legible
// than an organic hull, which goes spiky/self-intersecting once a cluster spans several rows.
export function regionGeometry(rects, pad = 26) {
  const xs = rects.flatMap(r => [r.x, r.x + r.w]); const ys = rects.flatMap(r => [r.y, r.y + r.h])
  const minX = Math.min(...xs) - pad, minY = Math.min(...ys) - pad
  const maxX = Math.max(...xs) + pad, maxY = Math.max(...ys) + pad
  const w = maxX - minX, h = maxY - minY
  return { x: minX, y: minY, w, h, path: roundRectPath(pad / 2, pad / 2, w - pad, h - pad, 20) }
}

// Collapse: replace each collapsed group's members with one super-node id. Returns hidden member
// ids + the deduped rerouted edge list (edges internal to one collapsed group are dropped).
export const superId = (key) => `super:${key}`
export function rerouteForCollapse(nodesObj, collapsed, nodeGroup) {
  const hidden = new Set()
  const endpoint = (id) => { const k = nodeGroup.get(id); return (k != null && collapsed.has(k)) ? superId(k) : `n:${id}` }
  Object.values(nodesObj).forEach(n => { const k = nodeGroup.get(n.id); if (k != null && collapsed.has(k)) hidden.add(n.id) })
  const seen = new Set(); const edges = []
  Object.values(nodesObj).forEach(n => (n.parent_ids || []).forEach(p => {
    if (!(p in nodesObj)) return
    const S = endpoint(p), T = endpoint(n.id)
    if (S === T) return                       // internal to a collapsed group -> drop
    const key = S + '>' + T
    if (seen.has(key)) return
    seen.add(key)
    edges.push({ source: S, target: T, srcId: p, dstId: n.id })
  }))
  return { hidden, edges }
}

// Phase 0 (auto-collapse): a pure policy that FILLS the collapsed Set — rendering reuses the existing
// collapse pipeline (rerouteForCollapse + groupAggregate + super-nodes), so this adds no new visuals.
// Folds every SETTLED group (no pending member) of meaningful size, EXCEPT the ones you're watching:
// the working node's group, the selected group, and the champion's group are hard never-collapse. Only
// meaningful in theme/niche mode (operator = one dominant group, metric = full-height terciles).
export function autoCollapseSet(nodesObj, groups, { mode, bestId = null, selectedId = null, workId = null, minSize = 3 } = {}) {
  const out = new Set()
  if (mode !== 'theme' && mode !== 'niche') return out
  const ng = nodeGroupMap(groups)
  const keep = new Set([ng.get(bestId), ng.get(selectedId), ng.get(workId)].filter(k => k != null))
  groups.forEach((ids, key) => {
    if (keep.has(key) || ids.length < minSize) return
    const settled = ids.every(id => { const s = nodesObj[id]?.status; return s && s !== 'pending' })
    if (settled) out.add(key)
  })
  return out
}

// Phase 2 (merge bridges): an edge ENTERING a merge node (a node with ≥2 parents) crosses between
// packed group cells, so it must be routed orthogonally (a "leader" edge) instead of as a bezier that
// would cut straight through a cell. Pure predicate so the classification is unit-testable.
export function isMergeEntryEdge(childNode) {
  return !!(childNode && (childNode.parent_ids || []).length > 1)
}

// Aggregate stats for a collapsed group's super-node card.
export function groupAggregate(memberIds, nodesObj, direction) {
  const better = direction === 'min' ? (a, b) => a < b : (a, b) => a > b
  let best = null; const series = []; const status = { evaluated: 0, failed: 0, pending: 0 }
  memberIds.map(id => nodesObj[id]).filter(Boolean).sort((a, b) => a.id - b.id).forEach(n => {
    const m = n.confirmed_mean ?? n.metric
    if (m != null) { series.push(m); if (best == null || better(m, best)) best = m }
    status[n.status] = (status[n.status] || 0) + 1
  })
  return { best, series, status, count: memberIds.length }
}

// A direction chip is a semantic filter, not just decoration. Collapsed cards therefore aggregate
// only matching experiments while retaining the full membership count as context. This prevents an
// operator/theme super-node from presenting a cross-theme best as if it belonged to the active theme.
export function themeFilteredGroupAggregate(memberIds, nodesObj, direction, themeFilter = null) {
  const totalCount = memberIds.length
  const matchedIds = themeFilter
    ? memberIds.filter(id => nodesObj[id]?.idea?.theme === themeFilter)
    : memberIds
  return {
    ...groupAggregate(matchedIds, nodesObj, direction),
    matchedIds,
    totalCount,
    matchedCount: matchedIds.length,
    filterActive: !!themeFilter,
    themeFilter,
  }
}
