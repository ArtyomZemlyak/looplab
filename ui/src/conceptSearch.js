// Pure search/filter model shared by BOTH concept views -- no React, unit-tested with `node --test`.
// View 2 (chip bar over the graph): free-text concept lookup that live-previews a graph highlight and,
// on commit, feeds the SAME onHighlight -> conceptHighlight -> Dag dimming path the chip selection uses.
// View 1 (concept tree): filters the projected tree to matching concepts/experiments, keeping every
// ancestor on the path so nested matches stay reachable. Concept ids are LLM-authored, so every id that
// becomes a map key goes through the prototype-safe helpers in conceptId.js (see its header). The graph
// highlight reuses matchingNodeIds from conceptChips.js so search and chip selection stay one mechanism.
import { canonicalId } from './conceptId.js'

const DEFAULT_LIMIT = 50    // cap the results list so a 1-char query cannot balloon the dropdown/DOM
const MAX_SUBTREE = 20_000 // bound the matched-subtree walk (>= the projection's 10k node cap) vs cycles

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
  // Lower-casing can expand one original character (for example İ -> i + combining dot), so an
  // index in the folded string is not necessarily an index in `value`. Keep a code-unit map while
  // folding and translate the matched span back before slicing the original display text.
  let folded = ''
  const foldedStarts = []
  const foldedEnds = []
  let originalIndex = 0
  for (const character of value) {
    const lower = character.toLowerCase()
    const originalEnd = originalIndex + character.length
    folded += lower
    for (let offset = 0; offset < lower.length; offset += 1) {
      foldedStarts.push(originalIndex)
      foldedEnds.push(originalEnd)
    }
    originalIndex = originalEnd
  }
  const index = folded.indexOf(q)
  if (index < 0) return [{ text: value, hit: false }]
  const start = foldedStarts[index]
  const end = foldedEnds[index + q.length - 1]
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)) {
    return [{ text: value, hit: false }]
  }
  const segments = []
  if (start > 0) segments.push({ text: value.slice(0, start), hit: false })
  segments.push({ text: value.slice(start, end), hit: true })
  if (end < value.length) segments.push({ text: value.slice(end), hit: false })
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

// The graph highlight for a live preview is the union of nodes touching any matched concept subtree.
// The chip bar builds it directly from the ranked searchConcepts result via matchingNodeIds (the same
// subtree/prefix OR match a pinned chip selection uses), so search preview and selection drive Dag
// identically without this model rebuilding the concept universe a second time per keystroke.

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
    // Path descendants include the matched id and therefore match on their own. Edge-lens children
    // are arbitrary relation endpoints, so explicitly retain and force-open the matched subtree.
    if (selfHit && opts.edgeProjection === true) {
      const pending = [id]
      const visited = new Set()
      while (pending.length && visited.size < MAX_SUBTREE) {
        const current = pending.pop()
        if (visited.has(current) || !has(nodes, current)) continue
        visited.add(current)
        result.visible.add(current)
        const children = Array.isArray(nodes[current]?.children) ? nodes[current].children : []
        const ownedChildren = children.filter(child => typeof child === 'string' && has(nodes, child))
        if (ownedChildren.length) result.expand.add(current)
        pending.push(...ownedChildren)
      }
    }
  }
  return result
}
