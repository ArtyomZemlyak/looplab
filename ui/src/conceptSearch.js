// Pure search/filter model shared by BOTH concept views -- no React, unit-tested with `node --test`.
// View 2 (chip bar over the graph): free-text concept lookup that live-previews a graph highlight and,
// on commit, feeds the SAME onHighlight -> conceptHighlight -> Dag dimming path the chip selection uses.
// View 1 (concept tree): filters the projected tree to matching concepts/experiments, keeping every
// ancestor on the path so nested matches stay reachable. Concept ids are LLM-authored, so every id that
// becomes a map key goes through the prototype-safe helpers in conceptId.js (see its header). The graph
// highlight reuses matchingNodeIds from conceptChips.js so search and chip selection stay one mechanism.
import { canonicalId } from './conceptId.js'
import { matchingNodeIds } from './conceptChips.js'

const DEFAULT_LIMIT = 50   // cap the results list so a 1-char query cannot balloon the dropdown/DOM

// Normalize a user query the way every matcher below reads it: trimmed + lower-cased ('' when absent).
export function normalizeQuery(query) {
  return typeof query === 'string' ? query.trim().toLowerCase() : ''
}

// Segment a string around the FIRST case-insensitive occurrence of the query, for <mark> rendering.
// Returns [{text, hit}] with hit=true on the matched slice; no query / no match -> one non-hit segment.
// Pure and allocation-light so the component maps segments to <mark> without dangerouslySetInnerHTML
// (the CodeViewer `highlighted` helper does the same by hand; this keeps it testable and id-safe).
export function highlightSegments(text, query) {
  const value = text == null ? '' : String(text)
  const q = normalizeQuery(query)
  if (!q) return [{ text: value, hit: false }]
  const index = value.toLowerCase().indexOf(q)
  if (index < 0) return [{ text: value, hit: false }]
  const segments = []
  if (index > 0) segments.push({ text: value.slice(0, index), hit: false })
  segments.push({ text: value.slice(index, index + q.length), hit: true })
  if (index + q.length < value.length) segments.push({ text: value.slice(index + q.length), hit: false })
  return segments
}

// The full concept universe for View 2: every canonical tag AND each of its ancestor prefixes, mapped to
// the set of nodes touching that subtree. Ancestors are included because the chip bar surfaces coarse
// levels (`loss`) even when only `loss/contrastive/dcl` is tagged -- searching must find them too. A node
// tagged with a concept counts toward that concept and every ancestor (Set-deduped), matching the exact
// subtree-count semantics of chipsAtPath. Returns a Map(conceptId -> Set(nodeId)); Map keeps untrusted
// ids as data, never prototype keys.
export function conceptUniverse(nodeConcepts = {}, rename = {}) {
  const counts = new Map()
  if (!nodeConcepts || typeof nodeConcepts !== 'object') return counts
  for (const [nid, ids] of Object.entries(nodeConcepts)) {
    const id = Number(nid)
    if (!Number.isSafeInteger(id)) continue
    for (const raw of (Array.isArray(ids) ? ids : [])) {
      const canonical = canonicalId(raw, rename)
      if (!canonical) continue
      const parts = canonical.split('/')
      for (let depth = 1; depth <= parts.length; depth += 1) {
        const ancestor = parts.slice(0, depth).join('/')
        let set = counts.get(ancestor)
        if (!set) counts.set(ancestor, (set = new Set()))
        set.add(id)
      }
    }
  }
  return counts
}

// Rank the concepts whose id contains the query (case-insensitive substring over the FULL path, so both
// `loss` and `dcl` find `loss/contrastive/dcl`). Leaf-anchored matches rank first (exact leaf, then leaf
// prefix, then leaf substring, then deep-path-only), then by subtree count desc, then id asc for a stable
// order. Returns [{id, label, count}] capped at `limit`. Empty query -> [].
export function searchConcepts(nodeConcepts = {}, rename = {}, query = '', limit = DEFAULT_LIMIT) {
  const q = normalizeQuery(query)
  if (!q) return []
  const universe = conceptUniverse(nodeConcepts, rename)
  const scored = []
  for (const [id, nodes] of universe) {
    const idLower = id.toLowerCase()
    if (!idLower.includes(q)) continue
    const leaf = id.split('/').pop()
    const leafLower = leaf.toLowerCase()
    const rank = leafLower === q ? 0 : leafLower.startsWith(q) ? 1 : leafLower.includes(q) ? 2 : 3
    scored.push({ id, label: leaf, count: nodes.size, rank })
  }
  scored.sort((a, b) => a.rank - b.rank || b.count - a.count || a.id.localeCompare(b.id))
  const cap = Number.isSafeInteger(limit) && limit > 0 ? limit : DEFAULT_LIMIT
  return scored.slice(0, cap).map(({ id, label, count }) => ({ id, label, count }))
}

// The graph highlight set for a live query preview: union of every node touching a matched concept
// subtree. No matches (or empty query) -> null, meaning "no dimming" -- the same contract the chip
// selection uses, so the search preview and a pinned selection drive Dag identically. Reuses
// matchingNodeIds (subtree/prefix OR match) by handing it the matched ids as the selection.
export function searchHighlightIds(nodeConcepts = {}, rename = {}, query = '') {
  const results = searchConcepts(nodeConcepts, rename, query)
  if (!results.length) return null
  return matchingNodeIds(nodeConcepts, results.map(result => result.id), rename)
}

// Does a frame ExperimentRef match the query? Matches the fields the concept frame actually carries per
// experiment -- the node id (with or without a leading `#`) and the lifecycle status -- not the live
// node's operator, which is not part of the generation-bound ref. Empty query -> false.
export function experimentRefMatches(ref, query) {
  const q = normalizeQuery(query)
  if (!q || !ref || typeof ref !== 'object') return false
  const nodeId = ref.node_id
  const idMatch = Number.isFinite(nodeId)
    && (('#' + nodeId).includes(q) || String(nodeId).includes(q))
  const statusMatch = typeof ref.status === 'string' && ref.status.toLowerCase().includes(q)
  return idMatch || statusMatch
}

// Filter the View 1 concept tree by a query. Operates on the validated projection shape
// (tree.nodes[id] = {parent, children, ...}) and the by-concept experiment map
// (experimentRefs[id] = [ExperimentRef]). Concept ids from the projection are already canonical, but they
// are still untrusted strings, so every lookup is own-property guarded. Returns null for an empty query
// (no filtering). Otherwise:
//   visible:      Set(conceptId) -- rows to keep: every match plus every ancestor on its path to a root
//   expand:       Set(conceptId) -- concepts to force-open so a nested match is reachable in the DFS
//   evidenceOpen: Set(conceptId) -- concepts whose experiment evidence should auto-open (an exp matched)
//   conceptHit:   Set(conceptId) -- concepts whose own id/label matched (for row emphasis)
export function filterConceptTree(tree, experimentRefs = {}, query = '', opts = {}) {
  const q = normalizeQuery(query)
  if (!q) return null
  const result = { visible: new Set(), expand: new Set(), evidenceOpen: new Set(), conceptHit: new Set() }
  const nodes = tree && typeof tree === 'object' ? tree.nodes : null
  if (!nodes || typeof nodes !== 'object') return result
  const refs = experimentRefs && typeof experimentRefs === 'object' ? experimentRefs : {}
  const matchExperiments = opts.matchExperiments !== false
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key)

  for (const id of Object.keys(nodes)) {
    if (!has(nodes, id)) continue
    const node = nodes[id]
    if (!node || typeof node !== 'object') continue
    const leaf = String(id).split('/').pop().toLowerCase()
    const selfHit = id.toLowerCase().includes(q) || leaf.includes(q)
    const expHit = matchExperiments && has(refs, id) && Array.isArray(refs[id])
      && refs[id].some(ref => experimentRefMatches(ref, query))
    if (selfHit) result.conceptHit.add(id)
    if (expHit) result.evidenceOpen.add(id)
    if (!selfHit && !expHit) continue
    // Walk to the root marking the match + every ancestor visible, expanding ancestors so the DFS in
    // visibleConceptRows reaches this row. Bounded by the projection's own depth cap (256).
    let current = id
    for (let guard = 0; current != null && guard <= 256; guard += 1) {
      result.visible.add(current)
      const parent = has(nodes, current) && nodes[current] ? nodes[current].parent : null
      if (parent == null) break
      result.expand.add(parent)
      current = parent
    }
  }
  return result
}
