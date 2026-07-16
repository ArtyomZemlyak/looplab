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
    const c = canonicalId(raw, rename)
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
