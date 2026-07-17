// Shared safe concept-id helpers. Concept ids are LLM-authored free strings, so a tag can collide with
// an Object.prototype key ("__proto__", "constructor", "toString", …). Any code that reads a rename map
// or builds a concept-keyed map MUST go through here, or one weird tag reaches the prototype chain — a
// silent wrong-key at best (`rename["constructor"]` -> Object's constructor, truthy), a crash at worst
// (`acc["__proto__"] ||= new Set()` reads Object.prototype, skips the assignment, then `.add()` throws).
// Two guarantees: canonicalId never reads an inherited property; conceptMap is a null-prototype map so
// building/reading it with an agent key can never touch the chain.

// Normalize a raw concept id the SAME way the server's concept_frame.py::concept_id() does — trim,
// case-fold, spaces->hyphens, strip leading/trailing slashes — so the client keys the SAME vocabulary
// the /concepts frame ships (which is normalized). Without this the graph-tab surfaces (chip bar, on-node
// Dag tags, highlight matching) key raw-cased ids while the tree/table use the normalized ids, so a
// Researcher-authored "Regularization/R-Drop" splits into two concepts across the two tabs and a
// lower-cased chip never highlights an upper-cased-authored node.
export function normalizeConceptId(raw) {
  return String(raw == null ? '' : raw).trim().toLowerCase().replace(/ /g, '-').replace(/^\/+|\/+$/g, '')
}

// Canonicalize a raw concept id: normalize it, then resolve the consolidation rename CHAIN exactly as the
// server's canonical_concept does (try the rename map by the raw id first, then by the normalized id,
// re-normalizing each hop, bounded to avoid a cycle). Every rename lookup is hasOwnProperty-guarded so an
// id that names a prototype property ("constructor", "__proto__", …) can never be silently replaced by an
// inherited value; an empty mapping ends the chain at the current normalized id.
export function canonicalId(raw, rename = {}) {
  let current = String(raw == null ? '' : raw)
  const seen = new Set()
  for (let hop = 0; hop < 8; hop++) {                       // MAX_RENAME_HOPS-style bound; cannot loop
    const canon = normalizeConceptId(current)
    if (!canon || seen.has(canon)) return canon
    seen.add(canon)
    let next
    if (rename && Object.prototype.hasOwnProperty.call(rename, current)) next = rename[current]
    else if (rename && current !== canon && Object.prototype.hasOwnProperty.call(rename, canon)) next = rename[canon]
    if (next == null || next === '') return canon          // no (further) rename -> the normalized id wins
    current = String(next)
  }
  return normalizeConceptId(current)
}

// A null-prototype object usable as a map with untrusted string keys (no inherited props to shadow or
// trip over). Prefer this (or a real Map) over `{}` anywhere concept ids become keys.
export function conceptMap() {
  return Object.create(null)
}
