// The DIRECTION -> EXPERIMENT forest's pure model. Every property here is one the board got wrong
// before this view existed, or one it would get wrong under an obvious simpler implementation.
//
// The shapes mirror the real wire measured against `runs/e5small-dr-unified-v4` on 2026-08-24:
// 141 cards, 7 of them directions carrying `identity_not_native` + `action_owner_missing` +
// `freshness_unknown` — unbuildable by construction — rendered in the Kanban lanes beside 134 real
// work items. On v5 that ratio was 5 of 5, i.e. a board that read as full with nothing to run.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import {
  CARD_KIND_DIRECTION, CARD_KIND_EXPERIMENT, UNFILED_GROUP_ID,
  cardIsDirection, cardKind, cardLineageIndex, cardLineageView, cardParentId,
  cardLineageViews, cardProposalDrift, directionGroups, rollupChips, splitBoardByKind,
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

test('the browser drift mirrors the python: only shared coordinates, silent on agreement', () => {
  // The pane leads with the value that RAN and keeps the proposal in brackets, so getting this
  // wrong is a confident falsehood about somebody's experiment. Measured on the live run: card-0
  // declared SIXTEEN coordinates and the carrier answered TWELVE — comparing the union would have
  // reported four "moved" knobs where nothing moved at all.
  assert.equal(cardProposalDrift({ params: { a: 1 }, applied_params: { a: 1 } }), null,
    'agreement renders nothing')
  assert.equal(cardProposalDrift({ params: { a: 1 } }), null, 'no applied record')
  assert.equal(cardProposalDrift({ params: { a: 1 }, applied_params: { z: 1 } }), null,
    'no shared coordinate')
  assert.deepEqual(
    cardProposalDrift({ params: { a: 1, b: 2, unread: 9 }, applied_params: { a: 1, b: 3 } }),
    { compared: 2, moved: 1, params: ['b'] })
})

test('a direction row is not judged by work-item gates', () => {
  // `identity_not_native` / `action_owner_missing` / `freshness_unknown` answer "why will the Card
  // queue not pick this up next". A direction owns no executable action BY DESIGN, so on its row
  // those three are its definition restated as alarms — every direction on the live v5 wore all
  // three, which reads as breakage on a row that is working exactly as intended.
  const source = readFileSync(new URL('../src/CardBoard.jsx', import.meta.url), 'utf8')
  const gate = source.split("className=\"card-kanban-k\">Gate")[0].slice(-260)
  assert.ok(gate.includes('!isDirection'), 'the Gate row is suppressed on a direction')
  const blockers = source.split('aria-label="Selection blockers"')[0].slice(-200)
  assert.ok(blockers.includes('!isDirection'), 'the blocker chips are suppressed on a direction')
  assert.ok(source.includes('not runnable by design'),
    'and the row says what IS true of a direction instead of leaving it blank')
  assert.ok(source.includes('no experiment filed under it yet'),
    'an unanswered direction states what it needs, which is the actionable half')
})

// The Kanban's population, split from the board's (P2). Measured on `runs/e5small-dr-unified-v5`:
// the opening board was 5 directions and 1 experiment, so the lanes read as SIX work items with one
// buildable — five rows describing work the engine would never dispatch.
test('the lanes take work items only, and the questions come back rather than vanishing', () => {
  const rows = [direction('card-0'), experiment('card-1'), direction('card-2')]
  const { work, questions } = splitBoardByKind(rows)
  assert.deepEqual(work.map(c => c.id), ['card-1'])
  assert.deepEqual(questions.map(c => c.id), ['card-0', 'card-2'])
  // A SPLIT and not a drop: nothing may be lost between the two halves, or the board's own total
  // stops reconciling with what it draws.
  assert.equal(work.length + questions.length, rows.length)
})

test('a wire that predates card_kind puts every row in the LANES, unchanged', () => {
  // `cardKind` reads an experiment out of an absent/None provenance on purpose (mislabelling work as
  // a question HIDES it), so an old log's board renders exactly as it always did.
  const legacy = [{ id: 'a' }, { id: 'b', selection_provenance: { action_source: 'propose' } }]
  const { work, questions } = splitBoardByKind(legacy)
  assert.deepEqual(work.map(c => c.id), ['a', 'b'])
  assert.deepEqual(questions, [])
})

test('splitBoardByKind is total over junk', () => {
  assert.deepEqual(splitBoardByKind(null), { work: [], questions: [] })
})

test('the champion-relative verdict is a chip, mirroring card_rollup_brief', () => {
  // The Python half and the wire (`serve/public_cards.py`) carry the pair; a mirror that omits the
  // bucket shows an answered question as unmeasured on the operator's board — the defect the pair
  // was added to end. Same finite discipline as `best`: NaN/inf/absent produce no chip, never a 0.
  const chips = rollupChips({ children: 2, evaluated: 2,
    best_vs_champion: -0.0027, best_vs_champion_card_id: 'card-0' })
  const champion = chips.find(c => c.key === 'champion')
  assert.ok(champion, JSON.stringify(chips))
  assert.equal(champion.label, 'best vs champion -0.0027 by card-0')
  const positive = rollupChips({ children: 1, best_vs_champion: 0.004 })
    .find(c => c.key === 'champion')
  assert.equal(positive.label, 'best vs champion +0.004')
  for (const bad of [undefined, null, NaN, Infinity, '0.1']) {
    assert.ok(!rollupChips({ children: 1, best_vs_champion: bad })
      .some(c => c.key === 'champion'), String(bad))
  }
})
