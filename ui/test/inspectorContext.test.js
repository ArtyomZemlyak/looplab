// The Inspector's three new contexts: what a node's Card is, what its concepts are, and what it
// taught. Plus the concept view's in-place inspection rules.
//
// Every assertion here drives a PROPERTY. The defects these guard against all COMPILE and RENDER:
// a pane that shows the current attempt under a row describing an older one, a card-level concept tag
// read as this experiment's own evidence, and — the expensive one — a lesson that was merged away
// still being displayed as if it were live. None of those is visible in the source text, only in what
// comes out for a fixture built to contain the case.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

let vite
let conceptInspect
let inspectorLinks
let derivedMemory
let Inspector

test.before(async () => {
  vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  conceptInspect = await vite.ssrLoadModule('/src/conceptInspect.js')
  inspectorLinks = await vite.ssrLoadModule('/src/inspectorLinks.js')
  derivedMemory = await vite.ssrLoadModule('/src/derivedMemory.js')
  ;({ default: Inspector } = await vite.ssrLoadModule('/src/Inspector.jsx'))
})

test.after(async () => { await vite?.close() })

// One run with the shapes that matter: a node whose attempt has moved on since the concept frame was
// captured, a node belonging to a Card that has THREE attempts, a node with no card stamp at all, and
// a lesson credited to one exact (node, generation) pair.
const STATE = {
  direction: 'max',
  best_node_id: 4,
  nodes: {
    2: { id: 2, attempt: 0, status: 'evaluated', metric: 0.4, operator: 'draft',
      idea: { card_id: 'card-many', params: {} } },
    4: { id: 4, attempt: 0, status: 'evaluated', metric: 0.9, operator: 'improve',
      idea: { card_id: 'card-many', params: {} } },
    5: { id: 5, attempt: 0, status: 'pending', operator: 'improve',
      idea: { card_id: 'card-many', params: {} } },
    // Re-run in place: the frame below still describes attempt 0.
    7: { id: 7, attempt: 2, status: 'evaluated', metric: 0.2, operator: 'debug',
      idea: { params: {} } },
  },
  node_concepts: { 4: ['loss/contrastive'] },
  concept_consolidation: {},
  cards: {
    'card-many': {
      id: 'card-many', status: 'evaluated', verdict: 'supported',
      statement: 'Contrastive loss beats cross-entropy here',
      seed_statement: 'contrastive loss helps',
      evidence: [2, 4], best_delta: 0.5,
      concept_tags: ['loss/contrastive', 'data/augmentation'],
      concept_source: { kind: 'node' },
    },
  },
  lessons_distilled: [
    { at_node: 8, trigger: 'cadence', count: 2, pairs: [[4, 2]], lessons: [
      { statement: 'switching to a contrastive loss  improved the metric', outcome: 'supported',
        claim_stance: 'support', evidence: [4, 2], lesson_id: 'lesson:sha256:aa',
        evidence_refs: [{ node_id: 4, generation: 0 }, { node_id: 2, generation: 0 }] },
      { statement: 'a deeper head regressed the metric', outcome: 'failed',
        claim_stance: 'support', evidence: [7], lesson_id: 'lesson:sha256:bb',
        evidence_refs: [{ node_id: 7, generation: 0 }] },
    ] },
  ],
}

// ── item 1: the concept view inspects in place, under the frame's attempt fence ───────────────────

test('a concept frame ref is inspectable only at the attempt the frame recorded', () => {
  const { conceptRefInspectable } = conceptInspect
  assert.equal(conceptRefInspectable({ node_id: 4, node_generation: 0 }, STATE), true)
  // Node 7 is on attempt 2; the frame row describes attempt 0. Opening the Inspector would answer a
  // different question than the row asked.
  assert.equal(conceptRefInspectable({ node_id: 7, node_generation: 0 }, STATE), false,
    'a superseded attempt must not be inspectable from a row describing the old one')
  assert.equal(conceptRefInspectable({ node_id: 7, node_generation: 2 }, STATE), true)
  assert.equal(conceptRefInspectable({ node_id: 99, node_generation: 0 }, STATE), false)
  assert.equal(conceptRefInspectable(null, STATE), false)
})

test('the concept pane distinguishes nothing-picked from picked-but-absent', () => {
  const { conceptPaneTarget } = conceptInspect
  assert.equal(conceptPaneTarget(STATE, null).kind, 'empty')
  assert.equal(conceptPaneTarget(STATE, 4).kind, 'node')
  assert.equal(conceptPaneTarget(STATE, 4).attempt, 0)
  // The failure this prevents: an id that resolves to nothing rendering as an empty pane, which reads
  // as "your click did not register" rather than "that experiment is not in this snapshot".
  const absent = conceptPaneTarget(STATE, 99)
  assert.equal(absent.kind, 'absent')
  assert.equal(absent.nodeId, 99)
})

// ── item 3: the Card is the hypothesis, and one node is not one hypothesis ────────────────────────

test('a node links to its Card, and the link reports the OTHER attempts at the same question', () => {
  const { nodeCardLink } = inspectorLinks
  const link = nodeCardLink(STATE, STATE.nodes[4])
  assert.equal(link.kind, 'card')
  assert.equal(link.cardId, 'card-many')
  // The whole point: 3 attempts (2 and 4 are evidence, 5 is card-stamped but still pending), so the
  // section can never read as "this node IS the hypothesis".
  assert.equal(link.summary.total, 3)
  assert.equal(link.summary.evidence, 2)
  assert.equal(link.summary.ownedOnly, 1, 'a reserved-but-not-yet-evidence attempt still counts')
  assert.deepEqual(link.siblings.map(entry => entry.nodeId), [2, 5])
})

test('no card stamp and an unpublished card are DIFFERENT answers, and neither is an error', () => {
  const { nodeCardLink } = inspectorLinks
  assert.equal(nodeCardLink(STATE, STATE.nodes[7]).kind, 'none')
  const orphan = { id: 11, attempt: 0, idea: { card_id: 'card-not-in-frame' } }
  const link = nodeCardLink(STATE, orphan)
  assert.equal(link.kind, 'unknown')
  assert.equal(link.cardId, 'card-not-in-frame',
    'the id must survive so the pane can name what is missing rather than claim there is nothing')
})

// ── item 5: the card's concepts are a separate claim from the node's own ──────────────────────────

test('card concept tags stay in their own lane and never join the node\'s own memberships', () => {
  const { nodeConceptLanes } = inspectorLinks
  const lanes = nodeConceptLanes(['loss/contrastive'], STATE.cards['card-many'])
  assert.deepEqual(lanes.own, ['loss/contrastive'])
  // `data/augmentation` is on the CARD, derived from its first linked node. Presenting it inside the
  // node's list would upgrade a card-level claim into a claim about this attempt's own evidence.
  assert.deepEqual(lanes.cardOnly, ['data/augmentation'])
  assert.equal(lanes.cardTagOrigin, 'node')
  assert.deepEqual(nodeConceptLanes(['loss/contrastive'], null).cardOnly, [])
})

// ── item 4: history is the spine; the mutable store supplies only a standing ──────────────────────

test('a lesson is credited to the exact (node, generation) it was distilled against', () => {
  const { nodeLessons } = derivedMemory
  const forFour = nodeLessons(STATE, 4, 0, null)
  assert.equal(forFour.length, 1)
  assert.deepEqual(forFour[0].alsoFrom, [2], 'the comparative pair\'s other half is the context')
  // Node 7 is now attempt 2. The lesson was drawn from attempt 0 — a different experiment that
  // happens to share a numeric id. Inheriting it is the exact defect `lessons_reconcile` retires.
  assert.equal(nodeLessons(STATE, 7, 2, null).length, 0,
    'a re-run node must not inherit its previous attempt\'s lessons')
  assert.equal(nodeLessons(STATE, 7, 0, null).length, 1)
})

test('store standing: present, absorbed, and the truncated window that must NOT read as absorbed', () => {
  const { nodeLessons } = derivedMemory
  const complete = tier => ({ page: { tiers: { lessons: {
    limit: 200, returned: 0, skipped: 0, filtered: 0,
    source_window_truncated: false, unavailable: false, ...tier } } } })

  const live = {
    ...complete(),
    // Whitespace differs from the event-log copy on purpose: the store's own group key normalizes
    // exactly this way (`lesson_hygiene.py::normalize_statement`), so matching must too.
    lessons: [{ statement: 'Switching to a contrastive loss improved the metric', run_id: 'r' }],
  }
  assert.equal(nodeLessons(STATE, 4, 0, live)[0].storeStatus, 'present')

  // Consolidation took its base from another run's row: this statement is simply gone, and there is
  // no descendant to follow. `absorbed` is the honest name for the three causes we cannot separate.
  const merged = { ...complete(), lessons: [{ statement: 'losses matter', run_id: 'r' }] }
  assert.equal(nodeLessons(STATE, 4, 0, merged)[0].storeStatus, 'absorbed')

  // The same empty answer, but the window was cut — absence proves nothing and must not be reported
  // as a fact about the lesson. This is the assertion that stops the feature from lying.
  const cut = { ...complete({ source_window_truncated: true }), lessons: [] }
  assert.equal(nodeLessons(STATE, 4, 0, cut)[0].storeStatus, 'unknown')
  assert.equal(nodeLessons(STATE, 4, 0, { ...complete({ unavailable: true }), lessons: [] })[0].storeStatus,
    'unknown')
  assert.equal(nodeLessons(STATE, 4, 0, { ...complete({ skipped: 3 }), lessons: [] })[0].storeStatus,
    'unknown')
  // A body with no receipt at all fails closed the same way.
  assert.equal(nodeLessons(STATE, 4, 0, { lessons: [] })[0].storeStatus, 'unknown')
  // Not loaded at all.
  assert.equal(nodeLessons(STATE, 4, 0, null)[0].storeStatus, 'unknown')
})

// The lane that makes the section useful rather than a wall of "absorbed". Measured across the 46
// runs in `runs/` on 2026-08-06: 15 of 16 node-attributed lessons were no longer present as written,
// while the surviving CONSOLIDATED rows still carried `evidence` naming those same nodes.
test('live lessons crediting this node are found even when its own lesson was absorbed', () => {
  const { liveLessonsForNode, nodeLessons } = derivedMemory
  const store = {
    lessons: [{
      // The paraphrased survivor of a merge: text no run produced verbatim, evidence from the base
      // row only, and an evidence_count carrying the erased contributors.
      statement: 'changing x and y parameters regressed the metric',
      run_id: 'r', evidence: [4, 2], evidence_generations: { 4: 0, 2: 0 },
      evidence_count: 3, outcome: 'failed', concepts: ['objective/quadratic'],
    }],
    page: { tiers: { lessons: { limit: 200, returned: 1, skipped: 0, filtered: 0,
      source_window_truncated: false, unavailable: false } } },
  }
  // The node's OWN lesson is gone …
  assert.equal(nodeLessons(STATE, 4, 0, store)[0].storeStatus, 'absorbed')
  // … and yet memory still says something about it. Both facts, neither hidden.
  const live = liveLessonsForNode(store, 4, 0)
  assert.equal(live.length, 1)
  assert.equal(live[0].attemptMatch, 'exact')
  assert.deepEqual(live[0].alsoFrom, [2])
  assert.equal(live[0].evidenceCount, 3,
    'the count is the only visible trace that other runs agreed and their provenance was dropped')
})

test('a live lesson recorded against a DIFFERENT attempt is excluded; an unrecorded one is marked', () => {
  const { liveLessonsForNode } = derivedMemory
  const row = extra => ({ statement: 's', run_id: 'r', evidence: [7], ...extra })
  const at = (attempt, extra) => liveLessonsForNode({ lessons: [row(extra)] }, 7, attempt)
  // Provably a different lifecycle of the same numeric id — the fence that stops a re-run node
  // inheriting its previous life.
  assert.equal(at(2, { evidence_generations: { 7: 0 } }).length, 0)
  assert.equal(at(0, { evidence_generations: { 7: 0 } })[0].attemptMatch, 'exact')
  // No recorded attempt: real for pre-`evidence_sig` rows and for consolidated survivors. Keeping it
  // out would hide a genuine lesson; showing it unmarked would repeat the bug the fence prevents.
  assert.equal(at(2, {})[0].attemptMatch, 'unrecorded')
  assert.equal(liveLessonsForNode(null, 7, 0).length, 0)
})

test('run-level memory reports the lessons NO event-log entry attributes to a node', () => {
  const { runMemory } = derivedMemory
  const store = {
    lessons: [
      { statement: 'switching to a contrastive loss improved the metric', run_id: 'r' },
      { statement: 'the run plateaued after node 8', run_id: 'r' },   // a run-end reflect lesson
    ],
    notes: [{ note: 'best metric 0.9', run_id: 'r' }],
    cases: [],
    page: { tiers: { lessons: { limit: 200, returned: 2, skipped: 0, filtered: 0,
      source_window_truncated: false, unavailable: false } } },
  }
  const summary = runMemory(STATE, store)
  assert.equal(summary.lessons.length, 2)
  // Reflect lessons ride on a diagnostic event with no evidence, so they can never appear per-node.
  // Reporting them keeps the per-node view from looking complete when it is not.
  assert.deepEqual(summary.unattributed.map(row => row.statement), ['the run plateaued after node 8'])
  assert.equal(runMemory(STATE, null), null)
})

// ── the rendered Inspector, so a structurally-plausible wrong pane fails here ─────────────────────

const render = props => renderToStaticMarkup(React.createElement(Inspector, {
  runId: 'demo', state: STATE, live: null, tab: 'Overview', setTab() {},
  expectedGeneration: 'a'.repeat(64), ...props,
}))

test('the Inspector renders its Card link and offers Lineage only when that goes somewhere', () => {
  const hosted = render({ nodeId: 4, onOpenLineage: () => {} })
  assert.match(hosted, /card-many/)
  assert.match(hosted, /Contrastive loss beats cross-entropy here/)
  assert.match(hosted, /3 attempts at this work item/,
    'the one-node-is-one-hypothesis correction must reach the screen, not just the model')
  assert.match(hosted, /Show #4 in Lineage/)
  // Hosted BY Lineage: an affordance that lands you where you already are is worse than none.
  assert.doesNotMatch(render({ nodeId: 4, onOpenLineage: null }), /in Lineage/)
})

test('the Inspector shows the card-only concept separately from the node\'s own tags', () => {
  const html = render({ nodeId: 4 })
  const own = html.indexOf('Concepts')
  const cardLane = html.indexOf('Also on its work item')
  assert.ok(own >= 0 && cardLane > own, 'the card lane follows the node\'s own tags')
  assert.match(html, /data\/augmentation/)
})

test('a node the log credits with a lesson says so, and one it does not stays silent', () => {
  const taught = render({ nodeId: 4 })
  assert.match(taught, /What this experiment taught/)
  assert.match(taught, /switching to a contrastive loss/)
  // Never claimed as live memory without having read the store.
  assert.match(taught, /standing unknown/)
  // Node 5 produced nothing: no section, and therefore no cross-run request is ever made for it.
  assert.doesNotMatch(render({ nodeId: 5 }), /What this experiment taught/)
})
