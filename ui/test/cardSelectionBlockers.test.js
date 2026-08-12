// "It says `blocked` on the card — I thought that was some kind of status. But it means it can't be
// moved. There needs to be a clearer visual pattern."
//
// It does not mean it cannot be moved either: `selection_ready === false` means the Card queue will
// not pick this card up next. For most values of that it is simply what a card looks like AFTER its
// experiment ran. Measured on the live board (rubertlite-dr-unified-v5, four cards):
//
//   card-0  blockers ['work_terminal']    — its experiment finished
//   card-1  blockers ['work_terminal']    — its experiment finished
//   card-3  blockers ['work_in_flight']   — it is training right now
//
// Not one of those is a problem, and all three were painted the same amber as a card whose
// ownership receipt is ambiguous — which IS a problem. The reason lived only in a `title`.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { cardSelectionBlock } from '../src/cardBoardModel.js'

const card = (extra = {}) => ({ id: 'card-0', selection_ready: false, ...extra })

test('a selectable card gets no chip at all', () => {
  assert.equal(cardSelectionBlock({ selection_ready: true, selection_blockers: [] }), null)
  assert.equal(cardSelectionBlock(null), null)
  assert.equal(cardSelectionBlock('card-0'), null)
})

test('the three real blockers on the live board read as lifecycle, not alarm', () => {
  const finished = cardSelectionBlock(card({ selection_blockers: ['work_terminal'] }))
  assert.equal(finished.tone, 'lifecycle')
  assert.equal(finished.label, 'its experiment has finished')

  const running = cardSelectionBlock(card({ selection_blockers: ['work_in_flight'] }))
  assert.equal(running.tone, 'lifecycle')
  assert.equal(running.label, 'an experiment is running')
})

test('a ledger fault still looks like one', () => {
  // This is the case the amber chip was RIGHT for, and the reason the two tones exist.
  for (const [blocker, label] of [
    ['action_owner_ambiguous', 'two ownership receipts'],
    ['action_receipt_incomplete', 'incomplete ownership receipt'],
    ['freshness_unknown', 'provenance could not be read'],
  ]) {
    const result = cardSelectionBlock(card({ selection_blockers: [blocker] }))
    assert.equal(result.tone, 'fault', blocker)
    assert.equal(result.label, label)
  }
})

test('a fault wins over a lifecycle reason on the same card', () => {
  // A defect is not made less real by the card also being finished.
  const both = cardSelectionBlock(
    card({ selection_blockers: ['work_terminal', 'action_owner_ambiguous'] }))
  assert.equal(both.tone, 'fault')
  assert.equal(both.label, 'two ownership receipts')
})

test('the full reason list is always in the tooltip, whatever the chip says', () => {
  const result = cardSelectionBlock(
    card({ selection_blockers: ['work_terminal', 'merged_work_items'] }))
  assert.match(result.title, /work_terminal/)
  assert.match(result.title, /merged_work_items/)
})

test('a blocker this build does not know is a FAULT, not a shrug', () => {
  // A card the queue refuses for a reason nothing can name is exactly what an operator must see.
  const unknown = cardSelectionBlock(card({ selection_blockers: ['some_future_blocker'] }))
  assert.equal(unknown.tone, 'fault')
  assert.match(unknown.title, /some_future_blocker/)
})

test('not selectable with no recorded reason says so rather than implying one', () => {
  const none = cardSelectionBlock(card({ selection_blockers: [] }))
  assert.equal(none.tone, 'fault')
  assert.match(none.title, /recorded no reason/)
})

test('every blocker the engine can emit is mapped', () => {
  // Derived from the ENGINE's own list, so a blocker added there and forgotten here is a red test
  // rather than a card that silently reads "not selectable" forever.
  const ledger = readFileSync(
    new URL('../../looplab/events/card_ledger.py', import.meta.url), 'utf8')
  const start = ledger.indexOf('blockers: list[str] = []')
  assert.ok(start > 0, 'the blocker computation moved; re-point this test')
  const block = ledger.slice(start, ledger.indexOf('c.selection_blockers = blockers'))
  const emitted = [...block.matchAll(/blockers\.append\("([a-z_]+)"\)/g)].map(m => m[1])
  assert.ok(emitted.length >= 8, `only found ${emitted.length} blockers; re-derive this test`)

  const model = readFileSync(new URL('../src/cardBoardModel.js', import.meta.url), 'utf8')
  for (const name of emitted) {
    assert.ok(model.includes(`${name}:`), `blocker ${name} has no operator-facing wording`)
  }
})
