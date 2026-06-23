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

// Layered layout that treats a collapsed group as ONE entity (semantic zoom). Returns positions
// keyed by entity id: `n:<id>` for a visible node, `super:<key>` for a collapsed group. When no
// group is collapsed it degenerates to the per-node layout above (one entity per node).
export function layoutWithGroups(nodes, { collapsed = new Set(), nodeGroup = new Map() } = {}) {
  const ent = (id) => { const k = nodeGroup.get(id); return (k != null && collapsed.has(k)) ? `super:${k}` : `n:${id}` }
  const parents = {}, ents = new Set()
  Object.values(nodes).forEach(n => {
    const e = ent(n.id); ents.add(e); parents[e] ||= new Set()
      ; (n.parent_ids || []).forEach(p => {
        if (!(p in nodes)) return
        const pe = ent(p); ents.add(pe); parents[pe] ||= new Set()
        if (pe !== e) parents[e].add(pe)
      })
  })
  const depth = {}
  const dep = (e) => {
    if (e in depth) return depth[e]
    depth[e] = 0   // cycle guard (DAG, so safe)
    const ps = [...(parents[e] || [])]
    depth[e] = ps.length ? 1 + Math.max(...ps.map(dep)) : 0
    return depth[e]
  }
  ents.forEach(dep)
  const byDepth = {}
  ents.forEach(e => { (byDepth[depth[e]] ||= []).push(e) })
  const XS = 230, YS = 150, pos = {}
  Object.keys(byDepth).map(Number).sort((a, b) => a - b).forEach(d => {
    const arr = byDepth[d].sort()
    const offset = -(arr.length - 1) / 2
    arr.forEach((e, i) => { pos[e] = { x: (offset + i) * XS, y: d * YS } })
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
async function send(path, method, body) {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}

// ---- ClearML-style project API ----
export const listProjects = () => get('/api/projects')
export const createProject = (name, parent_id = null) => post('/api/projects', { name, parent_id })
export const patchProject = (id, body) => send(`/api/projects/${id}`, 'PATCH', body)
export const deleteProject = (id) => send(`/api/projects/${id}`, 'DELETE')
export const assignRun = (runId, project_id) => post(`/api/runs/${encodeURIComponent(runId)}/project`, { project_id })
