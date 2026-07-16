// Shared safe concept-id helpers. Concept ids are LLM-authored free strings, so a tag can collide with
// an Object.prototype key ("__proto__", "constructor", "toString", …). Any code that reads a rename map
// or builds a concept-keyed map MUST go through here, or one weird tag reaches the prototype chain — a
// silent wrong-key at best (`rename["constructor"]` -> Object's constructor, truthy), a crash at worst
// (`acc["__proto__"] ||= new Set()` reads Object.prototype, skips the assignment, then `.add()` throws).
// Two guarantees: canonicalId never reads an inherited property; conceptMap is a null-prototype map so
// building/reading it with an agent key can never touch the chain.

// Canonicalize a raw concept id through the consolidation rename map. The rename lookup is guarded with
// hasOwnProperty so a raw id that names a prototype property is NOT silently replaced by an inherited
// value; a falsy mapping falls back to the raw id (matching the old `rename[raw] || raw` intent).
export function canonicalId(raw, rename = {}) {
  // REVIEW(2026-07-16): this canonicalizer does NOT mirror the server's _normalize_concept_id
  // (strip/lowercase/space->hyphen/trim slashes), so the client and server key DIFFERENT
  // vocabularies for the same run. Server projections normalize (graph_from_node_concepts,
  // project_hierarchy, concept_touch_counts all lowercase via _normalize_concept_id), but authored
  // ids fold RAW into node_concepts and the client joins on that raw string: a Researcher-authored
  // "Regularization/R-Drop" gets a lowercased tree/metrics row while experimentsByConcept keys the
  // raw-cased id — the Concept view renders the row with metrics but ZERO experiments/badge. Mirror
  // the server normalization here (trim, casefold, spaces->hyphens, strip slashes) before the rename
  // lookup, or have the server ship pre-normalized node_concepts in /state so there is one vocabulary.
  const key = String(raw == null ? '' : raw)
  if (rename && Object.prototype.hasOwnProperty.call(rename, key)) {
    const mapped = rename[key]
    if (mapped) return String(mapped)
  }
  return key
}

// A null-prototype object usable as a map with untrusted string keys (no inherited props to shadow or
// trip over). Prefer this (or a real Map) over `{}` anywhere concept ids become keys.
export function conceptMap() {
  return Object.create(null)
}
