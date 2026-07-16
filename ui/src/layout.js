// The dependency-free layered DAG layout (semantic-zoom groups + the banded grid-pack) and its
// stable group-similarity rank. Split out of util.js (mega-refactor P5.2 — bodies verbatim);
// util.js re-exports everything, so importers are unchanged.

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
    // Tighter spacing than before (node is 188×84, density pass) keeps the forest compact without
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
  const PX = 206, PY = 102, GAPCOLS = 0.7, BAND_GAP = 78
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
