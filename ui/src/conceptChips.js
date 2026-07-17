// Pure model for View 2 — the concept chip bar over the lineage graph. No React; unit-tested with
// `node --test`. Chips are breadcrumb-navigable (drill into a concept to reveal its children) and
// multi-selectable (OR). Selecting a concept highlights every node that touches it OR any descendant
// of it. All ids are canonicalized through the consolidation rename map (prototype-safe helpers in
// conceptId.js — concept ids are LLM-authored, so an id like "__proto__" must never reach the chain).
import { canonicalId } from './conceptId.js'

// The chips to show at a breadcrumb `path` ('' = top-level roots): the distinct next-level concepts
// under `path`, each with the count of nodes touching that subtree. Sorted by count desc, then id.
// When drilled (path != ''), a node tagged EXACTLY at `path` (not deeper) has no next-level child, so
// it would otherwise vanish from the chips — child counts silently summing below the parent count the
// user just clicked. We surface those as a trailing `atLevel: true` chip (id === path) so nothing is
// hidden; selecting it highlights the whole `path` subtree (which includes the bare-`path` nodes), and
// the UI can render it distinctly ("· here").
export function chipsAtPath(nodeConcepts = {}, rename = {}, path = '') {
  // Canonicalize the drilled path too (not just the node tags): a consolidation rename landing after the
  // user drilled would otherwise strand the breadcrumb on a retired id, matching no canonicalized node
  // tag, and the level flips to "No concepts at this level".
  path = path ? canonicalId(path, rename) : ''
  const prefix = path ? path + '/' : ''
  const depth = path ? path.split('/').length : 0
  const counts = new Map()                            // childId -> Set(nodeId)
  let here = null                                     // Set(nodeId) tagged EXACTLY at `path`
  for (const [nid, ids] of Object.entries(nodeConcepts || {})) {
    const id = Number(nid)
    for (const raw of (ids || [])) {
      const c = canonicalId(raw, rename)
      if (!c) continue
      if (path && !(c === path || c.startsWith(prefix))) continue
      const parts = c.split('/')
      if (parts.length <= depth) {                    // c is AT this level (c === path): no child chip
        if (c === path) (here ||= new Set()).add(id)
        continue
      }
      const childId = parts.slice(0, depth + 1).join('/')
      let set = counts.get(childId)
      if (!set) counts.set(childId, (set = new Set()))
      set.add(id)
    }
  }
  const chips = [...counts.entries()]
    .map(([id, set]) => ({ id, label: id.split('/').pop(), count: set.size }))
    .sort((a, b) => b.count - a.count || a.id.localeCompare(b.id))
  if (here && here.size) chips.push({ id: path, label: path.split('/').pop(), count: here.size, atLevel: true })
  return chips
}

// F2: order chips by which concept led to the best outcome — descending Δbest-from-baseline (the advisory
// per-concept subtree rollup from the /concepts frame, the same signal the old Directions bar carried,
// now on concepts). Concepts with no evaluated experiment (null Δ) sort last; ties break by touch count
// then id for stability. With no rollup available it preserves the given (touch-count) order, so the bar
// never blocks on the advisory metric fetch. Pure.
export function orderChipsByDelta(chips, rollup) {
  if (!rollup || typeof rollup !== 'object') return chips
  const key = (id) => {
    const d = rollup[id] && rollup[id].delta_best
    return typeof d === 'number' ? d : -Infinity
  }
  return [...chips].sort((a, b) =>
    key(b.id) - key(a.id) || b.count - a.count || a.id.localeCompare(b.id))
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

// Does a node (its raw concept id list) match ANY selected concept (OR)? A plain selected id matches a
// node tagged with it OR with any DESCENDANT of it (select `loss` -> highlight `loss/contrastive/dcl`).
// A selection prefixed with `=` is an EXACT match — it matches ONLY a node tagged with that id verbatim,
// not its descendants (the "· here" chip after drilling: `=loss` highlights only bare-`loss` nodes). A
// concept id is `axis/slug` (lower-case, hyphenated) and never begins with `=`, so the marker is
// unambiguous. Empty selection matches everything (no dimming).
export function conceptMatches(nodeConceptIds, selected, rename = {}) {
  if (!selected || !selected.length) return true
  // Canonicalize BOTH sides: the node tags AND each selected id (preserving the `=` exact marker). A
  // consolidation rename landing after selection would otherwise strand the filter — every node tag
  // canonicalizes to the NEW id while `selected` still holds the retired one, so nothing matches and the
  // whole graph dims. Passing the selection through the rename retargets it to the surviving concept.
  for (const raw of (nodeConceptIds || [])) {
    const c = canonicalId(raw, rename)
    if (!c) continue
    for (const s of selected) {
      const exact = s[0] === '='
      const id = canonicalId(exact ? s.slice(1) : s, rename)
      if (exact ? c === id : (c === id || c.startsWith(id + '/'))) return true
    }
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
