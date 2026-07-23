// Pure model for View 2 — the concept chip bar over the lineage graph. No React; unit-tested with
// `node --test`. Chips are breadcrumb-navigable (drill into a concept to reveal its children) and
// multi-selectable (OR). Selecting a concept highlights every node that touches it OR any descendant
// of it. All ids are canonicalized through the consolidation rename map (prototype-safe helpers in
// conceptId.js — concept ids are LLM-authored, so an id like "__proto__" must never reach the chain).
import { canonicalId, normalizeConceptId } from './conceptId.js'

// Mirror the server field cap (core.models: EV_CONCEPT_TAG_EDITED concepts list max_length=64). The
// editor drops the overflow rather than sending a payload the durable command would reject.
export const MAX_NODE_CONCEPTS = 64

// The chips to show at a breadcrumb `path` ('' = top-level roots): the distinct next-level concepts
// under `path`, each with the count of nodes touching that subtree. Sorted by canonical id so live
// evidence can update counts without moving keyboard/click targets under the operator.
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
    .sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
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

// Keep an OR selection semantically minimal. A plain concept selects its whole subtree, so retaining
// both `data` and `data/features/physics` makes the child look like a refinement while `data` still
// keeps every sibling highlighted. Prefer the concept the operator selected most recently whenever
// two plain subtree selections are ancestor/descendant-comparable. Exact (`=`) selections only
// overlap a plain subtree when that subtree contains the exact path; disjoint/exact-only selections
// remain valid OR alternatives.
const selectionPart = (key, rename = {}) => {
  const exact = typeof key === 'string' && key[0] === '='
  const id = canonicalId(exact ? key.slice(1) : key, rename)
  return { exact, id }
}

function addSelection(selected, nextKey, rename = {}, toggle = false) {
  const next = selectionPart(nextKey, rename)
  if (!next.id) return selected
  const same = selected.some(key => {
    const value = selectionPart(key, rename)
    return value.id === next.id && value.exact === next.exact
  })
  if (same) {
    if (!toggle) return selected
    return selected.filter(key => {
      const value = selectionPart(key, rename)
      return value.id !== next.id || value.exact !== next.exact
    })
  }

  const kept = selected.filter(key => {
    const value = selectionPart(key, rename)
    if (!value.id) return false
    if (value.exact) {
      // A newly selected plain ancestor contains this exact point and replaces it. An exact ancestor
      // does not contain a newly selected descendant, so that pair remains a meaningful OR.
      return next.exact || !(value.id === next.id || value.id.startsWith(next.id + '/'))
    }
    if (next.exact) {
      // Prefer the new exact point over a plain ancestor that would otherwise make it ineffective.
      return !(next.id === value.id || next.id.startsWith(value.id + '/'))
    }
    // Two plain subtree filters on one branch are always redundant in an OR; keep the newest intent.
    return !(next.id === value.id
      || next.id.startsWith(value.id + '/')
      || value.id.startsWith(next.id + '/'))
  })
  return [...kept, (next.exact ? '=' : '') + next.id]
}

export const addConceptSelection = (selected = [], nextKey, rename = {}) =>
  addSelection(selected, nextKey, rename, false)

export const toggleConceptSelection = (selected = [], nextKey, rename = {}) =>
  addSelection(selected, nextKey, rename, true)

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

// Phase 2c (concept re-tag affordance): a node's CURRENT canonical concept ids, read exactly like the
// Dag on-node tags — `node_concepts` keys are strings in the wire state while `node.id` is numeric (read
// both), canonicalized through the consolidation rename and de-duped, and prototype-safe (conceptId.js
// keeps LLM-authored ids off the chain). This is the editor's prefill, so the operator edits the same
// set they see on the graph.
export function nodeCanonicalConcepts(nodeConcepts = {}, nodeId, rename = {}) {
  const raw = (nodeConcepts && (nodeConcepts[nodeId] || nodeConcepts[String(nodeId)])) || []
  if (!Array.isArray(raw)) return []
  const seen = new Set(); const out = []
  for (const r of raw) {
    const c = canonicalId(r, rename)
    if (c && !seen.has(c)) { seen.add(c); out.push(c) }
  }
  return out
}

// Parse the operator's free-text tag input (comma- or newline-separated) into the exact concept id list
// the `concept_tag_edited` command carries: each token normalized through the SAME validity gate the
// server applies (`normalizeConceptId` mirrors core.models.valid_concept_id), invalid/empty tokens
// dropped, de-duped in first-seen order, and capped at MAX_NODE_CONCEPTS. Returns `{ concepts, dropped }`
// so the UI can tell the operator when a token was rejected or the overflow trimmed, instead of silently
// sending a different set than they typed.
export function parseConceptTagsInput(text = '') {
  const tokens = String(text == null ? '' : text).split(/[,\n]/)
  const seen = new Set(); const concepts = []; let dropped = 0
  for (const token of tokens) {
    const trimmed = token.trim()
    if (!trimmed) continue                       // pure separators/whitespace are not a rejected token
    const id = normalizeConceptId(trimmed)
    if (!id) { dropped += 1; continue }          // fails the server's valid_concept_id gate
    if (seen.has(id)) continue                   // de-dupe, keep first-seen order
    if (concepts.length >= MAX_NODE_CONCEPTS) { dropped += 1; continue }
    seen.add(id); concepts.push(id)
  }
  return { concepts, dropped }
}
