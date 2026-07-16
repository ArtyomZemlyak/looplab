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
