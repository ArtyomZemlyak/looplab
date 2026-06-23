// Shared helpers: metric formatting, semantic status, and a dependency-free layered DAG layout.

export function fmt(v, p = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (typeof v !== 'number') return String(v)
  const a = Math.abs(v)
  if (a !== 0 && (a < 1e-3 || a >= 1e6)) return v.toExponential(2)
  return Number(v.toPrecision(p)).toString()
}

export function fmtInt(v) {
  if (v === null || v === undefined) return '—'
  return Number(v).toLocaleString()
}

// Is this node the one currently being evaluated? (pending + the highest-id pending, and the run
// isn't paused/finished) — a good-enough "working" heuristic for the live pulse.
export function workingId(state) {
  if (!state || state.finished || state.paused) return null
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

// Longest-path layered layout from parent_ids (handles merges/DAG). Deterministic, no deps.
export function layout(nodes) {
  const ids = Object.keys(nodes).map(Number)
  const depth = {}
  const dep = (id) => {
    if (id in depth) return depth[id]
    const n = nodes[id]
    const ps = (n.parent_ids || []).filter(p => p in nodes)
    depth[id] = ps.length ? 1 + Math.max(...ps.map(dep)) : 0
    return depth[id]
  }
  ids.forEach(dep)
  const byDepth = {}
  ids.forEach(id => { (byDepth[depth[id]] ||= []).push(id) })
  const XS = 230, YS = 150
  const pos = {}
  Object.keys(byDepth).map(Number).sort((a, b) => a - b).forEach(d => {
    const arr = byDepth[d].sort((a, b) => a - b)
    const offset = -(arr.length - 1) / 2
    arr.forEach((id, i) => { pos[id] = { x: (offset + i) * XS, y: d * YS } })
  })
  return pos
}

export function phaseLabel(state) {
  return state?.phase || (state?.finished ? 'finished' : '—')
}

export const CONTROL = {
  pause: (rid) => post(`/api/runs/${rid}/control`, { type: 'pause', data: {} }),
  resume: (rid) => post(`/api/runs/${rid}/control`, { type: 'resume', data: {} }),
  abort: (rid) => post(`/api/runs/${rid}/control`, { type: 'run_abort', data: { reason: 'ui' } }),
  nodeAbort: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'node_abort', data: { node_id: id, reason: 'ui' } }),
  approve: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'approval_granted', data: { node_id: id } }),
  ratify: (rid) => post(`/api/runs/${rid}/control`, { type: 'spec_approved', data: {} }),
  hint: (rid, text) => post(`/api/runs/${rid}/control`, { type: 'hint', data: { text } }),
  budget: (rid, sec) => post(`/api/runs/${rid}/control`, { type: 'budget_extend', data: { max_eval_seconds: sec } }),
  forceConfirm: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'force_confirm', data: { node_id: id } }),
  forceAblate: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'force_ablate', data: { node_id: id } }),
  fork: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'fork', data: { from_node_id: id } }),
  annotate: (rid, id, text) => post(`/api/runs/${rid}/control`, { type: 'annotation', data: { node_id: id, text } }),
  promote: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'promote', data: { node_id: id, alias: 'champion' } }),
}

export async function get(path) {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
export async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
export async function putText(path, text) {
  const r = await fetch(path, { method: 'PUT', headers: { 'Content-Type': 'text/plain' }, body: text })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
