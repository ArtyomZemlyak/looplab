// Pure model for the CONCEPT SHELF — the concept tree used as a navigational axis over cross-run
// knowledge (lessons, cases, notes). No React; unit-tested with `node --test`.
//
// The server (`looplab/engine/concept_shelf.py`) stamps each memory row with `concepts` +
// `concept_source` and ships a `concept_index` carrying the tree and a coverage receipt. This module
// turns that into the two display modes the Memory panel offers — filter by concept, or group by the
// concept tree — and, just as importantly, into the honest counts that go beside them.
//
// The rule this module exists to enforce: an UNTAGGED row is never silently dropped. A concept filter
// that quietly returns nothing is worse than no filter, because "no results" reads as "this lab learned
// nothing about that" when the truth is "nothing was ever tagged". So every selection reports what it
// could not classify, and the tree grouping always ends with an explicit Untagged bucket.
//
// Ids are canonicalized through the same prototype-safe helpers the rest of the concept UI uses —
// concept ids are LLM-authored free strings, so `__proto__` is a reachable tag.
import { conceptMap, normalizeConceptId } from './conceptId.js'

// The bucket every untagged row lands in. Deliberately NOT a valid concept id (`normalizeConceptId`
// rejects a leading `\u0000`), so it can never collide with a real tag, and deliberately a value rather
// than `null` so it can be a selection like any other — "show me what I cannot classify" is a first-
// class question when you are deciding what to tag next.
// Written as the ESCAPE, never a raw 0x00 byte in the source: a literal NUL makes git classify
// this whole module as BINARY (`Bin 0 -> 6928 bytes`), so it cannot be diffed, blamed, or
// three-way merged, and every future change to it lands unreviewed. The runtime value is the same.
export const UNTAGGED = '\u0000untagged'

// Attribution sources, mirroring `engine/concept_shelf.py::ATTRIBUTION_SOURCES`. They are NOT
// interchangeable: `record` means the row itself was tagged from the experiments it describes, `run`
// only that the run behind it touched the concept. The UI must render the weaker one as weaker.
export const SOURCE_RECORD = 'record'
export const SOURCE_RUN = 'run'

export function rowConcepts(row) {
  const raw = row && Array.isArray(row.concepts) ? row.concepts : []
  const out = []
  for (const value of raw) {
    const id = normalizeConceptId(value)
    if (id && !out.includes(id)) out.push(id)
  }
  return out.sort()
}

export function rowSource(row) {
  const source = row && row.concept_source
  return source === SOURCE_RECORD || source === SOURCE_RUN ? source : null
}

// Whether `row` sits in the SUBTREE of `concept`. Subtree, not equality, is the whole point of using a
// tree as the axis: picking `loss` must find the lesson tagged `loss/contrastive/in-batch`, or the
// hierarchy is decoration. `${concept}/` guards the prefix so `loss` never matches `lossless/…`.
export function rowMatchesConcept(row, concept) {
  if (!concept) return true
  const concepts = rowConcepts(row)
  if (concept === UNTAGGED) return concepts.length === 0
  const prefix = concept + '/'
  return concepts.some(id => id === concept || id.startsWith(prefix))
}

// `{ shown, hidden, hiddenUntagged }` for one selection over one tier. `hiddenUntagged` is the number
// the surface has to disclose: rows the filter removed NOT because they are about something else, but
// because nobody ever said what they are about.
export function filterRows(rows, concept) {
  const shown = [], out = { shown, hidden: 0, hiddenUntagged: 0 }
  for (const row of rows || []) {
    if (rowMatchesConcept(row, concept)) { shown.push(row); continue }
    out.hidden += 1
    if (rowConcepts(row).length === 0) out.hiddenUntagged += 1
  }
  return out
}

// Distinct concepts present across the given tiers, each with the number of rows DIRECTLY carrying it
// and the number carrying it or a descendant. Sorted by id so counts can refresh under the operator
// without moving click targets — the same stability rule `conceptChips.js::chipsAtPath` follows.
export function shelfConcepts(tiers) {
  const direct = conceptMap(), subtree = conceptMap()
  const rows = []
  for (const tier of Object.values(tiers || {})) for (const row of tier || []) rows.push(row)
  for (const row of rows) {
    for (const id of rowConcepts(row)) {
      direct[id] = (direct[id] || 0) + 1
      // Every ancestor prefix gets the subtree credit, which is what makes a root chip a usable
      // entry point: `loss` must show a count even when only `loss/contrastive/dcl` was tagged.
      const parts = id.split('/')
      const seen = new Set()
      for (let k = 1; k <= parts.length; k += 1) {
        const ancestor = parts.slice(0, k).join('/')
        if (seen.has(ancestor)) continue
        seen.add(ancestor)
        subtree[ancestor] = (subtree[ancestor] || 0) + 1
      }
    }
  }
  return Object.keys(direct).concat(Object.keys(subtree).filter(id => !(id in direct)))
    .sort()
    .map(id => ({ id, depth: id.split('/').length - 1, direct: direct[id] || 0, subtree: subtree[id] || 0 }))
}

// The tree display MODE: rows grouped under the concept they carry, deepest-first so a row is filed at
// its most specific tag, plus the terminal Untagged group. A row with several concepts appears under
// each — multi-membership is real, and de-duplicating it to one "primary" concept would invent a
// ranking the data does not carry.
export function groupByConceptTree(rows) {
  const byConcept = conceptMap()
  const untagged = []
  for (const row of rows || []) {
    const concepts = rowConcepts(row)
    if (!concepts.length) { untagged.push(row); continue }
    for (const id of concepts) (byConcept[id] ||= []).push(row)
  }
  const groups = Object.keys(byConcept).sort().map(id => ({
    id, label: id, depth: id.split('/').length - 1, rows: byConcept[id], untagged: false,
  }))
  // Always emitted, even when empty: its absence would read as "everything is tagged", and the count
  // being zero is exactly the signal that says the shelf is trustworthy for this tier.
  groups.push({ id: UNTAGGED, label: 'Untagged', depth: 0, rows: untagged, untagged: true })
  return groups
}

// The receipt sentence the surface shows beside any concept-sorted or concept-filtered result.
// Returns null only when there is nothing at all to describe.
export function coverageSummary(receipt) {
  if (!receipt || !Number.isFinite(receipt.total) || receipt.total <= 0) return null
  // `null`, not 0, when the receipt does not state it: this is the disclosure whose whole purpose is
  // that silence must never read as "this lab learned nothing", and a tier read that partly failed
  // (or an older server) ships a receipt with a total and no `tagged`. Defaulting to 0 turned that
  // into a confident "0 of 214 lessons carry concepts". Both readers render `—` for null.
  const tagged = Number.isFinite(receipt.tagged) ? receipt.tagged : null
  const bySource = (receipt.by_source && typeof receipt.by_source === 'object') ? receipt.by_source : {}
  const fromRun = Number.isFinite(bySource[SOURCE_RUN]) ? bySource[SOURCE_RUN] : 0
  return {
    total: receipt.total,
    tagged,
    untagged: tagged === null ? null : receipt.total - tagged,
    fromRun,
    // A shelf carried entirely by run-level inheritance is coarse: every lesson from one run gets that
    // run's whole concept set, so the filter feels precise while being run-granular. Say so.
    coarse: tagged !== null && tagged > 0 && fromRun === tagged,
    complete: tagged !== null && tagged === receipt.total,
  }
}

// WHAT EACH TIER IS. The panel showed four tabs — Lessons, Cases, Notes, Knowledge — with counts and
// nothing else, so the operator's question was reasonable: "I don't understand what Cases are even
// for", and "there should be captions so I can tell the kinds of memory apart". The tiers really are
// different things with different writers and different readers, and none of that was on screen.
export const MEMORY_TIER_BLURB = Object.freeze({
  lessons: 'What a run CONCLUDED — one transferable claim per row, distilled at the end of a run '
    + 'from its own experiments, with the evidence and confidence behind it. Written by the engine; '
    + 'read back into a later run’s Researcher prompt.',
  cases: 'The winning run’s exact CONFIGURATION — the parameter dict that produced the best metric '
    + 'on this task, kept machine-readable. A later run on the SAME task is handed it in its '
    + 'Researcher prompt instead of rediscovering it. That is why there are so few: it is one per '
    + 'task and objective, not one per experiment.',
  notes: 'A run’s own closing summary — what it ended up with and how far it got, in prose. The '
    + 'CAUSE where a case is the recipe. Context for reading the lessons above, not a claim in '
    + 'itself.',
  knowledge: 'What a HUMAN authored: prompts, skills and notes every run can read. Nothing here is '
    + 'written by a run — that is the whole difference between this tab and the three beside it.',
})

export const memoryTierBlurb = tier => MEMORY_TIER_BLURB[tier] || ''
