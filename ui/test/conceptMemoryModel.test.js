// "You have to be able to go from a concept into memory, notes, cases, knowledge."
//
// The global concept view could answer "which RUNS touched this idea" and nothing else, while the
// Memory panel held the lessons, cases and notes indexed by the very same concept ids — one screen
// away, with no path between them.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CONCEPT_MEMORY_TIERS, conceptMemory, conceptMemoryNotice,
} from '../src/conceptMemoryModel.js'

const memory = () => ({
  lessons: [
    { statement: 'temperature 0.05 was decisive', concepts: ['loss/contrastive/dcl'], task_id: 't' },
    { statement: 'unrelated', concepts: ['retrieval/dense'], task_id: 't' },
    { statement: 'nobody said what this is about', concepts: [], task_id: 't' },
  ],
  cases: [{ goal: 'maximise recall', concepts: ['loss'], task_id: 't' }],
  notes: [{ note: 'ran 14 nodes', concepts: [], task_id: 't' }],
  knowledge: [{ name: 'a human note' }],
})

test('a concept selects everything BELOW it, not only the exact id', () => {
  // One definition of "about this concept", shared with the Memory panel's own filter — two would
  // drift, and the operator would get different answers from two screens about one question.
  const result = conceptMemory(memory(), 'loss')
  assert.deepEqual(result.groups.map(g => g.key), ['lessons', 'cases'])
  assert.equal(result.groups[0].rows[0].statement, 'temperature 0.05 was decisive')
  assert.equal(result.matched, 2)
})

test('an exact deeper id still selects itself', () => {
  const result = conceptMemory(memory(), 'loss/contrastive/dcl')
  assert.equal(result.matched, 1)
  assert.equal(result.groups.length, 1)
})

test('a tier with no match is omitted rather than shown empty', () => {
  const result = conceptMemory(memory(), 'retrieval')
  assert.deepEqual(result.groups.map(g => g.key), ['lessons'])
})

test('each row carries the text its tier is actually about', () => {
  // Lessons state a claim, cases state a goal, notes state a note. Reading `statement` off a case
  // would render an empty row for every case in the store.
  const result = conceptMemory(memory(), 'loss')
  assert.equal(result.groups[1].rows[0]._text, 'maximise recall')
  const notes = conceptMemory({ notes: [{ note: 'hello', concepts: ['x'] }] }, 'x')
  assert.equal(notes.groups[0].rows[0]._text, 'hello')
})

test('knowledge is never a group — a concept could not select it if it tried', () => {
  // It is human-authored and carries no run concepts. An always-empty group would imply otherwise.
  assert.ok(!CONCEPT_MEMORY_TIERS.some(([key]) => key === 'knowledge'))
  const result = conceptMemory(memory(), 'loss')
  assert.ok(!result.groups.some(g => g.key === 'knowledge'))
})

test('the untagged count is the honest denominator, not a footnote', () => {
  // "2 lessons" without "and 147 carry no concept" invites the reader to conclude the lab learned
  // almost nothing about everything else. Measured on the real store before the clean-up: 14 of 161.
  const result = conceptMemory(memory(), 'loss')
  assert.equal(result.untagged, 2)
  assert.equal(result.total, 5)
  assert.match(conceptMemoryNotice(result), /2 of 5 memory rows carry no concept/)
})

test('an empty match says whether anything COULD have matched', () => {
  const nothingTagged = conceptMemory({ lessons: [{ statement: 'x', concepts: [] }] }, 'loss')
  assert.match(conceptMemoryNotice(nothingTagged), /none of the 1 memory rows carries a concept/)

  const someTagged = conceptMemory(memory(), 'nothing/here')
  assert.match(conceptMemoryNotice(someTagged), /Nothing links to this concept\./)
  assert.match(conceptMemoryNotice(someTagged), /2 of 5/)
})

test('an empty store says so instead of implying the concept is unstudied', () => {
  assert.match(conceptMemoryNotice(conceptMemory({}, 'loss')), /Cross-run memory is empty/)
  assert.match(conceptMemoryNotice(conceptMemory(null, 'loss')), /Cross-run memory is empty/)
})

test('no selection matches nothing rather than everything', () => {
  // `rowMatchesConcept` returns true for a falsy concept — right for a FILTER, wrong here, where an
  // empty selection would dump the whole store into a concept's detail panel.
  const result = conceptMemory(memory(), '')
  assert.equal(result.matched, 0)
})


test('the Concepts view reads the API through the prefixed helper, never a bare fetch', async () => {
  // A root-relative `/api/...` resolves against the ORIGIN, so it misses the deployment's path
  // prefix — this UI is served under `/user/<name>/proxy/<port>/` on JupyterHub — and carries no
  // owner token. Both reads would 404 or 401 on exactly the deployment the operator uses, and the
  // section would silently render as "cross-run memory is empty". Found by review; the pre-existing
  // concept-policy read had the same shape and is fixed with it.
  const { readFileSync } = await import('node:fs')
  const source = readFileSync(new URL('../src/PortfolioConcepts.jsx', import.meta.url), 'utf8')
  assert.ok(!/\bfetch\('\/api/.test(source), 'a bare fetch of an /api path came back')
  assert.ok(source.includes("get('/api/memory'"), 'the memory read must go through the helper')
  assert.ok(source.includes("get('/api/cross-run/concept-policy'"))
})
