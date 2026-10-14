import assert from 'node:assert/strict'
import test from 'node:test'
import { findingsDelta, normalizeResearchMemo } from '../src/researchMemoModel.js'

// Review 2026-10-14 §9 / doc 76 §3: three memos — a repeat, a refinement and a refutation — must not
// inflate the current summary, and what changed must be visible.
test('author statuses win; exact repeats are unchanged; vanished findings are listed', () => {
  const first = normalizeResearchMemo({ findings: ['A holds.', 'B holds.', 'C holds.'],
    finding_meta: [{ id: 'a' }, { id: 'b' }, { id: 'c' }] })
  const second = normalizeResearchMemo({
    findings: ['A holds.', 'B holds only for new users.', 'C is false.'],
    finding_meta: [{ id: 'a', status: 'carried' }, { id: 'b2', status: 'refined', supersedes: 'b' },
      { id: 'c', status: 'refuted' }] })
  const delta = findingsDelta(second, first)
  assert.deepEqual(delta.counts, { new: 0, refined: 1, refuted: 1, carried: 1 })
  assert.deepEqual(delta.dropped, [])
  const third = normalizeResearchMemo({ findings: ['A holds.', 'D is new.'] })
  const next = findingsDelta(third, second)
  assert.deepEqual(next.items.map(item => item.status), ['carried', 'new'])
  assert.deepEqual(next.dropped, ['B holds only for new users.', 'C is false.'])
  assert.equal(findingsDelta(first, null).comparable, false)
  assert.equal(findingsDelta(first, null).items[0].status, null)
})

test('a reaffirming memo is labelled and legacy memos carry no metadata', () => {
  const memo = normalizeResearchMemo({ findings: ['x'], reaffirms: 'memo-' + 'a'.repeat(64) })
  assert.equal(findingsDelta(memo, null).reaffirms, 'memo-' + 'a'.repeat(64))
  assert.deepEqual(normalizeResearchMemo({ findings: ['x'] }).finding_meta, [])
  assert.deepEqual(normalizeResearchMemo({ findings: ['x'], finding_meta: [{ status: 'bogus', id: '../x' }] })
    .finding_meta, [{}])
})
