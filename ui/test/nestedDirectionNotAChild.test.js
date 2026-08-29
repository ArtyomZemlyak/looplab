// A NESTED QUESTION IS NOT SOMEBODY'S EXPERIMENT.
//
// `ResearchView` groups children by `parent_card_id` over `all`, which holds BOTH kinds, and the
// fold sets `parent_card_id` for any card without consulting its kind
// (`events/card_ledger.py`). So a DIRECTION carrying a parent was counted, labelled and drawn
// below its parent as an experiment — while the same card also stood in the lattice as a question.
// One card, two contradictory readings on one screen, and the experiment reading is the false one:
// a direction owns no action and has no result to roll up.
//
// THE STATE IS UNREACHED ON THIS BOX and this file says so rather than implying a recovery: across
// every preserved log, 0 of 218 `card_added` rows carry a direction with a `parent_card_id`. The
// fold permits it with no guard, so nothing but the Researcher\'s habit keeps it at zero.
//
// This drives the grouping rule directly rather than through the component, because the rule is
// what was wrong; `ResearchView` has SSR coverage elsewhere for the rendering.
import assert from 'node:assert/strict'
import test from 'node:test'

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { childrenByParent } from '../src/cardLineageModel.js'

// THE REAL FUNCTION, not a replica. An earlier cut of this file copied `ResearchView`'s loop, and a
// mutation run proved the copy worthless: INVERTING the production filter (keep only directions)
// passed every test, because the replica still had the right rule and the structural check below
// only asks that the call is PRESENT. The rule now lives in `cardLineageModel.js` where a test can
// reach it — CLAUDE.md's tier 2, "hoist it into a named function and test its truth table".
const groupChildren = childrenByParent

const question = (id, parent) => ({ id, card_kind: 'direction', parent_card_id: parent })
const experiment = (id, parent) => ({ id, card_kind: 'experiment', parent_card_id: parent })

test('a nested DIRECTION is not grouped as a child experiment', () => {
  const kids = groupChildren([question('q2', 'q1'), experiment('e1', 'q1')])
  assert.deepEqual((kids.get('q1') || []).map(c => c.id), ['e1'],
    'the nested question keeps its lattice position; drawing it here too is the contradiction')
})

test('an EXPERIMENT under a question is still grouped', () => {
  const kids = groupChildren([experiment('e1', 'q1'), experiment('e2', 'q1')])
  assert.deepEqual((kids.get('q1') || []).map(c => c.id), ['e1', 'e2'],
    'the whole point of the grouping must survive the filter')
})

test('a parentless card is grouped under nothing, either kind', () => {
  const kids = groupChildren([question('q1'), experiment('e1')])
  assert.equal(kids.size, 0)
})

test('the kind predicate is what decides, not the presence of a parent', () => {
  // A direction WITHOUT a parent and an experiment WITH one: only the second is a child.
  const kids = groupChildren([question('q1'), experiment('e1', 'q1')])
  assert.deepEqual([...kids.keys()], ['q1'])
  assert.deepEqual(kids.get('q1').map(c => c.id), ['e1'])
})

test('a card with an unknown kind is treated as an experiment, not dropped', () => {
  // `cardIsDirection` is a positive test for one kind; anything else keeps the behaviour it had.
  // Dropping unknown kinds would silently hide a card the board still holds.
  const kids = groupChildren([{ id: 'x1', parent_card_id: 'q1' }])
  assert.deepEqual((kids.get('q1') || []).map(c => c.id), ['x1'])
})

test('ResearchView USES the hoisted rule and grows no loop of its own', () => {
  // The behavioural tests above now drive the real function, so this no longer compensates for a
  // replica — it guards the WIRING: the view must call the shared rule rather than rebuilding a
  // parent map inline, which is how the two would drift apart again. Comments are stripped first,
  // because the cheapest mutation is to delete the code and leave a comment carrying the literal.
  const src = readFileSync(fileURLToPath(new URL('../src/ResearchView.jsx', import.meta.url)), 'utf8')
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.match(code, /childrenByParent\(/,
    'the view must CALL the shared rule')
  assert.ok(!/for\s*\(const card of all\)/.test(code),
    'and must not have regrown an inline parent-map loop beside it')
})
