// A RUN-LEVEL FACT IS DERIVED ONCE, AND THE ORDER IT BUYS IS A RULE YOU CAN STATE.
//
// `runConstantConcepts` answers "which concepts does EVERY active experiment carry" — an intersection
// over all of them. It was called inside `ExpNode`, the per-CARD component, in a useMemo whose deps
// (`state.node_concepts`, `state.nodes`, …) get fresh identities on every poll of a live run. So an
// N-node canvas ran the same intersection N times per tick for a value identical in every card.
//
// MEASURED with the real canonicalizer (`conceptChips.js::nodeCanonicalConcepts`) on the largest canvas
// this box has ever drawn — `rubertlite-dense-retrieval`, 81 nodes — at 6 tags each, 5 of them shared,
// which is the shape the strip's own comment measured on v9 (40 of 48 tag slots):
//
//     N=  8   canvas/tick   0.313 ms   once-only 0.039 ms
//     N= 16   canvas/tick   0.880 ms   once-only 0.055 ms
//     N= 81   canvas/tick  22.238 ms   once-only 0.275 ms      <- 21.96 ms of duplicate work
//
// i.e. O(N²) in nodes, 0.89 % of the 2.5 s poll at N=81 and 0.013 % at v11's size. Small in absolute
// terms and stated as such — what makes it worth closing is that the fix is strictly less code and
// removes the quadratic term, not a rescued frame budget.
//
// THE ORDERING RULE MOVED WITH IT, and that is the more valuable half. It was two spread filters inline
// in the card, and the thing guarding it was a source pin over that exact expression in
// `conceptRunScope.test.js` — a pin one comment away from vacuous, which would have gone red for this
// MOVE while never having been able to go red for a behaviour change. It is now
// `nodeProjection.js::orderConceptTags`, driven below at its own contract.
//
// Every assertion here has an input that makes it FAIL; the mutations are named in the messages.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { parseAst, transformWithOxc } from 'vite'

import { orderConceptTags, runConstantConcepts } from '../src/nodeProjection.js'
import { nodeCanonicalConcepts } from '../src/conceptChips.js'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')

// v9's real shape: five ids on every experiment, one that says what the node varied.
const RUN_WIDE = ['data/esci', 'eval/recall_at_k', 'loss/contrastive/dcl/nll_cos',
                  'model/encoder/e5_small', 'training/negative_mining']
const stateOf = concepts => ({
  nodes: Object.fromEntries(Object.keys(concepts).map(k => [k, { id: Number(k) }])),
  node_concepts: concepts, aborted_nodes: [], concept_consolidation: {},
})

test('the run-wide ids go LAST and not one tag is dropped', () => {
  const tags = ['data/esci', 'training/negative_mining/hard', 'eval/recall_at_k']
  const { tags: ordered } = orderConceptTags(tags, new Set(RUN_WIDE))
  assert.deepEqual(ordered, ['training/negative_mining/hard', 'data/esci', 'eval/recall_at_k'],
    'the one tag that says what this experiment varied must come first — that IS the fix; '
    + 'mutation: order the constants first, or filter the constants out instead of moving them')
  assert.deepEqual([...ordered].sort(), [...tags].sort(),
    'a REORDER is not a filter: every id the card was given must survive it, or the operator loses a '
    + 'chip. Mutation: return `own` alone')
})

test("ownCount counts the tags that are this experiment's OWN", () => {
  const tags = ['data/esci', 'training/negative_mining/hard', 'eval/recall_at_k']
  assert.equal(orderConceptTags(tags, new Set(RUN_WIDE)).ownCount, 1,
    "mutation: return `all.length`, and the strip's note claims 0 of 3 are run-wide while two of "
    + 'them are — the exact misreading the split exists to stop')
})

test('an experiment with NOTHING of its own reports ownCount 0', () => {
  const { tags, ownCount } = orderConceptTags(['data/esci', 'eval/recall_at_k'], new Set(RUN_WIDE))
  assert.equal(ownCount, 0, 'this is what makes the card say "none of them says what it varied" — a '
    + 'fact the operator can act on. Mutation: floor ownCount at 1, or count the constants instead')
  assert.equal(tags.length, 2, 'and it still shows both, because nothing is hidden')
})

test('an EMPTY constant set is NO CLAIM: arrival order is kept exactly', () => {
  const tags = ['zeta', 'alpha', 'mu']
  for (const empty of [new Set(), null, undefined, []]) {
    const { tags: ordered, ownCount } = orderConceptTags(tags, empty)
    assert.deepEqual(ordered, tags,
      `the fail-closed answer of runConstantConcepts must not reorder anything (${String(empty)}) — `
      + 'mutation: sort the tags, and every card silently re-orders on a run that made no claim')
    assert.equal(ownCount, 3, "with no claim, every tag is the node's own")
  }
})

test('the move changed NO behaviour — it agrees with the pre-move inline expression', () => {
  // The reference is the expression that stood in ExpNode before the hoist, spelled out here on
  // purpose: this is the one test that may replicate production code, because what it checks is
  // precisely that the new rule reproduces the old one. Mutating `orderConceptTags` reddens it.
  const state = stateOf({
    0: [...RUN_WIDE, 'training/negative_mining/hard'],
    1: [...RUN_WIDE, 'loss/temperature'],
    2: RUN_WIDE,
  })
  const runConstant = runConstantConcepts(state,
    (ids, key) => nodeCanonicalConcepts(state.node_concepts, key, state.concept_consolidation || {}))
  assert.equal(runConstant.size, 5, 'precondition: the fixture really does have a run-wide half')
  for (const key of Object.keys(state.node_concepts)) {
    const all = nodeCanonicalConcepts(state.node_concepts, Number(key), {})
    const legacyTags = runConstant.size
      ? [...all.filter(c => !runConstant.has(c)), ...all.filter(c => runConstant.has(c))]
      : all
    const legacyOwn = legacyTags.length - legacyTags.filter(c => runConstant.has(c)).length
    const now = orderConceptTags(all, runConstant)
    assert.deepEqual(now.tags, legacyTags, `node ${key}: the chip order must be byte-identical`)
    assert.equal(now.ownCount, legacyOwn, `node ${key}: the own-count must be identical`)
  }
})

// ---- the placement itself, over the real AST. A comment is not an AST node. ----

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

// Parse the REAL file. JSX is stripped first (oxc) because the parser below is a plain-ESTree one;
// identifiers, call expressions and object properties all survive that transform untouched, which is
// everything these three assertions read.
const dagAst = async () => {
  const jsx = readFileSync(join(SRC, 'Dag.jsx'), 'utf8')
  const { code } = await transformWithOxc(jsx, 'Dag.jsx', { lang: 'jsx' })
  return parseAst(code)
}

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

test('the per-CARD component does not derive the run-level fact', async () => {
  const expNode = findFn(await dagAst(), 'ExpNode')
  assert.ok(expNode, 'precondition: ExpNode is still a function declaration in Dag.jsx')
  assert.ok(!calleeNames(expNode).includes('runConstantConcepts'),
    'this IS the defect: an intersection over every experiment, run once per card per poll tick. '
    + 'Mutation: put the call back in ExpNode. A comment cannot satisfy this — it is an AST node walk, '
    + 'and the substring is still in the file (in the layout memo, where it belongs)')
})

test('the whole canvas derives it exactly ONCE', async () => {
  const ast = await dagAst()
  const total = calleeNames(ast).filter(n => n === 'runConstantConcepts').length
  assert.equal(total, 1,
    'one derivation per state revision, shared by every card. Mutation: add a second call site and '
    + 'two answers to one question can drift apart within a single render')
})

test('the card is HANDED the set through its data', async () => {
  const ast = await dagAst()
  const props = []
  const walk = n => {
    if (!n || typeof n !== 'object') return
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (n.type === 'Property' && n.key?.name === 'runConstant') props.push(n)
    for (const [k, v] of Object.entries(n)) if (k !== 'type' && v && typeof v === 'object') walk(v)
  }
  walk(ast)
  assert.ok(props.length >= 1,
    'derived once and never delivered is worse than derived N times: the strip would silently stop '
    + 'marking run-wide chips at all. Mutation: drop `runConstant` from the exp node data literal')
})
