// The question lattice's pure model. Every property here is one the shape gets wrong under an
// obvious simpler implementation — a path tree, a canonical parent, a summed rollup — and each was
// driven by mutating the module and watching this file go red.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import {
  UNGROUPED_ID, conceptSet, descendantIds, isStrictSubset, latticeRollups, latticeRows,
  questionClosure,
  UNFILED_EXPERIMENTS_ID, unfiledExperiments,
} from '../src/questionLattice.js'

const q = (id, tags, extra = {}) => ({ id, concept_tags: tags, ...extra })
const keys = rows => rows.map(r => `${'  '.repeat(r.depth)}${r.id}`)

test('a set is normalised, so {a,b} and {b,a} are one question position', () => {
  assert.deepEqual(conceptSet(q('x', ['b', 'a'])), ['a', 'b'])
  assert.deepEqual(conceptSet(q('x', ['a', 'a', ' a '])), ['a'])
  // Junk on the wire is not a concept and must not become a lattice coordinate.
  assert.deepEqual(conceptSet(q('x', ['', '  ', null, 7, 'a'])), ['a'])
  assert.deepEqual(conceptSet({ id: 'x' }), [])
  assert.deepEqual(conceptSet(null), [])
})

test('subset is STRICT — an equal pair is one position, not a sharpening of itself', () => {
  assert.equal(isStrictSubset(['a'], ['a', 'b']), true)
  assert.equal(isStrictSubset(['a', 'b'], ['a', 'b']), false)
  assert.equal(isStrictSubset(['a', 'c'], ['a', 'b']), false)
  assert.equal(isStrictSubset([], ['a']), true)
})

test('a sharpening hangs under the question it sharpens', () => {
  const rows = latticeRows([q('q2', ['distill', 'llm']), q('q1', ['distill'])])
  assert.deepEqual(keys(rows), ['q1', '  q2'])
  assert.equal(rows[1].parentId, 'q1')
})

test('a row with two parents is DUPLICATED under both, subtree and all', () => {
  const rows = latticeRows([
    q('q1', ['distill']), q('q4', ['llm']),
    q('q2', ['distill', 'llm']), q('q3', ['distill', 'llm', 'rl']),
  ])
  assert.deepEqual(keys(rows), ['q1', '  q2', '    q3', 'q4', '  q2', '    q3'])
  // Distinct render keys per PLACEMENT: one collapse must not close the other copy.
  assert.deepEqual(rows.filter(r => r.id === 'q2').map(r => r.rowKey), ['q1>q2', 'q4>q2'])
  assert.deepEqual(rows.filter(r => r.id === 'q2').map(r => r.duplicated), [true, true])
  assert.equal(rows.find(r => r.rowKey === 'q1>q2>q3').duplicated, false)
})

test('only the IMMEDIATE parent adopts — a grandchild is not also drawn at depth 1', () => {
  // `{a}` is a strict subset of `{a,b,c}` as well, and adopting on strictness alone draws the same
  // subtree twice at two depths and doubles every number rolled up through it.
  const rows = latticeRows([q('a', ['a']), q('ab', ['a', 'b']), q('abc', ['a', 'b', 'c'])])
  assert.deepEqual(keys(rows), ['a', '  ab', '    abc'])
  assert.equal(rows.filter(r => r.id === 'abc').length, 1)
})

test('an untagged question keeps its own bucket, last, and is never dropped', () => {
  const rows = latticeRows([q('bare', []), q('a', ['a'])])
  assert.deepEqual(keys(rows), ['a', 'bare'])
  const bare = rows.find(r => r.id === 'bare')
  assert.equal(bare.parentId, UNGROUPED_ID)
  assert.equal(bare.rowKey, `${UNGROUPED_ID}>bare`)
})

test('two questions with the SAME set are siblings, not one nested under the other', () => {
  const rows = latticeRows([q('x', ['a']), q('y', ['a'])])
  assert.deepEqual(keys(rows), ['x', 'y'])
})

test('a card with no id is not a row, and a non-record is not a card', () => {
  assert.deepEqual(latticeRows([null, 'q1', { concept_tags: ['a'] }, q('q1', ['a'])]).map(r => r.id),
    ['q1'])
  assert.deepEqual(latticeRows(null), [])
})

test('descendants are per PLACEMENT and exclude the row itself', () => {
  const rows = latticeRows([q('q1', ['a']), q('q4', ['b']), q('q2', ['a', 'b'])])
  assert.deepEqual(descendantIds(rows, 'q1'), ['q2'])
  assert.deepEqual(descendantIds(rows, 'q1>q2'), [])
  assert.deepEqual(descendantIds(rows, 'nope'), [])
})

// A question's number lives in `child_rollup` — the best its own experiments measured — because a
// question owns no action and its own `best_delta` is null on every real board.
const asked = (id, tags, best, extra = {}) => q(id, tags, {
  child_rollup: best === null ? null : { children: 1, best_delta: best, best_card_id: `${id}-x` },
  ...extra,
})

test('a question wears the BEST delta in its subtree, counting ITSELF and not summing', () => {
  const cards = [asked('q1', ['a'], 3), asked('q2', ['a', 'b'], 5), asked('q3', ['a', 'b', 'c'], 1)]
  const roll = latticeRollups({ nodes: {} }, cards, latticeRows(cards))
  const top = roll.get('q1')
  // 5, not 9 (a sum) and not 3 (its own alone), and it comes from the CHILD.
  assert.equal(top.best, 5)
  assert.equal(top.bestCardId, 'q2')
  assert.equal(top.own, 3, 'the row still reports what its OWN experiments reached')
  assert.equal(top.descendants, 2)
})

test('a question with experiments and NO sharpening still shows its own number', () => {
  // The first cut scanned descendants only and reported `null` here — i.e. on the whole early
  // board, where nobody has asked a sharper question yet.
  const cards = [asked('q1', ['a'], 4)]
  const roll = latticeRollups({ nodes: {} }, cards, latticeRows(cards)).get('q1')
  assert.equal(roll.best, 4)
  assert.equal(roll.bestCardId, 'q1')
  assert.equal(roll.descendants, 0)
})

test('the maximum is kept even when it arrives FIRST', () => {
  // Without this arm "keep the max" and "keep whatever came last" are indistinguishable.
  const cards = [asked('q1', ['a'], null), asked('q2', ['a', 'b'], 5), asked('q3', ['a', 'b', 'c'], 2)]
  const head = latticeRollups({ nodes: {} }, cards, latticeRows(cards)).get('q1')
  assert.equal(head.best, 5)
  assert.equal(head.bestCardId, 'q2')
})

test('an EXPERIMENT card in the lattice reports its own best_delta', () => {
  // Both fields are consulted: reading only `child_rollup` reports null for every experiment.
  const cards = [q('e1', ['a'], { best_delta: 7 })]
  assert.equal(latticeRollups({ nodes: {} }, cards, latticeRows(cards)).get('e1').best, 7)
})

test('a non-finite or missing delta contributes nothing rather than a zero', () => {
  const cards = [
    q('q1', ['a']),
    asked('q2', ['a', 'b'], Number.NaN),
    asked('q3', ['a', 'c'], Number.POSITIVE_INFINITY),
    q('q4', ['a', 'd'], {}),
  ]
  const roll = latticeRollups({ nodes: {} }, cards, latticeRows(cards))
  assert.equal(roll.get('q1').best, null)
  assert.equal(roll.get('q1').bestCardId, null)
})

test('provably different comparability MARKS the best, it does not suppress it', () => {
  const cards = [
    q('q1', ['a']),
    q('q2', ['a', 'b'], { best_delta: 2, evidence: ['n1'] }),
    q('q3', ['a', 'c'], { best_delta: 5, evidence: ['n2'] }),
  ]
  const rec = key => ({ metric_provenance: { comparability: { keys: { measured: key } } } })
  const state = { nodes: { n1: rec('LEFT'), n2: rec('RIGHT') } }
  const mixed = latticeRollups(state, cards, latticeRows(cards)).get('q1')
  assert.equal(mixed.mixedComparability, true)
  assert.equal(mixed.best, 5, 'the number stays — hiding it leaves the busiest question blank')
  assert.equal(mixed.measuredNodes, 2)

  const same = { nodes: { n1: rec('SAME'), n2: rec('SAME') } }
  assert.equal(latticeRollups(same, cards, latticeRows(cards)).get('q1').mixedComparability, false)
})

test('ABSENT keys are silence, not a second key — an unrecorded board is never marked', () => {
  // Every node on this box records no comparability key. A refusal that counted silence as
  // disagreement would fire on all of them and therefore mean nothing.
  const cards = [
    q('q1', ['a']),
    q('q2', ['a', 'b'], { best_delta: 2, evidence: ['n1'] }),
    q('q3', ['a', 'c'], { best_delta: 5, evidence: ['n2'] }),
  ]
  const state = { nodes: { n1: { metric: 1 }, n2: { metric: 2 } } }
  assert.equal(latticeRollups(state, cards, latticeRows(cards)).get('q1').mixedComparability, false)
})

test('an evidence id that resolves to no node is not counted as measured', () => {
  const cards = [q('q1', ['a']), q('q2', ['a', 'b'], { best_delta: 1, evidence: ['gone'] })]
  const roll = latticeRollups({ nodes: {} }, cards, latticeRows(cards)).get('q1')
  assert.equal(roll.measuredNodes, 0)
  assert.equal(roll.best, 1, 'the delta is on the CARD; a trimmed node does not unmake it')
})

test("a question's comparability marker comes from its EXPERIMENTS' nodes", () => {
  // A question's own `evidence` is empty by construction, so reading only that field marks nothing
  // on exactly the rows that aggregate the most work.
  //
  // THE FIXTURE IS THE WIRE SHAPE, and it was not: these cards carried `child_card_ids`, a field
  // `serve/public_cards.py::_FIELDS` deliberately never publishes, so this test passed against a
  // payload production cannot produce while the real one marked nothing on any question. The edge
  // that IS on the wire is `parent_card_id`, pointing the other way.
  const rec = key => ({ metric_provenance: { comparability: { keys: { measured: key } } } })
  const cards = [
    q('q1', ['a'], { child_rollup: { children: 2, best_delta: 3, best_card_id: 'e1' } }),
    q('e1', ['a'], { best_delta: 3, evidence: ['n1'], parent_card_id: 'q1' }),
    q('e2', ['a'], { best_delta: 1, evidence: ['n2'], parent_card_id: 'q1' }),
  ]
  // Only the question is a lattice row; its experiments are reached through the parent edge.
  const rows = latticeRows(cards.slice(0, 1))
  const split = latticeRollups({ nodes: { n1: rec('L'), n2: rec('R') } }, cards, rows).get('q1')
  assert.equal(split.measuredNodes, 2)
  assert.equal(split.mixedComparability, true)

  const agreed = latticeRollups({ nodes: { n1: rec('S'), n2: rec('S') } }, cards, rows).get('q1')
  assert.equal(agreed.mixedComparability, false)
})

test('a closed question with nothing narrower is an UNSUPPORTED discard', () => {
  const cards = [{ id: 'q1', concept_tags: ['a'], status: 'dropped' }]
  const rows = latticeRows(cards)
  const roll = latticeRollups({ nodes: {} }, cards, rows).get('q1')
  const closure = questionClosure(cards[0], roll)
  assert.equal(closure.closed, true)
  assert.equal(closure.by, 'dropped')
  assert.equal(closure.supported, false, 'nothing sharper and nothing measured')
})

test('a sharper question, or a measured experiment, SUPPORTS the discard', () => {
  const bySharper = [
    { id: 'q1', concept_tags: ['a'], status: 'dropped' },
    { id: 'q2', concept_tags: ['a', 'b'] },
  ]
  const sharperRoll = latticeRollups({ nodes: {} }, bySharper, latticeRows(bySharper)).get('q1')
  assert.equal(questionClosure(bySharper[0], sharperRoll).supported, true)

  // Same correction as above: the child is reached through its own `parent_card_id`, the field the
  // wire actually carries.
  const byEvidence = [{ id: 'q1', concept_tags: ['a'], verdict: 'abandoned' },
    { id: 'e1', concept_tags: ['a'], evidence: ['n1'], parent_card_id: 'q1' }]
  const rows = latticeRows(byEvidence.slice(0, 1))
  const roll = latticeRollups({ nodes: { n1: { metric: 1 } } }, byEvidence, rows).get('q1')
  const closure = questionClosure(byEvidence[0], roll)
  assert.equal(closure.by, 'abandoned')
  assert.equal(closure.supported, true)
})

test('an OPEN question has no closure at all — absence is not "supported"', () => {
  const cards = [{ id: 'q1', concept_tags: ['a'], status: 'proposed', verdict: 'open' }]
  const roll = latticeRollups({ nodes: {} }, cards, latticeRows(cards)).get('q1')
  assert.equal(questionClosure(cards[0], roll), null)
  assert.equal(questionClosure(null, roll), null)
})

// The complement of the ladder (P2, second half). Between `latticeRows` and this, every card the
// wire carried is drawn somewhere — which is the property that makes retiring the Directions tab
// safe rather than lossy. Its "Not filed under any direction" group was the ONLY surface that ever
// drew a parentless experiment, and the operator's first complaint about this board was two of them.
test('an experiment no question claims is drawn, and a claimed one is not', () => {
  const cards = [
    { id: 'q1', card_kind: 'direction', concept_tags: ['a'] },
    { id: 'e-kid', card_kind: 'experiment', parent_card_id: 'q1' },
    { id: 'e-orphan', card_kind: 'experiment' },
  ]
  assert.deepEqual(unfiledExperiments(cards).map(c => c.id), ['e-orphan'])
})

test('a parent this PAGE cannot show is still a parent — the card is filed, not unfiled', () => {
  // `child_card_ids` clips at CARD_CHILD_LIMIT and the wire caps the board at 256 rows, so an edge
  // naming an absent card is routine. Drawing it here would assert the run has no question for it
  // when it has one it cannot draw.
  const cards = [{ id: 'e', card_kind: 'experiment', parent_card_id: 'q-off-the-page' }]
  assert.deepEqual(unfiledExperiments(cards), [])
})

test('a QUESTION is never unfiled — a root question already has its own row', () => {
  const cards = [{ id: 'q', card_kind: 'direction' }]
  assert.deepEqual(unfiledExperiments(cards), [])
})

test('the ladder and the unfiled bucket together cover EVERY card', () => {
  // The invariant the tab retirement rests on. A card that is in neither is a row that vanishes.
  const cards = [
    { id: 'q1', card_kind: 'direction', concept_tags: ['a'] },
    { id: 'q2', card_kind: 'direction', concept_tags: ['a', 'b'] },
    { id: 'q3', card_kind: 'direction' },
    { id: 'e1', card_kind: 'experiment', parent_card_id: 'q1' },
    { id: 'e2', card_kind: 'experiment' },
  ]
  const questions = cards.filter(c => c.card_kind === 'direction')
  const drawn = new Set([
    ...latticeRows(questions).map(r => r.id),
    ...unfiledExperiments(cards).map(c => c.id),
    // an experiment WITH a parent is drawn under its question by the view, from `parent_card_id`
    ...cards.filter(c => c.card_kind !== 'direction' && c.parent_card_id).map(c => c.id),
  ])
  assert.deepEqual([...drawn].sort(), ['e1', 'e2', 'q1', 'q2', 'q3'])
})

test('unfiledExperiments is total over junk', () => {
  assert.deepEqual(unfiledExperiments(null), [])
  assert.deepEqual(unfiledExperiments([null, 'x', { no_id: 1 }]), [])
})

// --------------------------------------------------------------------------- //
// A question inherits the concepts of the experiments filed under it (2026-08-26).
//
// The operator reported four symptoms at once — cards filed nowhere, no hierarchy, no concepts on
// questions, and Research looking identical to the Directions tab — and they are ONE cause:
// `conceptSet` read only the AUTHORED `concept_tags` and ignored `child_concept_tags`, the union the
// fold computes over a question's children. With every set empty there is no subset relation, so
// every row falls to the ungrouped bucket and the ladder flattens into the parent->child list
// Directions drew.
//
// Measured on `runs/e5small-dr-unified-v7`: five questions, ALL `concept_tags = []`, one carrying
// `child_concept_tags` of NINE ids — the union present, populated and unread.
// --------------------------------------------------------------------------- //

function _question(id, { tags = [], childTags = [] } = {}) {
  return {
    id, statement: id, seed_statement: id,
    concept_tags: tags, child_concept_tags: childTags,
    // THE UI'S KIND IS THE SERVER-PUBLISHED `card_kind`, not `selection_provenance` — that is what
    // `cardLineageModel.js::cardKind` reads, and a fixture carrying the python-side field instead
    // renders as an EXPERIMENT and inherits nothing. Driving these caught exactly that.
    card_kind: 'direction',
  }
}

function _experiment(id, { tags = [], childTags = [] } = {}) {
  return {
    id, statement: id, seed_statement: id,
    concept_tags: tags, child_concept_tags: childTags,
    card_kind: 'experiment',
  }
}

test('a question with no authored tags inherits its children\'s', () => {
  const q = _question('q-1', { childTags: ['loss/contrastive', 'eval/recall-at-k'] })
  assert.deepEqual(conceptSet(q), ['eval/recall-at-k', 'loss/contrastive'],
    'MUTATION: drop `child_concept_tags` from the union and this is [] — the v7 shape, where every ' +
    'question is ungrouped and the ladder has no hierarchy to draw')
})

test('authored and inherited tags UNION rather than one replacing the other', () => {
  const q = _question('q-1', { tags: ['data'], childTags: ['data', 'loss/contrastive'] })
  assert.deepEqual(conceptSet(q), ['data', 'loss/contrastive'],
    'the overlap collapses — a set, not a concatenation')
})

test('an EXPERIMENT does not inherit: its tags are its own claim', () => {
  const e = _experiment('card-2', { tags: ['loss/contrastive'], childTags: ['data', 'eval'] })
  assert.deepEqual(conceptSet(e), ['loss/contrastive'],
    'MUTATION: union unconditionally instead of for direction rows and this gains `data`/`eval` — ' +
    'a card would appear to touch everything its siblings do')
})

test('inheritance actually produces the nesting the operator was missing', () => {
  // The end-to-end property, not the field read: a broad question and a narrower one that shares its
  // concepts must nest — and BOTH get their sets purely from their children.
  const rows = latticeRows([
    _question('broad', { childTags: ['loss/contrastive'] }),
    _question('narrow', { childTags: ['loss/contrastive', 'training/negative-mining'] }),
  ])
  const narrow = rows.find(r => r.id === 'narrow')
  assert.ok(narrow, 'the narrower question is placed')
  assert.equal(narrow.parentId, 'broad',
    'MUTATION: drop the inheritance and both sets are empty, both rows land in the ungrouped ' +
    'bucket, and depth is 0 for everything — exactly what the operator saw')
  assert.equal(narrow.depth, 1)
})

test('a question with neither authored nor inherited tags is still ungrouped', () => {
  // The honest limit of this fix: on v7 it lights up ONE question, because the other four have no
  // children AND no authored concepts. Those stay ungrouped, correctly — nothing places them.
  const rows = latticeRows([_question('bare')])
  assert.equal(rows.length, 1)
  assert.equal(rows[0].parentId, UNGROUPED_ID,
    'no concepts from any source means no position in a concept lattice — its own bucket, as before')
})

test('the model reads no card field the server never publishes', () => {
  // THE CLASS OF DEFECT, not one instance of it. `cardEvidenceNodes` joined a question to its
  // experiments through `card.child_card_ids`, which `serve/public_cards.py::_FIELDS` deliberately
  // does not put on the wire — so in the browser the loop ran zero times, `measuredNodes` was 0 for
  // every question, and every closed direction was drawn with the red "closed with NOTHING narrower
  // behind it" chip. The unit tests above passed only because their fixtures supplied the field by
  // hand, which is how a model can be tested green against a payload production cannot produce.
  //
  // Derived from the server's own `_FIELDS` tuple so it tracks the wire rather than restating it.
  const py = readFileSync(new URL('../../looplab/serve/public_cards.py', import.meta.url), 'utf8')
  const block = py.split('\n_FIELDS = (')[1].split(')')[0]
  const published = new Set([...block.matchAll(/"([a-z_]+)"/g)].map(m => m[1]))
  assert.ok(published.size > 10, 'the _FIELDS read came back too small to be the real tuple')
  assert.ok(published.has('parent_card_id') && !published.has('child_card_ids'),
    'sanity: the parent edge is published and the child list is not')

  const src = readFileSync(new URL('../src/questionLattice.js', import.meta.url), 'utf8')
    .replace(/^\s*\/\/.*$/gm, '')          // a comment naming a field is not a read of it
  // Every `card.<field>` / `.<field>` read this module performs against a card-shaped object.
  const known = new Set([...src.matchAll(/\bcard\.([a-z_]+)/g)].map(m => m[1]))
  const unpublished = [...known].filter(name => !published.has(name))
  assert.deepEqual(unpublished, [],
    `${unpublished.join(', ')} are read off a card here and never published by public_cards.py — ` +
    'each is permanently undefined in the browser')
})
