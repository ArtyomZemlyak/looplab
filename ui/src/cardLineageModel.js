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
  return { byId, childrenByParent, roots }
}

// The groups the Directions view draws, in a stable order: every direction (even an empty one — an
// unanswered question is the most actionable row on this view, not the least), then the unfiled
// experiments. `order` sorts within a group and is the board's own `cardOrder`, passed in so this
// module does not acquire a second opinion about priority.
export function directionGroups(cards, order) {
  const { childrenByParent, roots } = cardLineageIndex(cards)
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
  const grouped = new Set(groups.flatMap(g => [g.id, ...g.children.map(c => c.id)]))
  const unfiled = sorted(roots.filter(row => !cardIsDirection(row) && !grouped.has(row.id)))
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
  return chips
}

// What the Inspector shows on ONE card: who it answers to, who answers to it, and — for a
// direction — its own rollup. Returns nulls rather than throwing so the panel can render a card
// that arrived before its neighbours did.
export function cardLineageView(cards, cardId) {
  const { byId, childrenByParent } = cardLineageIndex(cards)
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
