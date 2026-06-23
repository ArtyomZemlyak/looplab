// Semantic grouping of DAG nodes (UI #7). Pure helpers: pick a group key per node under a chosen
// mode, build groups, compute a soft enclosing region (BubbleSets-lite hull) from member rects,
// and reroute edges when a group collapses into a super-node. No React, no side effects.
import { fmt } from './util.js'

export const GROUP_MODES = [
  ['theme', 'theme'], ['operator', 'operator'], ['metric', 'metric'], ['niche', 'niche'], ['none', 'none'],
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
    return keys.map(k => `${k}=${Math.round(Number(p[k]))}`).join(' · ')
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
export function groupColor(key) {
  let h = 0; const s = String(key)
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return TINTS[h % TINTS.length]
}

// --- geometry: a soft enclosing region for a set of node rectangles -----------------------------
function convexHull(pts) {
  if (pts.length < 3) return pts.slice()
  const p = pts.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
  const lower = []
  for (const q of p) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], q) <= 0) lower.pop(); lower.push(q) }
  const upper = []
  for (let i = p.length - 1; i >= 0; i--) { const q = p[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], q) <= 0) upper.pop(); upper.push(q) }
  return lower.slice(0, -1).concat(upper.slice(0, -1))
}

function expandFromCentroid(hull, pad) {
  const cx = hull.reduce((s, p) => s + p[0], 0) / hull.length
  const cy = hull.reduce((s, p) => s + p[1], 0) / hull.length
  return hull.map(([x, y]) => {
    const dx = x - cx, dy = y - cy, d = Math.hypot(dx, dy) || 1
    return [x + (dx / d) * pad, y + (dy / d) * pad]
  })
}

// Catmull-Rom -> cubic bezier, closed loop, for an organic boundary.
function smoothClosed(pts) {
  const n = pts.length
  if (n < 3) return ''
  let d = `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)} `
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n], p1 = pts[i], p2 = pts[(i + 1) % n], p3 = pts[(i + 2) % n]
    const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6
    const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6
    d += `C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p2[0].toFixed(1)} ${p2[1].toFixed(1)} `
  }
  return d + 'Z'
}

function roundRectPath(x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2)
  return `M ${x + r} ${y} H ${x + w - r} Q ${x + w} ${y} ${x + w} ${y + r} V ${y + h - r} ` +
    `Q ${x + w} ${y + h} ${x + w - r} ${y + h} H ${x + r} Q ${x} ${y + h} ${x} ${y + h - r} V ${y + r} Q ${x} ${y} ${x + r} ${y} Z`
}

// rects: [{x,y,w,h}] in absolute (flow) coords. Returns the region node geometry: its absolute
// origin {x,y}, its {w,h}, and an SVG path string in LOCAL coords (relative to that origin).
export function regionGeometry(rects, pad = 28) {
  const xs = rects.flatMap(r => [r.x, r.x + r.w]); const ys = rects.flatMap(r => [r.y, r.y + r.h])
  const minX = Math.min(...xs) - pad, minY = Math.min(...ys) - pad
  const maxX = Math.max(...xs) + pad, maxY = Math.max(...ys) + pad
  const w = maxX - minX, h = maxY - minY
  let path
  if (rects.length <= 2) {
    path = roundRectPath(pad / 2, pad / 2, w - pad, h - pad, 22)
  } else {
    const corners = rects.flatMap(r => [[r.x, r.y], [r.x + r.w, r.y], [r.x + r.w, r.y + r.h], [r.x, r.y + r.h]])
      .map(([x, y]) => [x - minX, y - minY])
    path = smoothClosed(expandFromCentroid(convexHull(corners), pad * 0.7))
  }
  return { x: minX, y: minY, w, h, path }
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
