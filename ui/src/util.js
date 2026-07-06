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

// Dynamic font size for a node card's one-line caption ("what this node did"). The chip is a fixed
// width (~168px) and single line, so a long param-diff / change-summary used to hit the hard ellipsis
// almost immediately. Instead of clipping, shrink the font as the text grows so MORE of the caption
// stays legible in the same footprint — a short "baseline" stays a comfortable 11px, a long
// "lr: 0.01 → 0.003, depth: 4 → 8, subsample: …" scales down toward an 8px floor before ellipsizing.
// Pure + deterministic (length-based, ~0.56em/char) so it never reflows or measures the DOM.
export function chipFontSize(text, { max = 11, min = 8, width = 168 } = {}) {
  const len = String(text || '').length
  if (!len) return max
  const fit = width / (len * 0.56)   // approx glyph advance ≈ 0.56em at this weight
  return Math.max(min, Math.min(max, Math.round(fit * 2) / 2))   // clamp to [min,max] in 0.5px steps
}

// Human-readable byte size (file listings, etc.).
export function fmtBytes(n) {
  if (n == null) return ''
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
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
//
// Returns { pos, cells }: `pos` keyed by entity id ({x,y}, the contract every caller relies on),
// `cells` the grouped regions to draw — one {key, ids[]} per non-collapsed group (layered modes),
// or one {key, band, ids[]} per (band, group) when the BANDED grid-pack is active (theme/niche).
export function layoutWithGroups(nodes, { collapsed = new Set(), nodeGroup = new Map(), groupMode = 'none' } = {}) {
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

  const pos = {}, cells = []
  const banded = (groupMode === 'theme' || groupMode === 'niche')

  // The barycenter crossing-minimisation below is consumed ONLY by the layered tail; the banded
  // grid-pack orders its cells/members itself (by id and stable rank). layoutWithGroups re-runs on
  // every SSE frame, so gate this O(nodes·depth·sweeps) work out of the banded path — it would be
  // pure waste there. `order`/`layers` are left untouched in banded mode (the banded tail ignores them).
  const order = {}
  if (!banded) {
    // Cluster key per entity (keeps a group's members adjacent). Ungrouped nodes get a unique key so
    // they order freely by barycenter; a collapsed group's super-node carries its own key.
    const clusterOf = (e) => {
      if (e.startsWith('super:')) return e.slice(6)
      const k = nodeGroup.get(Number(e.slice(2)))
      return k != null ? String(k) : ' ' + e
    }
    const median = (a) => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2 }

    // Seed order: cluster-key then entity id, so members of a group start as one contiguous block.
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
  }

  if (!banded) {
    // ---- layered tail (operator / metric / none): UNCHANGED from before -------------------------
    // Tighter spacing than before (node is 188×78, density pass) keeps the forest compact without
    // overlap. GAP: insert horizontal slack between adjacent entities of DIFFERENT groups, so a
    // grouped layout reads as separated blocks instead of one undifferentiated row. Ungrouped
    // neighbours get no extra gap, so a plain DAG layout is unchanged.
    const realKey = (e) => {
      if (e.startsWith('super:')) return e.slice(6)
      const k = nodeGroup.get(Number(e.slice(2)))
      return k != null ? String(k) : null
    }
    const XS = 206, YS = 122, GAP = 0.85
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
    // one region cell per non-collapsed group (Dag draws it only when ≥2 members)
    const byKey = new Map()
    ents.forEach(e => {
      if (e.startsWith('super:')) return
      const k = nodeGroup.get(Number(e.slice(2)))
      if (k == null || collapsed.has(k)) return
      if (!byKey.has(k)) byKey.set(k, [])
      byKey.get(k).push(Number(e.slice(2)))
    })
    byKey.forEach((ids, key) => cells.push({ key, ids }))
    return { pos, cells }
  }

  // ---- BANDED grid-pack (theme / niche) -----------------------------------------------------------
  // Depth is a COARSE axis (~10 levels), so we relax it from a hard row into a soft BAND (K depths
  // each). Inside a band, each group's members fill a FIXED-ROW, column-major grid cell — compact,
  // non-overlapping, and append-stable (a new member takes the next column slot; placed members never
  // move). Themeless entities (merges carry theme=None) collect in a per-band BRIDGE cell on the right
  // instead of being boxed. Cells order left→right by group similarity (Phase 1b) so like sits by like.
  const K = 2                 // depths per band
  const ROWS = 4              // fixed rows per cell → columns grow rightward (short, wide, stable)
  const PX = 206, PY = 92, GAPCOLS = 0.7, BAND_GAP = 78
  const BRIDGE = Symbol('bridge')   // unique cell key for themeless entities; never collides with a group key
  const bandOf = (e) => Math.floor((depth[e] || 0) / K)
  const keyOfEnt = (e) => e.startsWith('super:') ? e.slice(6) : (nodeGroup.get(Number(e.slice(2))) ?? null)
  const rank = similarityRank(nodes, nodeGroup, groupMode)

  // bucket: band → Map(cellKey → [entity]); a null group key routes to the bridge cell
  const cellMap = new Map()
  ents.forEach(e => {
    const b = bandOf(e)
    const k = keyOfEnt(e)
    const ck = (k == null) ? BRIDGE : k
    if (!cellMap.has(b)) cellMap.set(b, new Map())
    const m = cellMap.get(b)
    if (!m.has(ck)) m.set(ck, [])
    m.get(ck).push(e)
  })
  // fill order inside a cell: purely NODE ID (monotonic with creation time). Keying on id alone — not
  // depth or the barycenter sweep — is what makes the grid append-stable: a new node always has the
  // largest id, so it lands in the next free slot and never reshuffles already-placed members. (A cell
  // spans only K=2 depths, so intra-cell depth ordering carries no real signal worth the instability.)
  const idOf = (e) => e.startsWith('super:') ? Number.MAX_SAFE_INTEGER : Number(e.slice(2))
  const entSort = (a, b) => idOf(a) - idOf(b)

  const presentBands = [...cellMap.keys()].sort((a, b) => a - b)
  presentBands.forEach(b => {
    const m = cellMap.get(b)
    // cell x-order: group cells by the STABLE similarity rank (Phase 1b), bridge always last. Because
    // rank is immutable (discovery order / param tuple, not live metric), this order does not change
    // frame-to-frame, so cells never swap and placed nodes never jump horizontally.
    const cellKeys = [...m.keys()].sort((a, c) => {
      const ra = a === BRIDGE ? Infinity : (rank.get(a) ?? Infinity)
      const rc = c === BRIDGE ? Infinity : (rank.get(c) ?? Infinity)
      return (ra - rc) || String(a).localeCompare(String(c))
    })
    // reserve each cell ceil(n/ROWS) columns + a gap; LEFT-ALIGNED, and yTop keyed on the ABSOLUTE band
    // number b (not the array index) so a node landing in a new band can't shift other bands. With a
    // fixed cell order + column-major fill, appending a member only adds columns to the right.
    let colCursor = 0
    const yTop = b * (ROWS * PY + BAND_GAP)
    cellKeys.forEach(ck => {
      const arr = m.get(ck).slice().sort(entSort)
      const start = colCursor
      arr.forEach((e, i) => {
        pos[e] = { x: (start + Math.floor(i / ROWS)) * PX, y: yTop + (i % ROWS) * PY }
      })
      colCursor = start + Math.max(1, Math.ceil(arr.length / ROWS)) + GAPCOLS
      if (ck !== BRIDGE && !collapsed.has(ck)) {
        const ids = arr.filter(e => !e.startsWith('super:')).map(e => Number(e.slice(2)))
        if (ids.length) cells.push({ key: ck, band: b, ids })
      }
    })
  })
  return { pos, cells }
}

// Phase 1b: a deterministic, STABLE global x-rank per group so related groups land adjacent within a
// band (the "distance ∝ similarity" ask, kept cheap and DAG-preserving). The rank must NOT depend on
// live metrics: the layout re-runs every SSE frame, so a metric-driven order would reshuffle whole
// bands as metrics fill in and visibly jump already-placed nodes. So we order by IMMUTABLE keys:
//   niche → (param-name, param-value) tuple — same-axis niches sort by value (near params are
//           neighbours), different-axis niches separate by name. Values stay numeric when they parse.
//   theme/other → DISCOVERY order (smallest member id) — fixed once a group exists, and roughly groups
//           themes explored around the same time. Pure: same node set → same ranks (no metric churn).
export function similarityRank(nodes, nodeGroup, mode) {
  const members = new Map()
  nodeGroup.forEach((key, id) => { if (!members.has(key)) members.set(key, []); members.get(key).push(id) })
  // numeric compare when both are finite numbers, else a stable string compare — never NaN (a NaN from
  // a non-numeric param would otherwise poison the sort comparator and make the order non-deterministic).
  const cmpVal = (x, y) => {
    const nx = typeof x === 'number' && Number.isFinite(x)
    const ny = typeof y === 'number' && Number.isFinite(y)
    return (nx && ny) ? x - y : String(x).localeCompare(String(y))
  }
  const sortKey = (key) => {
    const ids = members.get(key) || []
    if (mode === 'niche') {
      const rep = ids.map(i => nodes[i]).find(n => n && n.idea && n.idea.params)
      const p = (rep && rep.idea.params) || {}
      return Object.keys(p).sort().flatMap(k => {        // [name, value, name, value, …]
        const v = p[k]; const nv = typeof v === 'number' ? v : Number(v)
        return [k, Number.isNaN(nv) ? String(v) : nv]    // keep the raw string rather than a bare NaN
      })
    }
    return [Math.min(...ids)]                            // discovery order (immutable)
  }
  const keyOf = new Map([...members.keys()].map(k => [k, sortKey(k)]))   // precompute once (no per-compare rescans)
  const keys = [...members.keys()].sort((a, b) => {
    const ka = keyOf.get(a), kb = keyOf.get(b)
    const n = Math.max(ka.length, kb.length)
    for (let i = 0; i < n; i++) {
      if (i >= ka.length) return -1
      if (i >= kb.length) return 1
      const c = cmpVal(ka[i], kb[i])
      if (c) return c
    }
    return String(a).localeCompare(String(b))
  })
  const out = new Map(); keys.forEach((k, i) => out.set(k, i)); return out
}

export function phaseLabel(state) {
  return state?.phase || (state?.finished ? 'finished' : '—')
}

export const CONTROL = {
  // Three operator controls (see docs/guide/concepts.md → "Stopping a run"):
  //   stop     — freeze the run, NO finalization (event: pause). Resumable; finalize later if wanted.
  //   finalize — stop AND wrap up (report / cross-run lessons+case / cost roll-up). event: run_abort.
  //   resume   — continue from ANY stopped state (pause / finalize / natural finish). event: resume.
  stop: (rid) => post(`/api/runs/${rid}/control`, { type: 'pause', data: {} }),
  finalize: (rid) => post(`/api/runs/${rid}/control`, { type: 'run_abort', data: { reason: 'finalized' } }),
  resume: (rid) => post(`/api/runs/${rid}/control`, { type: 'resume', data: {} }),
  // back-compat aliases (older callers / NL control): pause≡stop, abort≡finalize, reopen≡resume.
  pause: (rid) => post(`/api/runs/${rid}/control`, { type: 'pause', data: {} }),
  abort: (rid) => post(`/api/runs/${rid}/control`, { type: 'run_abort', data: { reason: 'finalized' } }),
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
  // U3: merge two nodes — inject a multi-parent `merge` node; the engine recombines the parents'
  // solutions via its real merge/ensemble operator (not a blank manual node).
  merge: (rid, ids) => post(`/api/runs/${rid}/control`, { type: 'inject_node', data: {
    idea: { operator: 'merge', rationale: `merge ${ids.map(i => '#' + i).join(' + ')}` }, parent_ids: ids } }),
  // A7: pin/override the Strategist's choice live (HITL parity). `strategy` = a Strategy dict
  // {policy?, policy_params?, developer?, operators?, fidelity?, rationale?}.
  setStrategy: (rid, strategy) => post(`/api/runs/${rid}/control`, { type: 'set_strategy', data: { strategy } }),
  // P2: ask the engine to run the Deep-Research stage now (read all results + the web, write a memo).
  deepResearch: (rid) => post(`/api/runs/${rid}/control`, { type: 'deep_research', data: {} }),
  // P1: register an open hypothesis on the board (a question the search should resolve), or drop one.
  addHypothesis: (rid, statement) => post(`/api/runs/${rid}/control`, { type: 'hypothesis_added', data: { statement, source: 'human' } }),
  abandonHypothesis: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'hypothesis_updated', data: { id, status: 'abandoned' } }),
  // Workstream A: force a high-quality regeneration of the agent-authored run report now. Dedicated
  // endpoint (not /control) — appends a `report_generated` event. Runs as a background job, so we
  // jobAwait the response (a slow/large regen can't 504 behind a proxy; a fast one returns inline).
  // Contract preserved: resolves to {ok, seq, content} (or {ok:false} offline), never a job_id.
  refreshReport: async (rid) => jobAwait(await post(`/api/runs/${rid}/report_refresh`, {})),
  // Workstream C: a generic control append by {type, data} — the single execution path every chat
  // action funnels through (slash commands and the LLM action-router both produce {type, data}).
  raw: (rid, type, data = {}) => post(`/api/runs/${rid}/control`, { type, data }),
}

// Workstream C: chat actions on a FINISHED run must reopen + re-enter the loop so the engine actually
// processes them (mirrors InjectModal's reopen→inject→resume). These verbs need the loop running.
// `budget_extend` is here too: raising the node budget on a finished run is pointless unless the run
// reopens and keeps going (the agentic boss pairs it with inject/hint steps).
// Actions whose effect only takes hold once the engine is (re)spawned on a stopped/finished run.
// run_abort = FINALIZE: the wrap-up (report/lessons/cost) needs the engine to fold stop_requested
// into run_finished; resume needs it to keep going. (Twin of tui.py _NEEDS_RESUME.)
const NEEDS_RESUME = new Set(['fork', 'inject_node', 'force_confirm', 'force_ablate', 'deep_research', 'set_strategy', 'budget_extend', 'resume', 'run_abort'])

// Does applying this action on a FINISHED run require reopening + resuming the engine? (Used to batch a
// multi-action plan: append every step's intent, then reopen+resume ONCE if any step needs the loop.)
export const actionNeedsEngine = (action) => NEEDS_RESUME.has(action?.type)

// Append ONE action's control intent WITHOUT reopening/resuming — the building block for applying an
// agentic plan as a batch (append every step, then resume once at the end). `__refresh_report__` is
// the report-refresh special case (its own endpoint, never the engine loop).
export async function appendAction(runId, action) {
  if (action.type === '__refresh_report__') return CONTROL.refreshReport(runId)
  return CONTROL.raw(runId, action.type, action.data || {})
}

// Re-enter the engine loop on an existing run dir (used to continue a finished run after an inject).
export const resumeRun = (rid) => post(`/api/runs/${encodeURIComponent(rid)}/resume`, {})

// round-7 "Replay": reset a run IN PLACE — the server archives its event log + spans and re-spawns a
// fresh run on the same run-id. Only offered on a FINISHED run (no live engine), so it's race-free.
export const resetRun = (rid) => post(`/api/runs/${encodeURIComponent(rid)}/reset`, {})

export const llmHealth = () => get('/api/llm/health')

// G1 server auth: when the server runs with LOOPLAB_UI_TOKEN it injects the token into the served
// page as <meta name="ll-token">. A *cross-origin* page can't read it (that's per-origin SOP), but a
// SAME-origin page on a different path CAN — so the token only isolates users when each has its own
// origin (default 127.0.0.1 bind, or a per-user subdomain), NOT on a shared jupyter-server-proxy
// origin where it's a per-deployment secret (server injects it only on top-level navigations + sets
// X-Frame-Options/no-store; see looplab/server.py and docs/guide/deployment.md). Send it on every
// mutating request. No token (default local) -> header omitted, behaviour unchanged.
const _authHeaders = (base) => {
  const t = (typeof document !== 'undefined' && document.querySelector('meta[name="ll-token"]')?.content) || ''
  return t ? { ...base, 'X-LoopLab-Token': t } : { ...base }
}
// Surface the server's error DETAIL (FastAPI puts the human-readable reason in `detail`) instead of a
// bare status code — so e.g. a 422 from a per-run config save reads "invalid settings — n_seeds: …"
// in the toast rather than just "422". Falls back to status when there's no JSON body.
async function _throw(r, path) {
  let detail = ''
  try { const j = await r.json(); detail = (j && (j.detail || j.error)) || '' } catch { /* no body */ }
  const err = new Error(detail ? String(detail) : `${path}: ${r.status}`)
  err.status = r.status   // callers branch on the code (e.g. 409 = run live / name taken), not a regex on the message
  throw err
}

// Path-mounting-proxy support. The UI may be served under a prefix (JupyterHub
// `/user/<name>/proxy/8765/`, a reverse-proxy subpath, …) rather than at the domain root, so an
// absolute `/api/…` would hit the proxy host's root and miss the backend. We route every request
// through apiUrl(), which prepends the prefix the page itself was served from. Routing is hash-based
// (`#/run/…`), so location.pathname is exactly that prefix; the proxy strips it before forwarding,
// so the backend still sees `/api/…`. At the root (local `looplab ui`) the prefix is '' — unchanged.
export function apiPrefix() {
  if (typeof location === 'undefined') return ''
  return location.pathname.replace(/\/index\.html$/, '').replace(/\/+$/, '')
}
export const apiUrl = (path) => apiPrefix() + path

export async function get(path) {
  // Carry the UI token on reads too: most GETs don't need it, but the artifact routes (raw file
  // content) are token-gated server-side. _authHeaders is a no-op when no token is set (local), so
  // ordinary local use is unchanged.
  const r = await fetch(apiUrl(path), { headers: _authHeaders({}) })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
export async function post(path, body) {
  const r = await fetch(apiUrl(path), { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
export async function putText(path, text) {
  const r = await fetch(apiUrl(path), { method: 'PUT', headers: _authHeaders({ 'Content-Type': 'text/plain' }), body: text })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
async function send(path, method, body) {
  // Only attach a JSON body for methods that carry one (PATCH/PUT/POST). A DELETE with a request
  // body + Content-Type is unusual and some reverse proxies (e.g. jupyter-server-proxy) mishandle it
  // — which surfaced as a 500 on "delete chat"/"delete run". DELETE goes bodyless.
  const hasBody = method !== 'DELETE' && method !== 'GET'
  const opts = { method, headers: _authHeaders(hasBody ? { 'Content-Type': 'application/json' } : {}) }
  if (hasBody) opts.body = JSON.stringify(body || {})
  const r = await fetch(apiUrl(path), opts)
  if (!r.ok) await _throw(r, path)
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
// Store (or clear, value='') a secret credential. Goes to the dedicated owner-only secret store,
// NOT ui_settings.json — the value is never echoed back (the GET reports it only as masked "***").
export const saveSecret = (key, value) => send('/api/settings/secret', 'PUT', { key, value })
// Per-run settings: edit a specific run's config.snapshot.json so the next RESUME picks up the
// change (only changed fields are sent). Blocked server-side while the run's engine is live.
export const saveRunConfig = (rid, settings) => send(`/api/runs/${encodeURIComponent(rid)}/config`, 'PUT', { settings })
export const listTasks = () => get('/api/tasks')
export const startRun = (body) => post('/api/start', body)
// chat-first run creation: the pre-run BOSS turns a goal into an editable spec {run_id, task|task_file,
// settings, rationale} + a conversational reply. `draft` carries the current card on refine turns.
export const genesis = ({ messages = [], instruction = '', draft = null }) =>
  post('/api/genesis', { messages, instruction, draft })
// Genesis runs an agentic, multi-turn loop server-side: a fast model returns the plan inline from the
// POST above; a slow one returns {status:'running', job_id} which we poll here until done (so a long
// agentic plan can't 504 behind a proxy). Generous overall cap — the agent decides how many turns it
// needs. Transient poll errors are tolerated (keep polling).
export const genesisJob = (jobId) => get(`/api/genesis/${encodeURIComponent(jobId)}`)
export async function genesisAwait(resp, { intervalMs = 1500, timeoutMs = 300000, onProgress } = {}) {
  if (!resp || resp.status !== 'running' || !resp.job_id) return resp   // fast-path: already the plan
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, intervalMs))
    let j
    try { j = await genesisJob(resp.job_id) } catch { continue }        // transient — keep polling
    if (j.status === 'running' && j.progress && onProgress) onProgress(j.progress)  // live scout step
    if (j.status === 'done') return j
    if (j.status === 'unknown') return { ok: false, error: 'planning expired — send the goal again' }
  }
  return { ok: false, error: 'planning timed out' }
}
export const research = (topic, save = false) => post('/api/research', { topic, save })

// cross-run aggregate reports over a scope (project | task | supertask). GET returns the stored report
// + staleness ({exists, content, generated_at, run_ids, stale, added, current_run_count}); generate
// (re)synthesizes on demand via an agent with access to every run in the scope.
const _scopeUrl = (type, id) => `/api/scope-report/${encodeURIComponent(type)}/${encodeURIComponent(id)}`
export const getScopeReport = (type, id) => get(_scopeUrl(type, id))
// Generic background-job poll (mirrors genesisAwait): the server hands back {status:'running', job_id}
// for slow work so it can't 504 behind a proxy. Returns the final result dict; tolerates transient
// poll errors. `resp` that's already a result (fast inline path) is returned unchanged.
const _job = (jobId) => get(`/api/jobs/${encodeURIComponent(jobId)}`)
export async function jobAwait(resp, { intervalMs = 1500, timeoutMs = 600000 } = {}) {
  if (!resp || resp.status !== 'running' || !resp.job_id) return resp
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, intervalMs))
    let j
    try { j = await _job(resp.job_id) } catch { continue }   // transient — keep polling
    if (j.status === 'done') return j
    if (j.status === 'unknown') return { ok: false, error: 'the job expired — try again' }
  }
  return { ok: false, error: 'timed out' }
}
// Cross-run synthesis can read many runs + drive an agent, so it runs as a background job; await it to
// completion and surface a hard failure as a throw (the panel's catch shows it), preserving the old
// "returns the final record" contract for callers.
export async function genScopeReport(type, id) {
  const r = await jobAwait(await post(`${_scopeUrl(type, id)}/generate`, {}))
  if (r && r.ok === false && r.error) throw new Error(r.error)
  return r
}

// ---- assistant (general chat agent — the evolution of Genesis) ----
export const assistantSessions = () => get('/api/assistant/sessions')
export const assistantCreate = (title = '', mode = 'plan') => post('/api/assistant/sessions', { title, mode })
export const assistantGet = (sid) => get(`/api/assistant/sessions/${encodeURIComponent(sid)}`)
export const assistantDelete = (sid) => send(`/api/assistant/sessions/${encodeURIComponent(sid)}`, 'DELETE')
export const assistantFork = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/fork`, {})
// Streaming turn: POST and read the SSE stream, invoking callbacks for token/step/todos/done/error.
// Real token streaming of the final answer (Claude-Desktop feel). Returns the final result dict.
export async function assistantMessageStream(sid, instruction, mode, cbs = {}, signal, display = null) {
  const r = await fetch(apiUrl(`/api/assistant/sessions/${encodeURIComponent(sid)}/message_stream`),
    { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(display && display !== instruction ? { instruction, mode, display } : { instruction, mode }), signal })
  if (!r.ok || !r.body) { await _throw(r, 'message_stream'); return null }
  const reader = r.body.getReader(); const dec = new TextDecoder()
  let buf = ''; let result = null
  for (;;) {
    let chunk
    try { chunk = await reader.read() } catch { break }   // aborted (unmount) — stop cleanly
    const { done, value } = chunk
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2)
      let ev = 'message'; let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      let parsed; try { parsed = JSON.parse(data) } catch { parsed = data }
      if (ev === 'token') cbs.onToken && cbs.onToken(parsed)
      else if (ev === 'text') cbs.onText && cbs.onText(parsed)
      else if (ev === 'step') cbs.onStep && cbs.onStep(parsed)
      else if (ev === 'todos') cbs.onTodos && cbs.onTodos(parsed)
      else if (ev === 'error') { cbs.onError && cbs.onError(parsed); result = { ok: false, error: parsed } }
      else if (ev === 'done') { result = parsed; cbs.onDone && cbs.onDone(parsed) }
    }
  }
  return result
}
// Full (uncapped) I/O for one trace observation — fetched lazily when the user expands a
// generation/tool in the trace tree (the tree itself is served light, without prompts/outputs).
export const spanDetail = (runId, spanId) =>
  get(`/api/runs/${encodeURIComponent(runId)}/spans/${encodeURIComponent(spanId)}`)

// Linear, de-duplicated conversation view of a node's trace (request once per sub-loop, then each
// generation's delta interleaved with tool calls) — the readable alternative to the raw span tree.
export const nodeConversation = (runId, nid) =>
  get(`/api/runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nid)}/conversation`)

// Stop an in-flight assistant turn server-side (survives a page reload, unlike aborting the local
// stream). Also used to poll whether a turn is still running (reattach after switch/reload).
export const assistantCancel = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/cancel`, {})
export const assistantProgress = (sid) => get(`/api/assistant/progress?session=${encodeURIComponent(sid)}`)

export const assistantCommands = () => get('/api/assistant/commands')
export const assistantRevert = (path) => post('/api/assistant/revert', { path })
export const assistantShare = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/share`, {})
// Pending human-in-the-loop confirm requests for a session, and resolving one.
export const assistantPermissions = (sid) => get(`/api/assistant/permissions?session=${encodeURIComponent(sid)}`)
export const assistantResolve = (reqId, decision) =>
  post(`/api/assistant/permissions/${encodeURIComponent(reqId)}`, { decision })
