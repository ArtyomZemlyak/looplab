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

import { cardIsDirection, cardLineageIndex, cardParentId } from './cardLineageModel.js'
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
  // A QUESTION INHERITS THE CONCEPTS OF THE EXPERIMENTS FILED UNDER IT, and until 2026-08-26 this
  // reader ignored the field that says so. `looplab/events/card_ledger.py::_apply_card_lineage` computes `child_concept_tags`
  // as the union over every child — deliberately written there and never onto `concept_tags`, whose
  // `concept_source` provenance records who AUTHORED a membership and may not be handed a derived
  // union. This function read only the authored field, so a question with no memo-authored tags
  // stayed permanently ungrouped even after tagged children arrived.
  //
  // THE OPERATOR REPORTED THE WHOLE CONSEQUENCE AT ONCE — "the cards are filed nowhere, where is the
  // hierarchy, where do concepts attach to questions, and why do Directions and Research look the
  // same" — and it is one cause: an empty set is a subset of nothing useful, so NO nesting relation
  // exists, every row falls to the ungrouped bucket, and the ladder degenerates into exactly the flat
  // parent->child list the Directions tab drew. Measured on `runs/e5small-dr-unified-v7`: five
  // questions, ALL with `concept_tags = []`, one of them carrying `child_concept_tags` of NINE ids
  // from the experiment filed under it — the union present, populated, and unread.
  //
  // DIRECTION ROWS ONLY. An experiment's tags are its OWN claim about what it touches; unioning a
  // child's would be meaningless here (an experiment owns no children) and, the day it did, would
  // make a card appear to touch everything its siblings do.
  const authored = isRecord(card) && Array.isArray(card.concept_tags) ? card.concept_tags : []
  const inherited = cardIsDirection(card) && isRecord(card) && Array.isArray(card.child_concept_tags)
    ? card.child_concept_tags : []
  const seen = new Set()
  for (const tag of [...authored, ...inherited]) {
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
// OPEN[lattice-placement-explosion] multi-parent expansion enumerates root-to-node PATHS, not
// cards, so a valid public payload of 255 cards (every non-empty subset of eight concepts) emits
// 109,600 placements — sum C(8,k)*k! — and `latticeRollups` scans all of them once per placement
// through `descendantIds`, approaching 12 billion prefix checks and freezing the browser. Bound
// the placements, elect a canonical one, or aggregate on the DAG without materialising paths.
// proof:`present:for (const kid of kids) emit(kid, depth@ui/src/questionLattice.js`
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
// EXPERIMENTS ran on.
//
// Reached through the `parent_card_id` EDGE, not through `child_card_ids`. That field is folded
// onto the Card and `serve/public_cards.py::_FIELDS` deliberately does not publish it, so in the
// browser it is always `undefined` — the loop below ran zero times, `measuredNodes` was 0 for every
// question, and `questionClosure` therefore reported every closed direction as unsupported: the red
// "closed with NOTHING narrower behind it — no experiment of its own produced evidence" chip, drawn
// over a question answered by three measured runs. `mixedComparability` was dead for the same
// reason. The unit tests passed only because their fixtures supplied `child_card_ids` by hand.
//
// `cardLineageIndex` is the shared inversion of that edge and carries two guards a local rebuild
// keeps losing: it refuses a SELF edge, and it treats a card whose parent is off the 256-row wire
// page as a root rather than as a child of an id this page cannot draw.
function cardEvidenceNodes(card, childrenByParent) {
  const out = Array.isArray(card.evidence) ? [...card.evidence] : []
  for (const child of childrenByParent.get(card.id) || []) {
    if (Array.isArray(child.evidence)) out.push(...child.evidence)
  }
  return out
}

export function latticeRollups(state, cards, rows) {
  const nodes = isRecord(state?.nodes) ? state.nodes : {}
  // ONCE per call, not once per row: the inversion is over the whole card set and does not vary
  // with the row being rolled up. `byId` comes from the SAME index rather than being rebuilt five
  // lines up — `cardLineageIndex` already filters with the identical `isRecord(card) && card.id`
  // predicate and returns it, so the local copy was a second full pass and a second Map over the
  // same array — on a call the lattice-placement-explosion note above already flags as hot. (That
  // slug is NOT repeated as a marker here: the open-item index is one declaration per slug, and a
  // second `OPEN[` token in a comment about it is a duplicate declaration, not a cross-reference.)
  const { byId, childrenByParent } = cardLineageIndex(cards)
  const out = new Map()
  for (const row of Array.isArray(rows) ? rows : []) {
    const ids = descendantIds(rows, row.rowKey)
    let best = null
    let bestCardId = null
    const measured = []
    // DEDUPED per row. A node reachable from a question through two paths — its own experiment and
    // that experiment's parent question, both of which are descendants of this row — was pushed
    // once per path, so `measuredNodes` counted paths rather than experiments and
    // `nodesSplitByComparability` (pairwise, O(m^2)) re-compared the same node against itself.
    const seen = new Set()
    for (const id of [row.id, ...ids]) {
      const card = byId.get(id)
      if (!isRecord(card)) continue
      for (const nodeId of cardEvidenceNodes(card, childrenByParent)) {
        if (seen.has(String(nodeId))) continue
        seen.add(String(nodeId))
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
// THE LADDER IS TOTAL AGAIN (2026-08-29), and it took a THIRD bucket to make the claim true.
// Two shapes rendered in neither half. (1) An experiment whose parent is an EXPERIMENT — the
// refinement-of-a-refinement `54dd4c9e` called "perfectly ordinary" when it fixed the same depth>=2
// loss in `directionGroups` — because `ResearchView` reads children only for LATTICE rows, which
// are questions, so a depth-2 card is grouped under its parent and never asked for. That half is
// fixed in the view, which now walks a question's descendants rather than only its immediate kids.
// (2) A card whose parent id is not on this page at all, clipped by the 256-row wire cap: it is not
// unfiled — the run HAS a question for it — so folding it in here would assert the opposite.
// `offPageParentExperiments` is its own counted bucket for exactly that reason, and the caller
// renders it with its own sentence.
// PARENT-BASED, never concept-based: an experiment carrying `loss/contrastive` is still unfiled if
// no question claims it, and grouping it by its tags would seat it in the lattice as though some
// question owned it. The edge is the claim; the tags are a description.
// THE CARDS WHOSE PARENT THIS PAGE DOES NOT HOLD. Not unfiled and not under a question: the edge
// exists and names a card the wire clipped, so the honest statement is "filed under something you
// cannot see here", which is a different sentence from "nobody filed this".
//
// Together with `unfiledExperiments` and the questions' own descendants this covers every
// non-direction card the wire carried — the totality the comment above claims and
// `nestedDirectionNotAChild`'s sibling test now drives.
export function offPageParentExperiments(cards, { order } = {}) {
  const rows = (Array.isArray(cards) ? cards : []).filter(c => isRecord(c) && c.id)
  const known = new Set(rows.map(c => String(c.id)))
  const sort = typeof order === 'function'
    ? order
    : (a, b) => String(a.id).localeCompare(String(b.id))
  return rows
    .filter(card => !cardIsDirection(card))
    .filter(card => {
      const parent = cardParentId(card)
      return !!parent && !known.has(String(parent))
    })
    .sort(sort)
}

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
