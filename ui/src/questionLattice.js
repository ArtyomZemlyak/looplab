// The question lattice — pure model, no React, no I/O (`node --test` drives it directly).
//
// WHAT THIS IS NOT: `conceptForest.js` builds a PATH tree, where `loss/contrastive/dcl` has exactly
// one parent and containment is decided by the slash. This is a different shape, and reusing that
// builder would give one that describes neither: a question is a SET of concepts, and `{distill}`
// and `{llm}` are both supersets-of-nothing while `{distill, llm}` sits under BOTH of them.
//
// THE OPERATOR'S THREE DECISIONS, one rule each:
//   * a row with two parents is DUPLICATED under both, never assigned a canonical one — picking
//     would hide half the structure and the choice would depend on iteration order;
//   * a question shows the BEST delta among its descendants, not a sum and not its own (it has none);
//   * comparability is TRANSPARENT — a question whose descendants were measured against different
//     baselines says so instead of reporting a "best" across numbers that never met.

import { cardIsDirection, cardParentId } from './cardLineageModel.js'
import { isRecord } from './panelPrimitives.js'
import { nodesSplitByComparability } from './runIndex.js'

export const UNGROUPED_ID = '__no_concepts__'
// The experiments no question owns. A concept lattice has no position for a card with no parent —
// it is not a question, so it is not a row — and the Directions view's "Not filed under any
// direction" group is the ONLY surface that has ever drawn them. Retiring that tab without this
// bucket would not tidy the board, it would delete rows: measured on `runs/e5small-dr-unified-v5`,
// the operator's first complaint about this board was two such cards.
export const UNFILED_EXPERIMENTS_ID = '__unfiled_experiments__'

// A question's set, normalised: sorted, de-duplicated, blank-free. Sorting is what makes the key
// stable — `{a,b}` and `{b,a}` are one set and must not render as two rows.
export function conceptSet(card) {
  const raw = isRecord(card) && Array.isArray(card.concept_tags) ? card.concept_tags : []
  const seen = new Set()
  for (const tag of raw) {
    if (typeof tag === 'string' && tag.trim()) seen.add(tag.trim())
  }
  return [...seen].sort()
}

// Is `outer` a STRICT subset of `inner`? Both are the normalised sorted arrays above, so this is a
// linear merge rather than Set arithmetic. Strictness is load-bearing: an equal pair is the SAME
// position in the lattice, not a sharpening of itself, and treating it as one would nest a
// duplicate-tagged question under its twin forever.
export function isStrictSubset(outer, inner) {
  if (outer.length >= inner.length) return false
  let i = 0
  for (const tag of inner) {
    if (i < outer.length && outer[i] === tag) i += 1
  }
  return i === outer.length
}

// The rows of the lattice, in render order, DUPLICATED under every immediate parent.
//
// "Immediate" is the whole subtlety. With `{a}`, `{a,b}` and `{a,b,c}` on the board, `{a}` is a
// strict subset of `{a,b,c}` too — hanging the row under both would draw one subtree at two depths
// and double every number rolled up through it. A candidate parent survives only when no OTHER
// candidate sits strictly between it and the child.
export function latticeRows(cards, { order } = {}) {
  const sort = typeof order === 'function'
    ? order
    : (a, b) => String(a.id).localeCompare(String(b.id))
  const entries = (Array.isArray(cards) ? cards : [])
    .filter(c => isRecord(c) && c.id)
    .map(card => ({ card, id: String(card.id), tags: conceptSet(card) }))
  const tagged = entries.filter(e => e.tags.length)
  const untagged = entries.filter(e => !e.tags.length)

  const parentsOf = new Map()
  for (const entry of tagged) {
    const strict = tagged.filter(o => o !== entry && isStrictSubset(o.tags, entry.tags))
    parentsOf.set(entry, strict.filter(
      cand => !strict.some(o => o !== cand && isStrictSubset(cand.tags, o.tags))))
  }

  const out = []
  const emit = (entry, depth, path) => {
    // A cycle cannot occur on strict subsets — the set size increases at every step — so the only
    // guard needed is against re-entering an id already on this branch, which equal-tag pairs would
    // otherwise do if `isStrictSubset` were ever loosened.
    if (path.includes(entry.id)) return
    out.push({
      id: entry.id,
      card: entry.card,
      tags: entry.tags,
      depth,
      // Unique per PLACEMENT, not per card: a duplicated row needs its own React key and its own
      // collapse state, or collapsing one copy silently collapses the other.
      rowKey: [...path, entry.id].join('>'),
      parentId: path.length ? path[path.length - 1] : null,
      duplicated: (parentsOf.get(entry) || []).length > 1,
    })
    const kids = tagged
      .filter(o => (parentsOf.get(o) || []).includes(entry))
      .sort((a, b) => sort(a.card, b.card))
    for (const kid of kids) emit(kid, depth + 1, [...path, entry.id])
  }

  for (const root of tagged.filter(e => !(parentsOf.get(e) || []).length)
    .sort((a, b) => sort(a.card, b.card))) {
    emit(root, 0, [])
  }
  // Untagged rows are neither dropped nor hoisted among the real roots: a question with no concepts
  // has no position in the lattice, and putting it at depth 0 would seat it beside questions whose
  // position is measured. Its own bucket, always last.
  for (const entry of untagged.sort((a, b) => sort(a.card, b.card))) {
    out.push({
      id: entry.id,
      card: entry.card,
      tags: [],
      depth: 0,
      rowKey: `${UNGROUPED_ID}>${entry.id}`,
      parentId: UNGROUPED_ID,
      duplicated: false,
    })
  }
  return out
}

// Every DESCENDANT of a row, by id — the set whose deltas roll up into it. Computed from the emitted
// rows rather than re-walking the lattice, so a row that was refused a placement above (path guard)
// contributes nothing here either: one traversal, one answer.
export function descendantIds(rows, rowKey) {
  const start = rows.find(r => r.rowKey === rowKey)
  if (!start) return []
  const prefix = `${rowKey}>`
  const out = new Set()
  for (const row of rows) {
    if (row.rowKey.startsWith(prefix)) out.add(row.id)
  }
  return [...out]
}

// The number a question row wears: the BEST delta any descendant measured, and whether the field it
// won was measured the same way.
//
// MAX, NOT SUM — the operator's call, and the engine already agrees: `core/cards.py::card_child_rollup`
// takes the max over a direction's children and a child with no measurement contributes nothing
// rather than a zero. Summing would let two experiments that tested the SAME sharpening add their
// improvements together and report a gain nobody measured.
//
// THE ROW COUNTS ITSELF, and getting this wrong is what the first cut of this function did. A
// question's own number is not `Card.best_delta` — a question owns no experiment, so that field is
// null on every one of them — it is `child_rollup.best_delta`, the best its OWN experiments
// measured (`core/cards.py::card_child_rollup`). Scanning descendants only reported `null` for
// `{distill}` whenever nobody had asked a SHARPER question yet, i.e. exactly the early board.
//
// AND DESCENDANTS, NOT ONLY CHILDREN, which is where this parts from the engine's rollup:
// `{distill}` is answered by `{distill,llm,rl}` just as much as by `{distill,llm}`, and stopping at
// the immediate row would headline a question with a number strictly worse than its subtree found.
//
// THE CAVEAT DOES NOT SUPPRESS THE NUMBER. When two descendants' nodes carry provably different
// comparability keys the best is still shown, marked — the same choice `runIndex.js` makes for a
// champion (`CHAMPION_CAVEAT_MIXED_COMPARABILITY`). Hiding it would leave the operator with no
// number at all on exactly the questions that got the most work, and "unknown" is not "different":
// absent keys are silence, so a board where nothing records a key is never marked.
// A card's OWN measured value. A QUESTION's lives in `child_rollup` (the best its own experiments
// reached); an EXPERIMENT's is its own `best_delta`. Reading only the second reports null for every
// question, reading only the first reports null for every experiment, so both are consulted and the
// rollup is preferred — on a card that has one, `best_delta` is null by construction.
const finite = value => (typeof value === 'number' && Number.isFinite(value) ? value : null)
export function ownBest(card) {
  if (!isRecord(card)) return null
  const rollup = isRecord(card.child_rollup) ? finite(card.child_rollup.best_delta) : null
  return rollup !== null ? rollup : finite(card.best_delta)
}

// The nodes a card's number rests on: its own audit set, plus — for a QUESTION — the audit sets of
// the experiments filed under it. A question's own `evidence` is empty by construction (it owns no
// action), so reading only that field marks nothing on exactly the rows that aggregate the most
// work, and the comparability of a question's headline number is a fact about the nodes its
// EXPERIMENTS ran on. `child_card_ids` is clipped at `CARD_CHILD_LIMIT`; the resulting marker is
// therefore a claim about the children the wire carries, which is also all this view can draw.
function cardEvidenceNodes(card, byId) {
  const out = Array.isArray(card.evidence) ? [...card.evidence] : []
  for (const childId of Array.isArray(card.child_card_ids) ? card.child_card_ids : []) {
    const child = byId.get(String(childId))
    if (isRecord(child) && Array.isArray(child.evidence)) out.push(...child.evidence)
  }
  return out
}

export function latticeRollups(state, cards, rows) {
  const byId = new Map((Array.isArray(cards) ? cards : [])
    .filter(c => isRecord(c) && c.id).map(c => [String(c.id), c]))
  const nodes = isRecord(state?.nodes) ? state.nodes : {}
  const out = new Map()
  for (const row of Array.isArray(rows) ? rows : []) {
    const ids = descendantIds(rows, row.rowKey)
    let best = null
    let bestCardId = null
    const measured = []
    for (const id of [row.id, ...ids]) {
      const card = byId.get(id)
      if (!isRecord(card)) continue
      for (const nodeId of cardEvidenceNodes(card, byId)) {
        const node = nodes[nodeId]
        if (isRecord(node)) measured.push(node)
      }
      const delta = ownBest(card)
      // `isinstance(True, float)` has no JS twin — `typeof true` is 'boolean', so a bool cannot pose
      // as a delta — but NaN/inf can, and a question headlined "best +Infinity" is worse than one
      // headlined nothing. Same refusal as `card_child_rollup`.
      if (typeof delta === 'number' && Number.isFinite(delta) && (best === null || delta > best)) {
        best = delta
        bestCardId = id
      }
    }
    out.set(row.rowKey, {
      descendants: ids.length,
      own: ownBest(row.card),
      best,
      bestCardId,
      measuredNodes: measured.length,
      mixedComparability: nodesSplitByComparability(measured),
    })
  }
  return out
}

// A question the run has CLOSED, and whether the closure rests on anything.
//
// The operator's own suspicion, made checkable: *"we cannot discard a direction if we have no
// experiments that are more precise."* A question is closed by `status: dropped` or by
// `verdict: abandoned` (`core/cards.py`), and the closure is SUPPORTED when the run actually
// narrowed it — at least one sharper question below it, or at least one experiment of its own that
// produced evidence. Otherwise the row was abandoned on nothing.
//
// IT ONLY REPORTS. Nothing here reopens a card, hides a row or changes a number: an unsupported
// closure is a fact for the operator to act on, and a view that quietly un-dropped a card would be
// asserting a decision the run's own record does not carry. A closed row is DIMMED and never
// removed, which is what keeps the chain readable — the whole point of the ladder.
export const CLOSED_QUESTION_STATUSES = new Set(['dropped'])
export const CLOSED_QUESTION_VERDICTS = new Set(['abandoned'])

export function questionClosure(card, rollup) {
  if (!isRecord(card)) return null
  const status = typeof card.status === 'string' ? card.status : ''
  const verdict = typeof card.verdict === 'string' ? card.verdict : ''
  if (!CLOSED_QUESTION_STATUSES.has(status) && !CLOSED_QUESTION_VERDICTS.has(verdict)) return null
  const sharper = isRecord(rollup) && Number.isSafeInteger(rollup.descendants)
    ? rollup.descendants : 0
  // An experiment that never ran is not a narrowing. `evidence` is the audit set — the nodes that
  // TESTED the card — which is exactly the distinction `card_ledger` draws between a build
  // reservation and evidence.
  const measured = isRecord(rollup) && Number.isSafeInteger(rollup.measuredNodes)
    ? rollup.measuredNodes : 0
  return {
    closed: true,
    by: CLOSED_QUESTION_STATUSES.has(status) ? status : verdict,
    supported: sharper > 0 || measured > 0,
    sharper,
    measured,
  }
}

// The experiments filed under NO question, in render order. The complement of the ladder over the
// same card list, so between them the two cover every card the wire carried — which is the property
// that makes retiring the Directions tab safe rather than lossy.
//
// PARENT-BASED, never concept-based: an experiment carrying `loss/contrastive` is still unfiled if
// no question claims it, and grouping it by its tags would seat it in the lattice as though some
// question owned it. The edge is the claim; the tags are a description.
export function unfiledExperiments(cards, { order } = {}) {
  const rows = (Array.isArray(cards) ? cards : []).filter(c => isRecord(c) && c.id)
  const sort = typeof order === 'function'
    ? order
    : (a, b) => String(a.id).localeCompare(String(b.id))
  return rows
    // A question is never "unfiled" — a root question is a root, and it already has its own row.
    .filter(card => !cardIsDirection(card))
    // UNFILED means the card DECLARES no parent. An edge naming a card this page does not hold —
    // clipped by the 256-row wire cap, or filtered out — is still an edge, and drawing that card
    // here would assert the run has no question for it when it has one it cannot show.
    //
    // The first cut of this filter also consulted a set of known ids and was exactly equivalent to
    // `!parent` in every branch — dead logic wearing a rule's clothes. Removed rather than kept
    // "for clarity": a condition that cannot change the answer is a claim nobody can check.
    .filter(card => !cardParentId(card))
    .sort(sort)
}
