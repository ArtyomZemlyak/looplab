// Pure model for View 2 — the concept chip bar over the lineage graph. No React; unit-tested with
// `node --test`. Chips are breadcrumb-navigable (drill into a concept to reveal its children) and
// multi-selectable (OR). Selecting a concept highlights every node that touches it OR any descendant
// of it. All ids are canonicalized through the consolidation rename map.

// The chips to show at a breadcrumb `path` ('' = top-level roots): the distinct next-level concepts
// under `path`, each with the count of nodes touching that subtree. Sorted by count desc, then id.
export function chipsAtPath(nodeConcepts = {}, rename = {}, path = '') {
  // REVIEW(2026-07-16): this replicates conceptViewModel.js's unsafe plain-object-map pattern in two
  // MORE places (here and conceptMatches): `rename[raw] || raw` reads through the prototype chain and
  // `counts[childId] ||= new Set()` on childId="__proto__" reads Object.prototype (truthy), skips the
  // assignment and TypeErrors on `.add()` — one LLM-authored tag can crash the chip bar. The hazard is
  // now copy-pasted across three functions in two files; extract ONE shared safe canonicalizer
  // (`canonicalId(raw, rename)` with a hasOwnProperty guard) + use Map for accumulators, instead of
  // fixing each copy separately. Also a semantic gap: a node tagged EXACTLY at `path` (not deeper) is
  // skipped by the `parts.length <= depth` guard, so after drilling into `loss` a node tagged bare
  // `loss` contributes to NO chip — child counts silently sum below the parent count the user just
  // clicked, with nothing showing where the remainder went (needs an "· at this level" chip or count).
  const prefix = path ? path + '/' : ''
  const depth = path ? path.split('/').length : 0
  const counts = {}                                   // childId -> Set(nodeId)
  for (const [nid, ids] of Object.entries(nodeConcepts || {})) {
    const id = Number(nid)
    for (const raw of (ids || [])) {
      const c = rename[raw] || raw
      if (!c) continue
      if (path && !(c === path || c.startsWith(prefix))) continue
      const parts = c.split('/')
      if (parts.length <= depth) continue             // c is AT/above this level -> no child chip here
      const childId = parts.slice(0, depth + 1).join('/')
      ;(counts[childId] ||= new Set()).add(id)
    }
  }
  return Object.entries(counts)
    .map(([id, set]) => ({ id, label: id.split('/').pop(), count: set.size }))
    .sort((a, b) => b.count - a.count || a.id.localeCompare(b.id))
}

// Breadcrumb segments for a path: [{id, label}] from root to the full path (inclusive).
export function breadcrumb(path = '') {
  if (!path) return []
  const parts = path.split('/')
  return parts.map((_, i) => {
    const id = parts.slice(0, i + 1).join('/')
    return { id, label: parts[i] }
  })
}

// Does a node (its raw concept id list) match ANY selected concept (OR)? A selected concept matches a
// node tagged with it OR with any DESCENDANT of it (select `loss` -> highlight `loss/contrastive/dcl`).
// Empty selection matches everything (no dimming).
export function conceptMatches(nodeConceptIds, selected, rename = {}) {
  if (!selected || !selected.length) return true
  for (const raw of (nodeConceptIds || [])) {
    const c = rename[raw] || raw
    if (!c) continue
    for (const s of selected) if (c === s || c.startsWith(s + '/')) return true
  }
  return false
}

// The set of node ids matching the selection (OR) — for highlighting the graph. Empty selection -> null
// (meaning "no filter / highlight nothing specific").
export function matchingNodeIds(nodeConcepts = {}, selected, rename = {}) {
  if (!selected || !selected.length) return null
  const out = new Set()
  for (const [nid, ids] of Object.entries(nodeConcepts || {})) {
    if (conceptMatches(ids, selected, rename)) out.add(Number(nid))
  }
  return out
}
