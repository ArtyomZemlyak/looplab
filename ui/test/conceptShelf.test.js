// The concept SHELF's display model: the concept tree as a navigational axis over cross-run knowledge,
// and the disclosures that keep it honest. The rule under test throughout: an untagged row is never
// silently dropped, because "no results" reads as "nothing was learned" when it means "nothing was tagged".
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  coverageSummary, filterRows, groupByConceptTree, rowConcepts, rowSource, shelfConcepts,
  SOURCE_RECORD, SOURCE_RUN, UNTAGGED,
} from '../src/conceptShelf.js'

test('row concepts are canonicalized, de-duplicated and sorted', () => {
  assert.deepEqual(rowConcepts({ concepts: ['Loss/Contrastive', 'loss/contrastive', 'A B/c'] }),
    ['a-b/c', 'loss/contrastive'])
  assert.deepEqual(rowConcepts({ concepts: [7, null, {}] }), [])
  assert.deepEqual(rowConcepts({}), [])
})

test('an unrecognised attribution source is not rendered as one', () => {
  assert.equal(rowSource({ concept_source: SOURCE_RECORD }), SOURCE_RECORD)
  assert.equal(rowSource({ concept_source: SOURCE_RUN }), SOURCE_RUN)
  assert.equal(rowSource({ concept_source: 'guessed' }), null)
})

test('selecting a concept matches its whole SUBTREE, not just an exact id', () => {
  // Without this the hierarchy is decoration: picking `loss` would miss every real, deeper tag.
  const row = { concepts: ['loss/contrastive/in-batch'] }
  assert.equal(filterRows([row], 'loss').shown.length, 1)
  assert.equal(filterRows([row], 'loss/contrastive').shown.length, 1)
  assert.equal(filterRows([row], 'loss/contrastive/in-batch').shown.length, 1)
})

test('a sibling prefix is not a subtree match', () => {
  assert.equal(filterRows([{ concepts: ['lossless/codec'] }], 'loss').shown.length, 0)
})

test('a filter reports how many rows it hid FOR LACK OF A TAG', () => {
  // The disclosure the panel renders. Hiding an untagged row is defensible; hiding it silently is not.
  const rows = [
    { concepts: ['loss/contrastive'] },
    { concepts: ['data/augmentation'] },
    {},
    { concepts: [] },
  ]
  const result = filterRows(rows, 'loss')
  assert.equal(result.shown.length, 1)
  assert.equal(result.hidden, 3)
  assert.equal(result.hiddenUntagged, 2)
})

test('Untagged is selectable in its own right', () => {
  const rows = [{ concepts: ['a/b'] }, {}]
  const result = filterRows(rows, UNTAGGED)
  assert.equal(result.shown.length, 1)
  assert.deepEqual(rowConcepts(result.shown[0]), [])
})

test('an empty selection shows everything', () => {
  const rows = [{ concepts: ['a/b'] }, {}]
  assert.equal(filterRows(rows, '').shown.length, 2)
})

test('concept options carry ancestor SUBTREE counts so a root is a usable entry point', () => {
  const options = shelfConcepts({
    lessons: [{ concepts: ['loss/contrastive/in-batch'] }, { concepts: ['loss/margin'] }],
    notes: [{ concepts: ['loss/contrastive/dcl'] }],
  })
  const byId = Object.fromEntries(options.map(o => [o.id, o]))
  assert.equal(byId['loss'].direct, 0, 'nobody tagged the bare root')
  assert.equal(byId['loss'].subtree, 3, 'but three rows live under it')
  assert.equal(byId['loss/contrastive'].subtree, 2)
  assert.equal(byId['loss/contrastive/in-batch'].direct, 1)
  // sorted by id, so a refreshing count never moves a click target
  assert.deepEqual(options.map(o => o.id), [...options.map(o => o.id)].sort())
})

test('the tree display mode always ends with an Untagged group, even when empty', () => {
  // Its ABSENCE would read as "everything is tagged"; a zero count is the signal that the axis is complete.
  const complete = groupByConceptTree([{ concepts: ['a/b'] }])
  const last = complete[complete.length - 1]
  assert.equal(last.id, UNTAGGED)
  assert.equal(last.untagged, true)
  assert.equal(last.rows.length, 0)
})

test('a multi-concept row is filed under each of its concepts', () => {
  // De-duplicating to one "primary" concept would invent a ranking the data does not carry.
  const groups = groupByConceptTree([{ concepts: ['a/b', 'c/d'] }])
  assert.deepEqual(groups.filter(g => !g.untagged).map(g => g.id), ['a/b', 'c/d'])
  assert.equal(groups[groups.length - 1].rows.length, 0)
})

test('untagged rows land in the Untagged group rather than vanishing', () => {
  const groups = groupByConceptTree([{ concepts: ['a/b'] }, {}, { concepts: [] }])
  assert.equal(groups[groups.length - 1].rows.length, 2)
})

test('coverage summary flags an all-inherited shelf as coarse', () => {
  const coarse = coverageSummary({ total: 10, tagged: 4, by_source: { record: 0, run: 4 } })
  assert.equal(coarse.untagged, 6)
  assert.equal(coarse.coarse, true, 'run-level attribution is only as precise as a whole run')
  assert.equal(coarse.complete, false)
  const mixed = coverageSummary({ total: 4, tagged: 4, by_source: { record: 3, run: 1 } })
  assert.equal(mixed.coarse, false)
  assert.equal(mixed.complete, true)
})

test('coverage summary is null only when there is nothing to describe', () => {
  assert.equal(coverageSummary({ total: 0, tagged: 0 }), null)
  assert.equal(coverageSummary(null), null)
})

test('the Untagged sentinel can never collide with a real concept id', () => {
  // It is a selection value like any other, so it has to be outside the id grammar entirely.
  assert.deepEqual(rowConcepts({ concepts: [UNTAGGED] }), [])
})
