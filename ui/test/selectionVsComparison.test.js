// SELECTION IS NOT COMPARISON.
//
// The run list capped SELECTION at 8, because comparison was the only thing it fed. An operator who
// wanted to pick twenty runs — to compare a few of them, or to act on all of them — was refused at
// the ninth checkbox with "Compare is limited to 8 runs". The limit is real; it belongs to the
// comparison, not to the act of choosing.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  COMPARE_MAX, SELECTION_MAX, comparisonScope, selectionNotice,
} from '../src/runIndex.js'

const runs = n => Array.from({ length: n }, (_, i) => ({ run_id: `r${i}` }))

test('selection is not bounded by the comparison limit', () => {
  assert.ok(SELECTION_MAX > COMPARE_MAX * 10,
    'the selection ceiling exists for the saved view and the URL, not for the compare table')
})

test('a comparison draws the first COMPARE_MAX and reports the rest as still selected', () => {
  const scope = comparisonScope(runs(20))
  assert.equal(scope.shown.length, COMPARE_MAX)
  assert.equal(scope.omitted, 20 - COMPARE_MAX)
  assert.equal(scope.total, 20)
  // The number the operator reads must say the rest are KEPT, not dropped — silently truncating is
  // worse than the refusal it replaces.
  const notice = selectionNotice(scope)
  assert.match(notice, /first 8 of 20/)
  assert.match(notice, /12 more stay selected/)
})

test('a selection inside the limit compares whole and says nothing about omissions', () => {
  const scope = comparisonScope(runs(3))
  assert.equal(scope.shown.length, 3)
  assert.equal(scope.omitted, 0)
  assert.equal(selectionNotice(scope), 'Ready to compare run details.')
})

test('one run is a selection, not yet a comparison', () => {
  assert.equal(selectionNotice(comparisonScope(runs(1))), 'Select one more run to compare.')
  assert.equal(selectionNotice(comparisonScope([])), '')
  assert.equal(selectionNotice(null), '')
})

test('junk input yields an empty scope rather than throwing inside a render', () => {
  assert.deepEqual(comparisonScope(null), { shown: [], omitted: 0, total: 0 })
  assert.deepEqual(comparisonScope(undefined, 0), { shown: [], omitted: 0, total: 0 })
  assert.equal(comparisonScope(runs(5), -1).shown.length, 0)
})

test('the list no longer refuses the ninth selection, and bounds only the compare view', () => {
  const source = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'RunList.jsx'), 'utf8')
  assert.ok(!source.includes('Compare is limited to 8 runs'),
    'the selection-time refusal came back')
  assert.ok(!/next\.size >= 8/.test(source), 'the hard-coded selection cap came back')
  assert.ok(source.includes('next.size >= SELECTION_MAX'))
  // The compare surface is what gets the bounded list.
  assert.ok(source.includes('<RunCompare runs={comparisonScope(compareRuns).shown}'),
    'the comparison must draw the bounded scope, not the whole selection')
})
