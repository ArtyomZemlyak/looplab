// Unit tests for the shared concept search/filter model. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  normalizeQuery, highlightSegments, conceptUniverse, searchConcepts, searchHighlightIds,
  experimentRefMatches, filterConceptTree,
} from '../src/conceptSearch.js'

// Same fixture shape as conceptChips.test.js: node id -> raw concept ids (subtree membership).
const NC = {
  0: ['loss/contrastive/dcl', 'architecture/moe'],
  1: ['loss/contrastive/mnr'],
  2: ['loss/mnr'],
  3: ['data/synth'],
  4: ['loss'],
}

test('normalizeQuery trims + lowercases; non-strings collapse to empty', () => {
  assert.equal(normalizeQuery('  Loss  '), 'loss')
  assert.equal(normalizeQuery(''), '')
  assert.equal(normalizeQuery(null), '')
  assert.equal(normalizeQuery(undefined), '')
})

test('highlightSegments marks the first case-insensitive occurrence', () => {
  assert.deepEqual(highlightSegments('contrastive', 'trast'),
    [{ text: 'con', hit: false }, { text: 'trast', hit: true }, { text: 'ive', hit: false }])
  // case-insensitive, and leading match has no empty prefix segment
  assert.deepEqual(highlightSegments('AdamW', 'adam'),
    [{ text: 'Adam', hit: true }, { text: 'W', hit: false }])
  // no query / no match -> one non-hit segment
  assert.deepEqual(highlightSegments('loss', ''), [{ text: 'loss', hit: false }])
  assert.deepEqual(highlightSegments('loss', 'zzz'), [{ text: 'loss', hit: false }])
  // nullish text is safe
  assert.deepEqual(highlightSegments(null, 'x'), [{ text: '', hit: false }])
})

test('conceptUniverse counts every ancestor with subtree membership', () => {
  const universe = conceptUniverse(NC, {})
  // loss subtree touches nodes 0,1,2,4
  assert.deepEqual([...universe.get('loss')].sort((a, b) => a - b), [0, 1, 2, 4])
  // ancestor prefixes exist even when never tagged exactly
  assert.deepEqual([...universe.get('loss/contrastive')].sort((a, b) => a - b), [0, 1])
  assert.deepEqual([...universe.get('loss/contrastive/dcl')], [0])
  assert.deepEqual([...universe.get('architecture')], [0])
  assert.equal(universe.get('data').size, 1)
})

test('conceptUniverse follows the consolidation rename map', () => {
  const universe = conceptUniverse(NC, { 'architecture/moe': 'model/moe' })
  assert.ok(universe.has('model/moe'))
  assert.ok(universe.has('model'))
  assert.ok(!universe.has('architecture/moe'))
})

test('searchConcepts ranks leaf-anchored matches first, then by count', () => {
  const results = searchConcepts(NC, {}, 'loss')
  const ids = results.map(r => r.id)
  // exact-leaf `loss` first (rank 0), then deeper `loss/...` path matches by count desc
  assert.equal(ids[0], 'loss')
  assert.ok(ids.includes('loss/contrastive'))
  assert.ok(ids.includes('loss/contrastive/dcl'))
  // count is the subtree membership
  assert.equal(results.find(r => r.id === 'loss').count, 4)
  assert.equal(results.find(r => r.id === 'loss/contrastive').count, 2)
  // label is the leaf segment
  assert.equal(results.find(r => r.id === 'loss/contrastive/dcl').label, 'dcl')
})

test('searchConcepts matches a deep leaf by its own name', () => {
  const results = searchConcepts(NC, {}, 'dcl')
  assert.deepEqual(results.map(r => r.id), ['loss/contrastive/dcl'])
})

test('searchConcepts empty query -> [] and respects the limit', () => {
  assert.deepEqual(searchConcepts(NC, {}, ''), [])
  assert.deepEqual(searchConcepts(NC, {}, '   '), [])
  assert.equal(searchConcepts(NC, {}, 'loss', 1).length, 1)
})

test('searchHighlightIds unions matching subtrees; no match -> null', () => {
  const hi = searchHighlightIds(NC, {}, 'contrastive')
  assert.ok(hi instanceof Set)
  assert.deepEqual([...hi].sort((a, b) => a - b), [0, 1])       // dcl + mnr nodes
  // a coarse query highlights the whole subtree
  assert.deepEqual([...searchHighlightIds(NC, {}, 'loss')].sort((a, b) => a - b), [0, 1, 2, 4])
  // no match -> null (no dimming), never an empty Set that would strand the graph fully dimmed
  assert.equal(searchHighlightIds(NC, {}, 'zzz'), null)
  assert.equal(searchHighlightIds(NC, {}, ''), null)
})

test('searchHighlightIds retargets through a live rename', () => {
  const hi = searchHighlightIds(NC, { 'architecture/moe': 'model/moe' }, 'model')
  assert.deepEqual([...hi], [0])
})

test('prototype-named concept ids stay data, never crash', () => {
  const evil = { 0: ['__proto__/x', 'constructor/y'], 1: ['loss'] }
  // must not throw and must not leak Object.prototype keys
  const universe = conceptUniverse(evil, {})
  assert.ok(universe.has('loss'))
  assert.equal(searchHighlightIds(evil, {}, 'loss') instanceof Set, true)
})

// ── View 1 tree filter ──────────────────────────────────────────────────────
// Minimal validated-shape tree: is_a hierarchy where node.parent is the path parent.
const TREE = {
  lens: 'is_a',
  roots: ['loss', 'optimizer'],
  nodes: {
    loss: { parent: null, depth: 0, children: ['loss/contrastive'], tagged: false },
    'loss/contrastive': { parent: 'loss', depth: 1, children: ['loss/contrastive/dcl'], tagged: false },
    'loss/contrastive/dcl': { parent: 'loss/contrastive', depth: 2, children: [], tagged: true },
    optimizer: { parent: null, depth: 0, children: ['optimizer/adamw'], tagged: false },
    'optimizer/adamw': { parent: 'optimizer', depth: 1, children: [], tagged: true },
  },
}
const REFS = {
  'loss/contrastive/dcl': [{ node_id: 8, node_generation: 1, status: 'evaluated' }],
  'optimizer/adamw': [
    { node_id: 3, node_generation: 1, status: 'failed' },
    { node_id: 8, node_generation: 1, status: 'evaluated' },
  ],
}

test('filterConceptTree returns null for an empty query', () => {
  assert.equal(filterConceptTree(TREE, REFS, ''), null)
  assert.equal(filterConceptTree(TREE, REFS, '  '), null)
})

test('filterConceptTree keeps a concept match and every ancestor, expanding the path', () => {
  const f = filterConceptTree(TREE, REFS, 'dcl')
  assert.deepEqual([...f.visible].sort(), ['loss', 'loss/contrastive', 'loss/contrastive/dcl'])
  // ancestors are force-expanded so the DFS reaches the match; the leaf itself is not expanded
  assert.deepEqual([...f.expand].sort(), ['loss', 'loss/contrastive'])
  assert.ok(f.conceptHit.has('loss/contrastive/dcl'))
  assert.equal(f.evidenceOpen.size, 0)
})

test('a concept-id match pulls in its whole subtree (descendants are matches too)', () => {
  // Every loss-tree id contains "loss", so each descendant is itself a match, visible and on an
  // expanded path — no explicit subtree pass is needed to reveal children of a matched concept.
  const f = filterConceptTree(TREE, REFS, 'loss')
  assert.deepEqual([...f.visible].sort(), ['loss', 'loss/contrastive', 'loss/contrastive/dcl'])
  assert.deepEqual([...f.expand].sort(), ['loss', 'loss/contrastive'])
  assert.equal(f.conceptHit.size, 3)
})

test('filterConceptTree matches an experiment by status and auto-opens its evidence', () => {
  const f = filterConceptTree(TREE, REFS, 'failed')
  // only optimizer/adamw has a failed experiment
  assert.deepEqual([...f.visible].sort(), ['optimizer', 'optimizer/adamw'])
  assert.ok(f.evidenceOpen.has('optimizer/adamw'))
  assert.equal(f.conceptHit.size, 0)
  assert.deepEqual([...f.expand], ['optimizer'])
})

test('filterConceptTree matches an experiment by #id', () => {
  const f = filterConceptTree(TREE, REFS, '#3')
  assert.deepEqual([...f.visible].sort(), ['optimizer', 'optimizer/adamw'])
  assert.ok(f.evidenceOpen.has('optimizer/adamw'))
})

test('filterConceptTree can disable experiment matching', () => {
  const f = filterConceptTree(TREE, REFS, 'failed', { matchExperiments: false })
  assert.equal(f.visible.size, 0)
})

test('experimentRefMatches guards node id and status; empty query -> false', () => {
  const ref = { node_id: 8, node_generation: 1, status: 'evaluated' }
  assert.equal(experimentRefMatches(ref, '#8'), true)
  assert.equal(experimentRefMatches(ref, '8'), true)
  assert.equal(experimentRefMatches(ref, 'eval'), true)
  assert.equal(experimentRefMatches(ref, '7'), false)
  assert.equal(experimentRefMatches(ref, ''), false)
  assert.equal(experimentRefMatches(null, 'x'), false)
})

test('filterConceptTree tolerates a malformed tree without throwing', () => {
  const f = filterConceptTree(null, REFS, 'loss')
  assert.deepEqual([...f.visible], [])
  assert.deepEqual([...f.expand], [])
})
