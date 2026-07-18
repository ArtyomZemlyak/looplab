// Shared safe concept-id helpers. Concept ids are LLM-authored free strings, so a tag can collide with
// an Object.prototype key ("__proto__", "constructor", "toString", …). Any code that reads a rename map
// or builds a concept-keyed map MUST go through here, or one weird tag reaches the prototype chain — a
// silent wrong-key at best (`rename["constructor"]` -> Object's constructor, truthy), a crash at worst
// (`acc["__proto__"] ||= new Set()` reads Object.prototype, skips the assignment, then `.add()` throws).
// Two guarantees: canonicalId never reads an inherited property; conceptMap is a null-prototype map so
// building/reading it with an agent key can never touch the chain.

const MAX_ID_CHARS = 256
const MAX_ID_DEPTH = 12
const MAX_RENAME_HOPS = 16

export function normalizeConceptId(raw) {
  if (typeof raw !== 'string' || [...raw].length > MAX_ID_CHARS) return ''
  const value = raw.trim().toLowerCase().replaceAll(' ', '-').replace(/^\/+|\/+$/g, '')
  const parts = value.split('/')
  return value && [...value].length <= MAX_ID_CHARS && parts.length <= MAX_ID_DEPTH
    && parts.every(Boolean) && !/[\s\p{C}]/u.test(value) ? value : ''
}

// Canonicalize exactly like the server ConceptFrame boundary: normalize each hop, follow a bounded
// raw-or-normalized rename chain, and fail closed on invalid ids/cycles. Every lookup is own-property
// guarded, so an agent-authored prototype name can never resolve through Object.prototype.
export function canonicalId(raw, rename = {}) {
  const map = rename && typeof rename === 'object' ? rename : {}
  const seen = new Set()
  let current = raw
  for (let hop = 0; hop <= MAX_RENAME_HOPS; hop += 1) {
    const canonical = normalizeConceptId(current)
    if (!canonical || seen.has(canonical)) return ''
    seen.add(canonical)
    // Replay can retain an exact legacy raw key, while current producers store the
    // normalized key. Mirror the server's lookup order so both surfaces join on one vocabulary.
    const exact = Object.prototype.hasOwnProperty.call(map, current)
    const normalized = current !== canonical && Object.prototype.hasOwnProperty.call(map, canonical)
    if (!exact && !normalized) return canonical
    const next = map[exact ? current : canonical]
    if (next == null) return canonical
    current = next
  }
  return ''
}

// A null-prototype object usable as a map with untrusted string keys (no inherited props to shadow or
// trip over). Prefer this (or a real Map) over `{}` anywhere concept ids become keys.
export function conceptMap() {
  return Object.create(null)
}

// Compatibility reader for the coarse, single-slot direction used by the legacy "theme" UI.
// With run state, mirror events/digest.py::node_theme exactly: folded post-rename node_concepts are
// authoritative (including an explicit empty membership), and only a genuinely missing folded row
// may fall back to the immutable authoring payload. This keeps DAG grouping, charts and reports in
// the same vocabulary as Concepts after classifier re-tags, operator edits and run-base deltas.
export function nodeTheme(node, state = null) {
  const memberships = state?.node_concepts
  const nodeId = node?.id
  if (memberships && typeof memberships === 'object' && !Array.isArray(memberships)
      && nodeId != null && Object.hasOwn(memberships, String(nodeId))) {
    const rename = state?.concept_consolidation || {}
    const raw = Array.isArray(memberships[String(nodeId)])
      ? memberships[String(nodeId)] : []
    const axes = new Set()
    for (const concept of raw) {
      const canonical = canonicalId(concept, rename)
      const axis = canonical.split('/', 1)[0]
      if (axis) axes.add(axis)
    }
    return [...axes].sort()[0] || null
  }
  const legacy = node?.idea?.theme
  if (legacy) return String(legacy)
  const concepts = node?.idea?.concepts
  if (!Array.isArray(concepts)) return null
  for (const concept of concepts) {
    const axis = String(concept == null ? '' : concept).trim().split('/', 1)[0].trim()
    if (axis) return axis
  }
  return null
}
