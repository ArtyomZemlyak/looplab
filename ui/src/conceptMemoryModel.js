// FROM A CONCEPT INTO WHAT THE LAB LEARNED ABOUT IT.
//
// The global concept view could answer "which RUNS touched this idea" and nothing else, while the
// Memory panel held the lessons, cases and notes indexed by the very same concept ids — one click
// away, in a different screen, with no path between them. The operator asked for that path twice:
// "you have to be able to go from a concept into memory, notes, cases, knowledge".
//
// This is the join, and it is a pure fold over the payload the Memory panel already fetches. Subtree
// matching comes from `conceptShelf.rowMatchesConcept`, so a selection on `loss` answers with
// everything under `loss/contrastive/...` — one definition of what "about this concept" means,
// shared by both surfaces, because two would drift.
import { rowMatchesConcept, rowConcepts } from './conceptShelf.js'

// The tiers, in the order an operator reads them: what was concluded, what solved a task, what a run
// noted. `knowledge` is deliberately absent — it is human-authored and carries no run concepts, so a
// concept can never select it, and showing an always-empty group would imply otherwise.
export const CONCEPT_MEMORY_TIERS = Object.freeze([
  ['lessons', 'Lessons', 'statement'],
  ['cases', 'Cases', 'goal'],
  ['notes', 'Notes', 'note'],
])

/**
 * What this lab learned about `concept`, per tier.
 *
 * `untagged` is the honest denominator: rows that carry NO concept at all cannot be selected by any
 * concept, and a surface that shows "2 lessons" without saying "and 147 carry no concept" invites
 * the reader to conclude the lab has learned almost nothing about everything else.
 */
export function conceptMemory(memory, concept) {
  const groups = []
  let untagged = 0
  let total = 0
  for (const [key, label, textField] of CONCEPT_MEMORY_TIERS) {
    const rows = Array.isArray(memory?.[key]) ? memory[key] : []
    total += rows.length
    const matched = []
    for (const row of rows) {
      if (rowConcepts(row).length === 0) untagged += 1
      if (concept && rowMatchesConcept(row, concept)) matched.push({ ...row, _text: row?.[textField] })
    }
    if (matched.length) groups.push({ key, label, rows: matched })
  }
  return { groups, untagged, total, matched: groups.reduce((n, g) => n + g.rows.length, 0) }
}

/** One line under the section: what was found, and what could never have been found. */
export function conceptMemoryNotice(result) {
  if (!result || !result.total) return 'Cross-run memory is empty, so nothing can be linked yet.'
  if (!result.matched) {
    return result.untagged >= result.total
      ? `Nothing links to this concept — none of the ${result.total} memory rows carries a concept `
        + 'at all, so no concept could select any of them.'
      : `Nothing links to this concept. ${result.untagged} of ${result.total} memory rows carry no `
        + 'concept, so they could not be selected by any concept either.'
  }
  return result.untagged
    ? `${result.untagged} of ${result.total} memory rows carry no concept and cannot be reached from `
      + 'any concept.'
    : ''
}
