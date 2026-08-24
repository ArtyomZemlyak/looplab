// The DIRECTION -> EXPERIMENT forest's pure model. Every property here is one the board got wrong
// before this view existed, or one it would get wrong under an obvious simpler implementation.
//
// The shapes mirror the real wire measured against `runs/e5small-dr-unified-v4` on 2026-08-24:
// 141 cards, 7 of them directions carrying `identity_not_native` + `action_owner_missing` +
// `freshness_unknown` — unbuildable by construction — rendered in the Kanban lanes beside 134 real
// work items. On v5 that ratio was 5 of 5, i.e. a board that read as full with nothing to run.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  CARD_KIND_DIRECTION, CARD_KIND_EXPERIMENT, UNFILED_GROUP_ID,
  cardIsDirection, cardKind, cardLineageIndex, cardLineageView, cardParentId,
  cardLineageViews, directionGroups, rollupChips,
} from '../src/cardLineageModel.js'

const direction = (id, extra = {}) => ({ id, card_kind: 'direction', ...extra })
const experiment = (id, extra = {}) => ({ id, card_kind: 'experiment', ...extra })

test('a wire that predates card_kind renders exactly as it always did', () => {
  // The conservative side is `experiment`, matching `core/cards.py::card_kind_of`: mislabelling
  // work as a direction HIDES it from the work accounting; the reverse only misplaces a question.
  assert.equal(cardKind({ id: 'c' }), CARD_KIND_EXPERIMENT)
  assert.equal(cardKind(null), CARD_KIND_EXPERIMENT)
  assert.equal(cardKind({ id: 'c', card_kind: 'something-invented-later' }), CARD_KIND_EXPERIMENT)
  assert.equal(cardKind(direction('d')), CARD_KIND_DIRECTION)
  assert.equal(cardIsDirection(direction('d')), true)
})

test('a blank or non-string parent is no parent', () => {
  for (const raw of [undefined, null, '', '   ', 7, ['x']]) {
    assert.equal(cardParentId({ id: 'c', parent_card_id: raw }), null)
  }
  assert.equal(cardParentId({ id: 'c', parent_card_id: '  dir-1 ' }), 'dir-1')
})

test('a card whose parent is off the page is a ROOT, never dropped', () => {
  // The wire caps at 256 cards (`PUBLIC_CARD_MAX_COUNT`), so a parent can legitimately be absent.
  // Dropping its children would understate the board — the one thing this view must not do.
  const { roots, childrenByParent } = cardLineageIndex([experiment('a', { parent_card_id: 'gone' })])
  assert.deepEqual(roots.map(r => r.id), ['a'])
  assert.equal(childrenByParent.size, 0)
})

test('a self edge is refused at the boundary', () => {
  // The fold cannot emit one, but a stale cache or a fixture can, and a self edge here is an
  // infinite group rather than a wrong label.
  const { roots, childrenByParent } = cardLineageIndex([experiment('a', { parent_card_id: 'a' })])
  assert.deepEqual(roots.map(r => r.id), ['a'])
  assert.equal(childrenByParent.size, 0)
})

test('an unanswered direction still gets a row', () => {
  // The most actionable row on this view is a question nobody has run an experiment against; an
  // implementation that only emits groups with children hides exactly that.
  const groups = directionGroups([direction('dir-1')])
  assert.deepEqual(groups.map(g => g.id), ['dir-1'])
  assert.equal(groups[0].children.length, 0)
})

test('experiments filed under a direction leave the unfiled bucket', () => {
  const groups = directionGroups([
    direction('dir-1'), experiment('a', { parent_card_id: 'dir-1' }),
    experiment('b', { parent_card_id: 'dir-1' }), experiment('lone'),
  ])
  assert.deepEqual(groups.map(g => g.id), ['dir-1', UNFILED_GROUP_ID])
  assert.deepEqual(groups[0].children.map(c => c.id), ['a', 'b'])
  assert.deepEqual(groups[1].children.map(c => c.id), ['lone'])
})

test('the unfiled bucket is absent when nothing is unfiled', () => {
  const groups = directionGroups([direction('dir-1'), experiment('a', { parent_card_id: 'dir-1' })])
  assert.deepEqual(groups.map(g => g.id), ['dir-1'])
})

test('an experiment with children of its own gets a group rather than losing them', () => {
  // A real shape — one experiment refined into two variants — and the alternative to grouping it
  // is children that appear nowhere at all.
  const groups = directionGroups([
    experiment('base'), experiment('v1', { parent_card_id: 'base' }),
  ])
  assert.deepEqual(groups.map(g => g.id), ['base'])
  assert.deepEqual(groups[0].children.map(c => c.id), ['v1'])
})

test('the order function is the BOARD\'s, never a second opinion', () => {
  const reverse = (a, b) => b.id.localeCompare(a.id)
  const groups = directionGroups([
    direction('dir-1'), experiment('a', { parent_card_id: 'dir-1' }),
    experiment('b', { parent_card_id: 'dir-1' }),
  ], reverse)
  assert.deepEqual(groups[0].children.map(c => c.id), ['b', 'a'])
})

test('a direction wears COUNTS and never a borrowed lane', () => {
  // THE OPERATOR'S OWN REQUIREMENT, stated before this was built: a broad direction must not sit in
  // "Running" for months because one of two hundred experiments under it happens to be training.
  const chips = rollupChips({
    children: 17, open: 2, running: 1, evaluated: 12, failed: 2, dropped: 0,
    best_delta: 0.0041, best_card_id: 'card-12',
  })
  assert.deepEqual(chips.map(c => c.label), [
    '17 experiments', '2 open', '1 running', '12 evaluated', '2 no result', 'best +0.0041 by card-12',
  ])
  assert.equal(chips.some(c => c.key === 'dropped'), false, 'a zero bucket is omitted, not carried forever')
})

test('one experiment is not "1 experiments"', () => {
  assert.equal(rollupChips({ children: 1 })[0].label, '1 experiment')
})

test('no rollup, no chips — and a zero-child rollup is no rollup', () => {
  assert.deepEqual(rollupChips(null), [])
  assert.deepEqual(rollupChips({ children: 0 }), [])
  assert.deepEqual(rollupChips({ children: 'many' }), [])
})

test('a non-finite best never headlines a direction', () => {
  const chips = rollupChips({ children: 1, best_delta: Infinity })
  assert.equal(chips.some(c => c.key === 'best'), false)
})

test('an unresolvable parent is REPORTED, not laundered into "unfiled"', () => {
  // `parentId` set with `parent` null is a different statement from no parent at all, and the panel
  // must be able to say "filed under card-12 (not on this page)" rather than a falsehood.
  const view = cardLineageView([experiment('a', { parent_card_id: 'gone' })], 'a')
  assert.equal(view.parentId, 'gone')
  assert.equal(view.parent, null)
})

test('the inspector view resolves both directions of the edge', () => {
  const cards = [
    direction('dir-1', { child_rollup: { children: 1, running: 1 } }),
    experiment('a', { parent_card_id: 'dir-1' }),
  ]
  const child = cardLineageView(cards, 'a')
  assert.equal(child.kind, CARD_KIND_EXPERIMENT)
  assert.equal(child.parent.id, 'dir-1')
  assert.deepEqual(child.children, [])
  assert.equal(child.rollup, null)
  const parent = cardLineageView(cards, 'dir-1')
  assert.equal(parent.kind, CARD_KIND_DIRECTION)
  assert.equal(parent.parentId, null)
  assert.deepEqual(parent.children.map(c => c.id), ['a'])
  assert.deepEqual(parent.rollup, { children: 1, running: 1 })
})

test('a card that has not arrived yet renders as empty rather than throwing', () => {
  const view = cardLineageView([], 'missing')
  assert.deepEqual(view, { card: null, kind: null, parent: null, parentId: null, children: [], rollup: null })
})

test('every card\'s view from ONE walk agrees with the per-card one', () => {
  // The board needs all of them at once and the per-card helper rebuilds the index each call, which
  // is O(cards^2) at the wire's 256-row cap. Same answers is the property; the speed is the point.
  const cards = [
    direction('dir-1', { child_rollup: { children: 2, running: 1 } }),
    experiment('a', { parent_card_id: 'dir-1' }),
    experiment('b', { parent_card_id: 'dir-1' }),
    experiment('lone'), experiment('orphan', { parent_card_id: 'gone' }),
  ]
  const bulk = cardLineageViews(cards)
  assert.deepEqual([...bulk.keys()].sort(), ['a', 'b', 'dir-1', 'lone', 'orphan'])
  for (const card of cards) {
    assert.deepEqual(bulk.get(card.id), cardLineageView(cards, card.id), card.id)
  }
})
