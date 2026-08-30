// THE ROLLUP ASKED FOR EVERY ROW'S DESCENDANTS ONCE PER ROW, AND EACH ASK SCANNED EVERY ROW.
//
// `latticeRows` duplicates a card under every immediate parent, so a row is a PLACEMENT and the row
// list enumerates root-to-node PATHS. `latticeRollups` then called `descendantIds(rows, rowKey)` per
// row, and that helper scanned the whole list doing `startsWith` on keys that grow with depth —
// O(rows^2) string comparisons over a list that is itself super-linear in cards.
//
// MEASURED on the worst shape the note named, a COMPLETE subset lattice (every non-empty subset of n
// concepts, so 2^n - 1 cards and sum C(n,k)*k! placements), before and after:
//
//   n  cards    rows    latticeRows_ms   latticeRollups_ms before -> after
//   5     31     325           2.1              3.5  ->    1.3
//   6     63    1956           5.0             29.4  ->    6.8
//   7    127   13699          27.0           1596.3  ->   35.9      (44x)
//   8    255  109600         340.7        ~unrunnable ->  241.8
//
// n=8 is not an arbitrary ceiling: the cards wire is capped at 256 rows, so 255 IS the largest
// payload this surface can ever be handed, and it went from freezing the tab to 583 ms of one memo.
//
// REACHABILITY, stated honestly: no board on this box comes near it. Across every preserved run,
// 197 `card_added` rows carry at most ELEVEN concepts and 165 carry none, and a plausible 64-card
// board over a 6-concept pool draws 1,616 placements (1.6 ms + 17.4 ms). The fix is worth making
// because the WORST case is a payload the wire permits, not because a real run has hit it.
//
// The enumeration itself is deliberately NOT bounded here. Electing a canonical parent would hide
// half the structure — the operator's own stated decision, recorded at the top of the module — and
// after this change the enumeration is the larger half of a cost that no longer freezes anything.
//
// Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { UNGROUPED_ID, descendantIds, descendantIndex, latticeRollups, latticeRows }
  from '../src/questionLattice.js'

const card = (id, concept_tags) => ({ id, card_kind: 'question', statement: id, concept_tags })

// The reading this replaced, spelled out: for each row, every row whose key extends it. This is the
// one place a test may replicate production code, because what it checks is that the index is the
// SAME ANSWER — a faster derivation that returns something else is not an optimisation.
const referenceScan = (rows, rowKey) => {
  if (!rows.find(r => r.rowKey === rowKey)) return []
  const out = new Set()
  for (const row of rows) if (row.rowKey.startsWith(`${rowKey}>`)) out.add(row.id)
  return [...out]
}

const fullLattice = n => {
  const tags = Array.from({ length: n }, (_, i) => `c/${i}`)
  const out = []
  for (let m = 1; m < (1 << n); m += 1) out.push(card(`s${m}`, tags.filter((_, i) => m & (1 << i))))
  return out
}

test('the index is the SAME ANSWER as the scan it replaced, on every row of a real shape', () => {
  const cards = [
    card('q-distill', ['distill']),
    card('q-llm', ['llm']),
    card('q-both', ['distill', 'llm']),
    card('q-deep', ['distill', 'llm', 'depth']),
    card('q-none', []),
  ]
  const rows = latticeRows(cards)
  const index = descendantIndex(rows)
  assert.ok(rows.length > cards.length,
    'precondition: this board really does duplicate a card under two parents, or the test proves '
    + 'nothing about the shape that was slow')
  for (const row of rows) {
    assert.deepEqual([...(index.get(row.rowKey) || [])].sort(), referenceScan(rows, row.rowKey).sort(),
      `row ${row.rowKey}: mutation — index the row's own key too (i <= parts.length) and a row `
      + 'becomes its own descendant, inflating every rolled-up count by one')
  }
})

test('and on a COMPLETE subset lattice, the shape that made it quadratic', () => {
  const rows = latticeRows(fullLattice(4))
  const index = descendantIndex(rows)
  assert.equal(rows.length, 64, 'precondition: 15 cards expand to 64 placements — sum C(4,k)*k!')
  for (const row of rows) {
    assert.deepEqual([...(index.get(row.rowKey) || [])].sort(), referenceScan(rows, row.rowKey).sort(),
      `row ${row.rowKey}: mutation — key the index on rowKey instead of id, and a placement id `
      + 'stands in for a card id, so `descendants` counts paths again')
  }
})

test('the UNGROUPED bucket label is not a row, so it has NO descendants', () => {
  const rows = latticeRows([card('q-a', ['a']), card('q-none', []), card('q-none2', [])])
  const untagged = rows.filter(r => r.parentId === UNGROUPED_ID)
  assert.equal(untagged.length, 2, 'precondition: two untagged cards landed in the bucket')
  assert.deepEqual(descendantIds(rows, UNGROUPED_ID), [],
    'mutation: drop the `known` set and index every prefix blindly — the bucket LABEL then collects '
    + 'every untagged card as a descendant, which the scan never did because it began with '
    + '`rows.find(rowKey)`. That is a different answer, not a faster one')
  assert.deepEqual(descendantIds(rows, 'nope'), [], 'an unknown key is still empty')
})

test('a rolled-up descendant COUNT is unchanged by the reindex', () => {
  const cards = [
    card('q-distill', ['distill']),
    card('q-llm', ['llm']),
    card('q-both', ['distill', 'llm']),
  ]
  const rows = latticeRows(cards)
  const roll = latticeRollups({ nodes: {} }, cards, rows)
  for (const row of rows) {
    assert.equal(roll.get(row.rowKey).descendants, referenceScan(rows, row.rowKey).length,
      `row ${row.rowKey}: the rollup must report exactly what the scan would have counted — `
      + 'mutation: build the index from the CARDS instead of the rows and a duplicated placement '
      + 'reports its sibling copy\'s subtree')
  }
  assert.ok(roll.get('q-distill').descendants > 0, 'precondition: something is actually under it')
})

test('a deep chain attributes every descendant to every ancestor, not just the immediate one', () => {
  const rows = latticeRows([card('a', ['x']), card('b', ['x', 'y']), card('c', ['x', 'y', 'z'])])
  const index = descendantIndex(rows)
  assert.deepEqual([...index.get('a')].sort(), ['b', 'c'],
    'mutation: record only the immediate parent (drop the loop over prefixes) and a question stops '
    + 'seeing the experiments two levels under it — the roll-up silently under-reports')
  assert.deepEqual([...index.get('a>b')].sort(), ['c'])
})

// ---- the efficiency property itself, over the real AST ----
//
// THE FIRST CUT OF THIS FILE LET A MUTANT LIVE, and it is the same lesson the DAG run-constant fix
// recorded one file over: reverting `latticeRollups` to ask `descendantIds(rows, row.rowKey)` per row
// — i.e. deleting the entire fix — left all five behavioural tests above GREEN. It had to: the change
// is behaviour-preserving by construction, so no assertion about the ANSWER can see it go.
//
// A timing assertion is the wrong instrument and this repo has already paid for that mistake (four
// flaky tests, one root: an assertion over a duration the box controls). What is stable is the SHAPE
// of the call, so that is what is pinned, over a real parse — a comment is not an AST node.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseAst } from 'vite'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')

const findFn = (ast, name) => {
  let hit = null
  const walk = n => {
    if (!n || typeof n !== 'object' || hit) return
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (n.type === 'FunctionDeclaration' && n.id?.name === name) { hit = n; return }
    for (const [k, v] of Object.entries(n)) if (k !== 'type' && v && typeof v === 'object') walk(v)
  }
  walk(ast)
  return hit
}
const calleeNames = node => {
  const found = []
  const walk = n => {
    if (!n || typeof n !== 'object') return
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (n.type === 'CallExpression' && n.callee?.type === 'Identifier') found.push(n.callee.name)
    for (const [k, v] of Object.entries(n)) if (k !== 'type' && v && typeof v === 'object') walk(v)
  }
  walk(node)
  return found
}
const rollupCalls = () => {
  const ast = parseAst(readFileSync(join(SRC, 'questionLattice.js'), 'utf8'))
  const fn = findFn(ast, 'latticeRollups')
  assert.ok(fn, 'precondition: latticeRollups is still a function declaration')
  return calleeNames(fn)
}

test('the rollup builds the descendant map ONCE', () => {
  assert.equal(rollupCalls().filter(n => n === 'descendantIndex').length, 1,
    'mutation: build it inside the row loop and the whole fix is undone with every answer still '
    + 'correct — which is exactly why this is pinned on the call and not on the output')
})

test('the rollup never asks for ONE row s descendants', () => {
  assert.ok(!rollupCalls().includes('descendantIds'),
    'THE MUTANT THAT SURVIVED the first cut of this file: reverting to a per-row `descendantIds` '
    + 'restores O(rows^2) `startsWith` — 1,596 ms at n=7, a frozen tab at n=8 — and every behavioural '
    + 'assertion above stays green, because the answer is identical. The single-row helper is still '
    + 'exported and still correct for a caller that wants exactly one row; what must not come back '
    + 'is asking it once per row')
})
