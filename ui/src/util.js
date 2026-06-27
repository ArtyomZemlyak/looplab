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

// Epoch-SECONDS timestamp helpers (run mtime/created come from os.stat → seconds, not ms).
export function fmtDate(sec, withTime = true) {
  if (!sec) return '—'
  return new Date(sec * 1000).toLocaleString(undefined, withTime
    ? { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: 'numeric' })
}
export function fmtAgo(sec) {
  if (!sec) return ''
  const d = Date.now() / 1000 - sec
  if (d < 60) return 'just now'
  if (d < 3600) return Math.floor(d / 60) + 'm ago'
  if (d < 86400) return Math.floor(d / 3600) + 'h ago'
  if (d < 7 * 86400) return Math.floor(d / 86400) + 'd ago'
  return new Date(sec * 1000).toLocaleDateString()
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

// Layered ("Sugiyama-lite") layout that treats a collapsed group as ONE entity (semantic zoom).
// Returns positions keyed by entity id: `n:<id>` for a visible node, `super:<key>` for a collapsed
// group. Beyond depth-by-longest-path, it does two things the naïve version didn't:
//   1. orders nodes WITHIN each layer by an iterated barycenter (median) heuristic, which pulls
//      parents and children into vertical alignment and sharply cuts edge crossings; and
//   2. when grouping is active, keeps a group's members contiguous in every layer (clusters sort
//      by their mean barycenter) — so "group by" visibly re-arranges the nodes into clusters
//      instead of only drawing a hull over wherever they happened to land.
// With no grouping the clustering is a no-op (each node is its own singleton cluster), so it
// degenerates to a plain crossing-minimised layered layout.
export function layoutWithGroups(nodes, { collapsed = new Set(), nodeGroup = new Map() } = {}) {
  const ent = (id) => { const k = nodeGroup.get(id); return (k != null && collapsed.has(k)) ? `super:${k}` : `n:${id}` }
  const parents = {}, children = {}, ents = new Set()
  Object.values(nodes).forEach(n => {
    const e = ent(n.id); ents.add(e); parents[e] ||= new Set(); children[e] ||= new Set()
      ; (n.parent_ids || []).forEach(p => {
        if (!(p in nodes)) return
        const pe = ent(p); ents.add(pe); parents[pe] ||= new Set(); children[pe] ||= new Set()
        if (pe !== e) { parents[e].add(pe); children[pe].add(e) }
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
  const layers = {}
  ents.forEach(e => { (layers[depth[e]] ||= []).push(e) })
  const depths = Object.keys(layers).map(Number).sort((a, b) => a - b)

  // Cluster key per entity (keeps a group's members adjacent). Ungrouped nodes get a unique key so
  // they order freely by barycenter; a collapsed group's super-node carries its own key.
  const clusterOf = (e) => {
    if (e.startsWith('super:')) return e.slice(6)
    const k = nodeGroup.get(Number(e.slice(2)))
    return k != null ? String(k) : ' ' + e
  }
  const median = (a) => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2 }

  // Seed order: cluster-key then entity id, so members of a group start as one contiguous block.
  const order = {}
  depths.forEach(d => {
    layers[d].sort((a, b) => clusterOf(a).localeCompare(clusterOf(b)) || a.localeCompare(b, undefined, { numeric: true }))
    layers[d].forEach((e, i) => { order[e] = i })
  })

  // Iterated median sweeps (down using parents, up using children). Sorting first by the cluster's
  // mean barycenter and only then by the entity's own keeps clusters whole while still aligning
  // each node under its neighbours — the crossing-reduction win without breaking the grouping.
  const sweep = (downward) => {
    (downward ? depths : [...depths].reverse()).forEach(d => {
      const layer = layers[d]
      const bary = {}
      layer.forEach(e => {
        const nb = [...((downward ? parents : children)[e] || [])]
        bary[e] = nb.length ? median(nb.map(n => order[n] ?? 0)) : order[e]
      })
      const acc = {}
      layer.forEach(e => { const c = clusterOf(e); (acc[c] ||= []).push(bary[e]) })
      const cmean = {}; Object.entries(acc).forEach(([c, a]) => { cmean[c] = a.reduce((s, x) => s + x, 0) / a.length })
      layer.sort((a, b) => (cmean[clusterOf(a)] - cmean[clusterOf(b)])
        || (clusterOf(a) === clusterOf(b) ? bary[a] - bary[b] : clusterOf(a).localeCompare(clusterOf(b)))
        || (order[a] - order[b]))
      layer.forEach((e, i) => { order[e] = i })
    })
  }
  for (let i = 0; i < 4; i++) { sweep(true); sweep(false) }

  // Tighter spacing than before (node is 188×78, density pass) keeps the forest compact without overlap.
  // GAP: insert horizontal slack between adjacent entities that belong to DIFFERENT groups, so a
  // grouped layout reads as separated blocks instead of one undifferentiated row (the #7 readability
  // win). Ungrouped neighbours get no extra gap, so a plain DAG layout is unchanged.
  const realKey = (e) => {
    if (e.startsWith('super:')) return e.slice(6)
    const k = nodeGroup.get(Number(e.slice(2)))
    return k != null ? String(k) : null
  }
  const XS = 206, YS = 122, GAP = 0.85, pos = {}
  depths.forEach(d => {
    const arr = layers[d]
    let slot = 0
    const slots = arr.map((e, i) => {
      if (i > 0) {
        const a = realKey(arr[i - 1]), b = realKey(e)
        if (a !== b && (a != null || b != null)) slot += GAP   // group boundary → visible gap
      }
      const s = slot; slot += 1; return s
    })
    const span = slots.length ? slots[slots.length - 1] : 0
    const offset = -span / 2
    arr.forEach((e, i) => { pos[e] = { x: (offset + slots[i]) * XS, y: d * YS } })
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
  // Operator-authored experiment: hand-add a node to the search tree. `idea` = {operator, params,
  // rationale, theme?}; optional parent_id (branch from a node) and code (ship ready-made code).
  inject: (rid, { idea, parent_id = null, code = null }) =>
    post(`/api/runs/${rid}/control`, { type: 'inject_node', data: { idea, parent_id, code } }),
  reopen: (rid) => post(`/api/runs/${rid}/control`, { type: 'run_reopened', data: {} }),
  // A7: pin/override the Strategist's choice live (HITL parity). `strategy` = a Strategy dict
  // {policy?, policy_params?, developer?, operators?, fidelity?, rationale?}.
  setStrategy: (rid, strategy) => post(`/api/runs/${rid}/control`, { type: 'set_strategy', data: { strategy } }),
  // P2: ask the engine to run the Deep-Research stage now (read all results + the web, write a memo).
  deepResearch: (rid) => post(`/api/runs/${rid}/control`, { type: 'deep_research', data: {} }),
  // Workstream A: force a high-quality regeneration of the agent-authored run report now. Dedicated
  // endpoint (not /control) — generates inline server-side and appends a `report_generated` event.
  refreshReport: (rid) => post(`/api/runs/${rid}/report_refresh`, {}),
  // Workstream C: a generic control append by {type, data} — the single execution path every chat
  // action funnels through (slash commands and the LLM action-router both produce {type, data}).
  raw: (rid, type, data = {}) => post(`/api/runs/${rid}/control`, { type, data }),
}

// Workstream C: chat actions on a FINISHED run must reopen + re-enter the loop so the engine actually
// processes them (mirrors InjectModal's reopen→inject→resume). These verbs need the loop running.
const NEEDS_RESUME = new Set(['fork', 'inject_node', 'force_confirm', 'force_ablate', 'deep_research', 'set_strategy'])

// Execute one confirmed chat action. `action` = {type, data}. Returns the control promise. Reopens +
// resumes a finished run for engine-served verbs. `__refresh_report__` is the report-refresh special.
export async function applyAction(runId, action, finished) {
  if (action.type === '__refresh_report__') return CONTROL.refreshReport(runId)
  if (finished && NEEDS_RESUME.has(action.type)) {
    await CONTROL.reopen(runId)
    await CONTROL.raw(runId, action.type, action.data || {})
    return resumeRun(runId)
  }
  return CONTROL.raw(runId, action.type, action.data || {})
}

// Re-enter the engine loop on an existing run dir (used to continue a finished run after an inject).
export const resumeRun = (rid) => post(`/api/runs/${encodeURIComponent(rid)}/resume`, {})

// round-7 "Replay": reset a run IN PLACE — the server archives its event log + spans and re-spawns a
// fresh run on the same run-id. Only offered on a FINISHED run (no live engine), so it's race-free.
export const resetRun = (rid) => post(`/api/runs/${encodeURIComponent(rid)}/reset`, {})

// ---- experiment chat / suggest / LLM health ----
export const chat = (rid, messages, node_id = null) => post(`/api/runs/${rid}/chat`, { messages, node_id })
export const suggestIdea = (rid, { node_id = null, messages = [], instruction = '' }) =>
  post(`/api/runs/${rid}/suggest`, { node_id, messages, instruction })
// Workstream C: the action-router — turn a free-text instruction into EITHER an advisory reply or a
// concrete control action {type, data, label, rationale} the chat proposes for confirmation.
export const command = (rid, { messages = [], node_id = null, instruction = '' }) =>
  post(`/api/runs/${rid}/command`, { messages, node_id, instruction })
export const llmHealth = () => get('/api/llm/health')

// G1 server auth: when the server runs with LOOPLAB_UI_TOKEN it injects the token into the served
// page as <meta name="ll-token">; same-origin only, so a cross-origin page can't read it. Send it on
// every mutating request. No token (default local) -> header omitted, behaviour unchanged.
const _authHeaders = (base) => {
  const t = (typeof document !== 'undefined' && document.querySelector('meta[name="ll-token"]')?.content) || ''
  return t ? { ...base, 'X-LoopLab-Token': t } : { ...base }
}
export async function get(path) {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
export async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
export async function putText(path, text) {
  const r = await fetch(path, { method: 'PUT', headers: _authHeaders({ 'Content-Type': 'text/plain' }), body: text })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}
async function send(path, method, body) {
  const r = await fetch(path, { method, headers: _authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body || {}) })
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json()
}

// ---- ClearML-style project API ----
export const listProjects = () => get('/api/projects')
export const createProject = (name, parent_id = null) => post('/api/projects', { name, parent_id })
export const patchProject = (id, body) => send(`/api/projects/${id}`, 'PATCH', body)
export const deleteProject = (id) => send(`/api/projects/${id}`, 'DELETE')
export const assignRun = (runId, project_id) => post(`/api/runs/${encodeURIComponent(runId)}/project`, { project_id })
export const renameRun = (runId, label) => send(`/api/runs/${encodeURIComponent(runId)}`, 'PATCH', { label })
export const deleteRun = (runId) => send(`/api/runs/${encodeURIComponent(runId)}`, 'DELETE')

// super-tasks: a user-managed, flat grouping of runs by the global task they attack (parallel axis
// to projects). create / rename / delete the bucket, then assign any run (existing or new) to it.
export const listSupertasks = () => get('/api/supertasks')
export const createSupertask = (name, task_id = null) => post('/api/supertasks', { name, task_id })
export const renameSupertask = (id, name) => send(`/api/supertasks/${id}`, 'PATCH', { name })
export const deleteSupertask = (id) => send(`/api/supertasks/${id}`, 'DELETE')
export const assignSupertask = (runId, supertask_id) => post(`/api/runs/${encodeURIComponent(runId)}/supertask`, { supertask_id })

export const gpuStat = () => get('/api/gpu')

// ---- settings + run launch ----
export const getSettings = () => get('/api/settings')
export const saveSettings = (settings) => send('/api/settings', 'PUT', { settings })
export const listTasks = () => get('/api/tasks')
export const startRun = (body) => post('/api/start', body)
// chat-first run creation: the pre-run BOSS turns a goal into an editable spec {run_id, task|task_file,
// settings, rationale} + a conversational reply. `draft` carries the current card on refine turns.
export const genesis = ({ messages = [], instruction = '', draft = null }) =>
  post('/api/genesis', { messages, instruction, draft })
export const research = (topic, save = false) => post('/api/research', { topic, save })

// cross-run aggregate reports over a scope (project | task | supertask). GET returns the stored report
// + staleness ({exists, content, generated_at, run_ids, stale, added, current_run_count}); generate
// (re)synthesizes on demand via an agent with access to every run in the scope.
const _scopeUrl = (type, id) => `/api/scope-report/${encodeURIComponent(type)}/${encodeURIComponent(id)}`
export const getScopeReport = (type, id) => get(_scopeUrl(type, id))
export const genScopeReport = (type, id) => post(`${_scopeUrl(type, id)}/generate`, {})
