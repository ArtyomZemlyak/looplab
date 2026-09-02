// Pure decisions for the DIRECTION -> EXPERIMENT forest — no React, no I/O, so `node --test` can
// drive them directly (the `ui/` house pattern: a pure model beside its React half).
//
// WHAT THIS VIEW EXISTS FOR. `cardBoardModel.js` next door states the relation the board had to
// learn first — a Card is not a node, it is the work item and its nodes are the attempts. This is
// the level ABOVE that one, and the board was missing it in a way that made the board itself lie:
// a research DIRECTION ("cross-distillation from a stronger teacher") is not a minimal-change
// hypothesis and can never be made into one, so the engine writes it as a card that owns no
// executable action — `identity_not_native`, `action_owner_missing` — and the Kanban rendered it in
// the lanes beside real work. Measured on `runs/e5small-dr-unified-v5`: 5 of 5 rows were
// directions, none was buildable, and the board read as FULL while the engine had nothing to run.
//
//     Concept (a slash path, hierarchical)
//       └── Direction (a Card owning no action)         <- this file
//             └── Experiment (a Card owning one action) <- `cardBoardModel.js`
//                   └── Node (an attempt)               <- `cardBoardModel.js::cardAttempts`
//
// THE ONE RULE THAT DECIDES THE WHOLE RENDER: a direction NEVER borrows a lifecycle lane from its
// children. The operator named the failure before this was built — a broad direction sitting in
// "Running" for months because one of two hundred experiments under it happens to be training — and
// a lane is a statement about ONE piece of work. A direction gets COUNTS instead, which is also the
// only form that stays readable as the family grows.

import { isRecord } from './panelPrimitives.js'

export const CARD_KIND_DIRECTION = 'direction'
export const CARD_KIND_EXPERIMENT = 'experiment'

// The bucket that holds every experiment nobody filed. It is deliberately ALWAYS rendered when it
// is non-empty and never merged into a direction: "not filed under any direction" is a fact about
// the board an operator acts on, and hiding it behind a total would make the view claim a coverage
// the run does not have. Mirrors `conceptForest.js`'s always-present Untagged bucket.
export const UNFILED_GROUP_ID = '__unfiled__'

// Absent/unknown reads as `experiment`, matching `core/cards.py::card_kind_of`'s own conservative
// side: mislabelling work as a direction HIDES it from the work accounting, while the reverse only
// draws a question in the wrong place. A wire that predates `card_kind` therefore renders exactly
// as it always did.
export function cardKind(card) {
  return isRecord(card) && card.card_kind === CARD_KIND_DIRECTION
    ? CARD_KIND_DIRECTION : CARD_KIND_EXPERIMENT
}

export const cardIsDirection = card => cardKind(card) === CARD_KIND_DIRECTION

// THE EXPERIMENTS EACH QUESTION OWNS, keyed by the parent's id. Hoisted out of `ResearchView` in
// 2026-08-29 so the rule is reachable by a test: while it lived inline, a test could only replicate
// the loop, and a replica keeps passing when the original is inverted — which is exactly what a
// mutation run showed before this move.
//
// A DIRECTION IS NEVER SOMEBODY'S EXPERIMENT. `parent_card_id` is set by the fold for any card
// without consulting its kind (`events/card_ledger.py`), so a nested QUESTION arrived here and was
// counted, labelled and drawn below its parent as an experiment — while the same card also stood in
// the lattice as a question. One card, two contradictory readings, and the experiment one is false:
// a direction owns no action and has no result to roll up. A nested question keeps its lattice
// position, which is the surface that can say what the nesting MEANS.
//
// UNREACHED ON THIS BOX AND SAID PLAINLY: 0 of 218 preserved `card_added` rows carry a direction
// with a parent. The fold permits it with no guard, so this is a cheap honesty rule on a reachable
// shape, not a recovery of anything that has happened.
export function childrenByParent(cards) {
  const out = new Map()
  for (const card of Array.isArray(cards) ? cards : []) {
    if (cardIsDirection(card)) continue
    const parent = cardParentId(card)
    if (!parent) continue
    if (!out.has(parent)) out.set(parent, [])
    out.get(parent).push(card)
  }
  return out
}
// EVERY experiment under a question, not only its immediate ones. `ResearchView` draws children for
// LATTICE rows, which are questions — so a refinement-of-a-refinement (question -> exp -> exp) was
// grouped under its experiment parent and then never asked for, rendering in no section at all.
// `54dd4c9e` fixed the identical depth>=2 loss in `directionGroups` one day before `9440cff5`
// retired the only total view, which is how the ladder inherited it.
//
// Breadth-first from the question's own children, with a VISITED set: `parent_card_id` is a
// free-form edge the fold does not check for cycles, and a self- or mutual-parent pair would
// otherwise hang the render. Depth is not capped — a cap would silently drop the deepest card,
// which is the defect this walk exists to end — and the visited set already bounds the work to the
// card count.
export function descendantsOf(parentId, byParent) {
  const out = []
  if (!parentId || !(byParent instanceof Map)) return out
  const seen = new Set([String(parentId)])
  const queue = [String(parentId)]
  while (queue.length) {
    for (const child of byParent.get(queue.shift()) || []) {
      const id = String(child?.id ?? '')
      if (!id || seen.has(id)) continue
      seen.add(id)
      out.push(child)
      queue.push(id)
    }
  }
  return out
}


// The parent id as the wire actually carries it. `Card.child_card_ids` is deliberately NOT on the
// wire (`serve/public_cards.py` says why), so the inverse edge is rebuilt HERE from the same card
// map the board already holds — exactly as `cardBoardModel.js::nodeCardId` rebuilds the node join.
export function cardParentId(card) {
  const raw = isRecord(card) ? card.parent_card_id : null
  return typeof raw === 'string' && raw.trim() ? raw.trim() : null
}

// parent id -> child rows, and the roots. Cards whose parent is not in the visible set are treated
// as ROOTS rather than dropped: the wire caps at 256 cards, so a parent can legitimately be off the
// page, and silently deleting its children would understate the board.
export function cardLineageIndex(cards) {
  const rows = Array.isArray(cards) ? cards.filter(card => isRecord(card) && card.id) : []
  const byId = new Map(rows.map(card => [card.id, card]))
  const childrenByParent = new Map()
  const roots = []
  for (const card of rows) {
    const parentId = cardParentId(card)
    // A self edge cannot survive the fold, but the fold is not the only thing that can hand this
    // function a card — a stale cache, a hand-built fixture — and a self edge here is an infinite
    // group. Refuse it at the boundary rather than trusting the producer.
    if (parentId && parentId !== card.id && byId.has(parentId)) {
      if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, [])
      childrenByParent.get(parentId).push(card)
    } else {
      roots.push(card)
    }
  }
  return { rows, byId, childrenByParent, roots }
}

// The groups the Directions view draws, in a stable order: every direction (even an empty one — an
// unanswered question is the most actionable row on this view, not the least), then the unfiled
// experiments. `order` sorts within a group and is the board's own `cardOrder`, passed in so this
// module does not acquire a second opinion about priority.
export function directionGroups(cards, order) {
  const { rows, childrenByParent, roots } = cardLineageIndex(cards)
  const sort = typeof order === 'function' ? order : undefined
  const sorted = rows => (sort ? [...rows].sort(sort) : rows)
  const groups = []
  const directions = roots.filter(cardIsDirection)
  for (const direction of sorted(directions)) {
    groups.push({
      id: direction.id,
      direction,
      children: sorted(childrenByParent.get(direction.id) || []),
    })
  }
  // An EXPERIMENT with children of its own is a real shape (an experiment refined into two
  // variants) and it is rendered as its own group rather than being flattened away, because the
  // alternative is children that appear nowhere.
  for (const parent of sorted(roots.filter(row => !cardIsDirection(row)))) {
    const children = childrenByParent.get(parent.id) || []
    if (children.length) groups.push({ id: parent.id, direction: parent, children: sorted(children) })
  }
  // ANY card that is still in no group gets one, and this is the bug the first cut shipped: groups
  // were built from ROOTS only, so a card at depth >= 2 — `dir-1 -> exp-1 -> exp-2`, a perfectly
  // ordinary refinement of a refinement — rendered NOWHERE. Not under its parent, not under the
  // root, not even in the Unfiled bucket. The forest is bounded at `CARD_LINEAGE_MAX_DEPTH`, not at
  // one, so the view has to be total over whatever the fold publishes.
  //
  // A deep card becomes its OWN group headed by its parent rather than being flattened into the
  // root's: "these experiments answer that experiment" is the true statement, and hoisting them to
  // the root would claim they answer a question they are two steps away from.
  const heads = new Set(groups.map(g => g.id))
  const claimed = new Set(groups.flatMap(g => [g.id, ...g.children.map(c => c.id)]))
  for (const card of rows) {
    const children = childrenByParent.get(card.id) || []
    // A card that HAS children heads a group, whether or not it is itself somebody's child. The
    // `claimed` set may not gate this: `exp-1` is a child of `dir-1` AND the parent of `exp-2`, and
    // treating "already appears somewhere" as "already handled" is what dropped `exp-2` into the
    // Unfiled bucket — filed under nothing, when it is filed under a card on the same screen.
    if (!children.length || heads.has(card.id)) continue
    groups.push({ id: card.id, direction: card, children: sorted(children) })
    heads.add(card.id)
    claimed.add(card.id)
    for (const child of children) claimed.add(child.id)
  }
  // Whatever is STILL unclaimed is genuinely filed under nothing this page can show.
  const unfiled = sorted(rows.filter(row => !claimed.has(row.id)))
  if (unfiled.length) {
    groups.push({ id: UNFILED_GROUP_ID, direction: null, children: unfiled })
  }
  return groups
}

// The chips a direction row wears INSTEAD of a status lane. Zero buckets are omitted — a direction
// with twelve evaluated children should not carry `0 no-result` forever — and the total is always
// first because it is the one number that is exact even when the engine clipped the id list.
// Mirrors `core/cards.py::card_rollup_brief`; the two are separate because one renders into a
// prompt and one into DOM, and neither may invent a bucket the other does not have.
export const ROLLUP_CHIPS = [
  ['open', 'open'], ['running', 'running'], ['evaluated', 'evaluated'],
  ['failed', 'no result'], ['dropped', 'dropped'],
]

export function rollupChips(rollup) {
  if (!isRecord(rollup)) return []
  const total = rollup.children
  if (!Number.isSafeInteger(total) || total <= 0) return []
  const chips = [{ key: 'children', label: `${total} experiment${total === 1 ? '' : 's'}` }]
  for (const [key, label] of ROLLUP_CHIPS) {
    const count = rollup[key]
    if (Number.isSafeInteger(count) && count > 0) chips.push({ key, label: `${count} ${label}` })
  }
  const best = rollup.best_delta
  if (typeof best === 'number' && Number.isFinite(best)) {
    const owner = typeof rollup.best_card_id === 'string' && rollup.best_card_id
      ? ` by ${rollup.best_card_id}` : ''
    chips.push({ key: 'best', label: `best ${best > 0 ? '+' : ''}${best}${owner}` })
  }
  // The champion-relative verdict, mirrored from `card_rollup_brief`'s own second number — a
  // DIFFERENT baseline from `best_delta` (the run champion, not each child's parent), and the one
  // that answers a direction answered by DRAFTS, whose children have no parent to delta against.
  // Omitting it here while the Python half and the wire carried it left the operator's board
  // showing an answered question as unmeasured — the exact defect the pair was added to end.
  const anchor = rollup.best_vs_champion
  if (typeof anchor === 'number' && Number.isFinite(anchor)) {
    const owner = typeof rollup.best_vs_champion_card_id === 'string' && rollup.best_vs_champion_card_id
      ? ` by ${rollup.best_vs_champion_card_id}` : ''
    chips.push({ key: 'champion', label: `best vs champion ${anchor > 0 ? '+' : ''}${anchor}${owner}` })
  }
  return chips
}

// What the Inspector shows on ONE card: who it answers to, who answers to it, and — for a
// direction — its own rollup. Returns nulls rather than throwing so the panel can render a card
// that arrived before its neighbours did.
export function cardLineageView(cards, cardId) {
  return viewFromIndex(cardLineageIndex(cards), cardId)
}

// Every card's view from ONE walk of the edges. The board needs all of them at once, and calling
// `cardLineageView` per card rebuilds the index each time — O(cards^2) on a board the wire already
// lets reach 256 rows (`PUBLIC_CARD_MAX_COUNT`). Same answers, one pass.
export function cardLineageViews(cards) {
  const index = cardLineageIndex(cards)
  return new Map([...index.byId.keys()].map(id => [id, viewFromIndex(index, id)]))
}

function viewFromIndex({ byId, childrenByParent }, cardId) {
  const card = byId.get(cardId) || null
  if (!card) return { card: null, kind: null, parent: null, parentId: null, children: [], rollup: null }
  const parentId = cardParentId(card)
  return {
    card,
    kind: cardKind(card),
    parentId,
    // A parent id we cannot resolve is REPORTED, not hidden: `parentId` stays set while `parent` is
    // null, so the panel can say "filed under card-12 (not on this page)" instead of "unfiled",
    // which is a different and false statement.
    parent: parentId ? byId.get(parentId) || null : null,
    children: childrenByParent.get(card.id) || [],
    rollup: isRecord(card.child_rollup) ? card.child_rollup : null,
  }
}

// How far the experiment that RAN is from the one the card proposed — `null` when there is nothing
// to say. MIRRORS `core/cards.py::card_proposal_drift` and is deliberately a second implementation
// rather than a wire field: the two maps are already on the wire, the rule is four lines, and a
// derived scalar would be one more thing to keep in sync across a version skew.
//
// The rules that matter are the python's and are repeated because getting either wrong is a
// confident falsehood about somebody's experiment: only the coordinates BOTH sides name are
// compared (a knob the carrier never answered is not evidence of a move, so `moved` can never
// exceed `compared`), and `null` for "nothing was comparable" is a different answer from
// `moved: 0` for "they agree".
export function cardProposalDrift(card) {
  if (!isRecord(card)) return null
  const proposed = isRecord(card.params) ? card.params : null
  const applied = isRecord(card.applied_params) ? card.applied_params : null
  if (!proposed || !applied) return null
  const shared = Object.keys(proposed).filter(name => Object.hasOwn(applied, name)).sort()
  if (!shared.length) return null
  const params = shared.filter(name => proposed[name] !== applied[name])
  if (!params.length) return null          // agreement renders nothing; the loud case stays loud
  return { compared: shared.length, moved: params.length, params }
}

// THE KANBAN'S POPULATION, split from the board's. The lanes answer "what is the machine doing now"
// — a lifecycle question — and a QUESTION has no lifecycle of its own: it owns no action, so it can
// never be building, running or evaluated on its own account, and every selection blocker drawn on
// it is about work it was never going to do (the R7 finding, one view over).
//
// Measured before this shipped, on `runs/e5small-dr-unified-v5`: the opening board was 5 directions
// and 1 experiment, so the Kanban read as SIX work items with one buildable — five of the six rows
// describing something the engine would never dispatch.
//
// IT IS A SPLIT, NOT A DROP, and the difference is the whole safety of it: the questions come back
// as the second half of the pair so the board can SAY how many it moved and where they went. A
// filter that silently shrank the lanes would turn "five questions are waiting for an experiment"
// into "the board is empty", which is the more expensive misreading of the two.
export function splitBoardByKind(cards) {
  const rows = Array.isArray(cards) ? cards : []
  const work = []
  const questions = []
  for (const card of rows) {
    if (cardIsDirection(card)) questions.push(card)
    else work.push(card)
  }
  return { work, questions }
}
