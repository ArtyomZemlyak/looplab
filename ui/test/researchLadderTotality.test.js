// THE LADDER CLAIMS TO COVER EVERY CARD. It did not.
//
// `questionLattice.js` says the ladder and `unfiledExperiments` "between them cover every card the
// wire carried — which is the property that makes retiring the Directions tab safe rather than
// lossy". Two shapes rendered in NEITHER half:
//
//   1. question -> exp -> exp. `ResearchView` reads children only for LATTICE rows, which are
//      questions, so a refinement-of-a-refinement was grouped under its experiment parent and then
//      never asked for. `54dd4c9e` fixed the identical depth>=2 loss in `directionGroups` one day
//      before `9440cff5` retired the only total view — the ladder inherited the bug as the tab that
//      compensated for it went away.
//   2. a card whose `parent_card_id` names a card this page does not hold (the 256-row wire cap).
//      It is NOT unfiled — the run has a question for it — so `unfiledExperiments` rightly refuses
//      it, and before this there was nowhere else for it to go.
//
// The totality test below is the one that matters: it asserts the partition directly, which is the
// claim the comment makes and the thing a per-function test cannot check.
import assert from 'node:assert/strict'
import test from 'node:test'

import { childrenByParent, descendantsOf } from '../src/cardLineageModel.js'
import { offPageParentExperiments, unfiledExperiments } from '../src/questionLattice.js'

const q = (id, parent) => ({ id, card_kind: 'direction', parent_card_id: parent })
const x = (id, parent) => ({ id, card_kind: 'experiment', parent_card_id: parent })

// Every non-direction card must appear in EXACTLY ONE of: some question's descendants, unfiled,
// or off-page-parent.
const partition = (cards, questionIds) => {
  const byParent = childrenByParent(cards)
  const under = new Set()
  for (const qid of questionIds) for (const c of descendantsOf(qid, byParent)) under.add(String(c.id))
  const unfiled = new Set(unfiledExperiments(cards).map(c => String(c.id)))
  const offpage = new Set(offPageParentExperiments(cards).map(c => String(c.id)))
  return { under, unfiled, offpage }
}

test('TOTALITY: every experiment lands in exactly one section', () => {
  const cards = [
    q('q1'), x('e1', 'q1'), x('e2', 'e1'), x('e3', 'e2'),   // depth 3 under a question
    x('e4'),                                                 // unfiled
    x('e5', 'gone'),                                         // parent off page
  ]
  const { under, unfiled, offpage } = partition(cards, ['q1'])
  for (const id of ['e1', 'e2', 'e3', 'e4', 'e5']) {
    const hits = [under.has(id), unfiled.has(id), offpage.has(id)].filter(Boolean).length
    assert.equal(hits, 1, `${id} appears in ${hits} sections, must be exactly 1`)
  }
  assert.deepEqual([...under].sort(), ['e1', 'e2', 'e3'])
  assert.deepEqual([...unfiled], ['e4'])
  assert.deepEqual([...offpage], ['e5'])
})

test('a DEPTH-2 experiment is reached — the defect', () => {
  const cards = [q('q1'), x('e1', 'q1'), x('e2', 'e1')]
  const kids = descendantsOf('q1', childrenByParent(cards))
  assert.deepEqual(kids.map(c => c.id).sort(), ['e1', 'e2'],
    'drawing only immediate children put the refinement-of-a-refinement in no section at all')
})

test('an OFF-PAGE parent is its own bucket, never folded into unfiled', () => {
  const cards = [x('e5', 'gone'), x('e4')]
  assert.deepEqual(unfiledExperiments(cards).map(c => c.id), ['e4'],
    'the run HAS a question for e5; calling it unfiled asserts the opposite')
  assert.deepEqual(offPageParentExperiments(cards).map(c => c.id), ['e5'])
})

test('a DIRECTION is in none of the three — it has its own lattice row', () => {
  const cards = [q('q1'), q('q2', 'q1')]
  assert.deepEqual(unfiledExperiments(cards).map(c => c.id), [])
  assert.deepEqual(offPageParentExperiments(cards).map(c => c.id), [])
  assert.deepEqual(descendantsOf('q1', childrenByParent(cards)).map(c => c.id), [],
    'a nested question is drawn by the lattice, not as its parent\'s work')
})

test('a PARENT CYCLE terminates — driven at the walk\'s OWN contract', () => {
  // NOT reachable from a question, and that is the point worth recording. `parent_card_id` is a
  // single edge and `childrenByParent` drops directions, so from a question root every path is a
  // tree and a cycle can never be entered — my first cycle fixtures started at `q1`, and a mutant
  // that DELETED the visited set passed them all, because the cycle sat in a component the walk
  // never reached. The guard is real at the function's own contract, where the start node is
  // inside the cycle, so that is where it is driven.
  const cards = [
    { id: 'm1', card_kind: 'experiment', parent_card_id: 'm2' },
    { id: 'm2', card_kind: 'experiment', parent_card_id: 'm1' },
  ]
  const walked = descendantsOf('m1', childrenByParent(cards))
  assert.deepEqual(walked.map(c => c.id), ['m2'],
    'm1 is the seed and must not be re-emitted as its own descendant')
})

test('a SELF-PARENT card does not re-emit itself', () => {
  const cards = [{ id: 'c', card_kind: 'experiment', parent_card_id: 'c' }]
  assert.deepEqual(descendantsOf('c', childrenByParent(cards)).map(c => c.id), [])
})

test('a question root is a TREE, so its walk terminates without needing the guard', () => {
  const cards = [q('q1'), x('a', 'q1'), x('b', 'a'), x('a2', 'b')]
  assert.deepEqual(descendantsOf('q1', childrenByParent(cards)).map(c => c.id).sort(),
    ['a', 'a2', 'b'])
})
