import test from 'node:test'
import assert from 'node:assert/strict'
import { initialMapPage, mapHasMore, nextMapPage, previousMapPage, sectionMapPage } from '../src/researchMapPaging.js'
const names = ['uploads', 'cards', 'research_memos', 'reviews', 'decisions', 'experiments', 'nodes', 'reports']
const token = n => `map1:${n}:${'a'.repeat(64)}`
const page = () => ({ response_budget: { version: 1 }, ...Object.fromEntries(names.map(name => [name,
  { has_more: true, selected: true, next_cursor: token(name === 'cards' ? 1 : name === 'nodes' ? 3 : 0) }])) })
test('unequal per-collection pages advance exact cursors, and Back restores request state', () => {
  const first = initialMapPage(), next = nextMapPage(first, page())
  assert.equal(next.offset, 0)
  assert.equal(next.cursors.cards, token(1)); assert.equal(next.cursors.nodes, token(3))
  assert.deepEqual(previousMapPage(next), first)
  const section = sectionMapPage(next, 'quality')
  assert.deepEqual(section.collections, ['quality']); assert.equal(section.cursors, null)
  assert.deepEqual(previousMapPage(section), next)
  assert.ok(mapHasMore(page()))
  assert.equal(mapHasMore({ reports: { has_more: true, selected: false } }), false)
  assert.ok(mapHasMore({ reports: { has_more: true } }), 'report-only continuation stays reachable')
})
test('unsupported or incomplete new cursors cannot fall back to a skipping global offset', () => {
  const body = page(); body.nodes.next_cursor = 'bad'
  assert.throws(() => nextMapPage(initialMapPage(), body))
  assert.throws(() => nextMapPage(initialMapPage(), { response_budget: {version: 2} }))
  assert.equal(nextMapPage(initialMapPage(), {}).offset, 20, 'legacy replies keep their old paging')
})
