// The Card board as a workspace view, and the join that makes its detail pane honest.
//
// Every assertion here drives a PROPERTY rather than pinning source text, because the defect this
// whole change exists to prevent is a rendering that is structurally plausible and semantically
// wrong: a board that shows one Card as one experiment. That mistake compiles, renders, and reads
// fine — only a count can catch it. So the fixtures below deliberately contain a Card with THREE
// nodes and a Card with ZERO, and the tests assert on how many attempt rows come out.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

let vite
let CardWorkspace
let cardAttempts
let cardAttemptSummary
let nodeCardId
let route

test.before(async () => {
  vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  ;({ CardWorkspace } = await vite.ssrLoadModule('/src/CardBoard.jsx'))
  ;({ cardAttempts, cardAttemptSummary, nodeCardId } =
    await vite.ssrLoadModule('/src/cardBoardModel.js'))
  route = await vite.ssrLoadModule('/src/runRouteState.js')
})

test.after(async () => {
  await vite?.close()
})

// One Card with three attempts, one Card with none, one node reserved but not yet evidence, and one
// evidence id whose node is absent from the snapshot. This is the real shape: `card.evidence` is
// derived from terminals, so an in-flight build appears only through its `idea.card_id` stamp.
const STATE = {
  cards: {
    'card-many': {
      id: 'card-many', status: 'evaluated', verdict: 'supported', statement: 'Log-transform the target',
      evidence: [4, 7, 9], best_delta: 0.25, selection_ready: false,
    },
    'card-building': {
      id: 'card-building', status: 'building', verdict: 'open', statement: 'Deeper trees overfit here',
      evidence: [], selection_ready: false,
    },
    'card-empty': {
      id: 'card-empty', status: 'dropped', verdict: 'abandoned', statement: 'Rejected proposal',
      evidence: [], selection_ready: false,
    },
    'card-gappy': {
      id: 'card-gappy', status: 'evaluated', verdict: 'tested', statement: 'Historical fold',
      evidence: [4, 999], selection_ready: false,
    },
  },
  cards_projection: { source_valid: true, total: 4, returned: 4, omitted: 0, complete: true, items: {} },
  nodes: {
    4: { id: 4, status: 'evaluated', metric: 0.81, attempt: 0, idea: { operator: 'draft', card_id: 'card-many' } },
    7: { id: 7, status: 'failed', attempt: 1, idea: { operator: 'improve', card_id: 'card-many' } },
    9: { id: 9, status: 'evaluated', metric: 0.9, attempt: 2, idea: { operator: 'improve', card_id: 'card-many' } },
    // Reserved by the mint stamp, not yet in any evidence list — the state an in-flight build is in.
    12: { id: 12, status: 'running', attempt: 0, idea: { operator: 'improve', card_id: 'card-building' } },
    // Belongs to nothing: `Idea.card_id` is optional and legacy/external writers leave it absent.
    20: { id: 20, status: 'evaluated', metric: 0.4, idea: { operator: 'draft' } },
  },
}

const render = (props = {}) => renderToStaticMarkup(React.createElement(CardWorkspace, {
  state: STATE, runId: 'run-1', runGeneration: 'a'.repeat(64), onToast: () => {},
  selectedCardId: null, onSelectCard: () => {}, selectedNodeId: null, onSelectNode: () => {},
  renderInspector: null, pane: null, onSelect: () => {}, ...props,
}))

test('a Card holds a LIST of nodes: the join reports every attempt, not one', () => {
  const many = cardAttempts(STATE, STATE.cards['card-many'])
  // The property, stated as a number. Anything that treats a Card as an experiment collapses this
  // to 0 or 1 — which is exactly the reading the detail pane exists to refute.
  assert.equal(many.length, 3)
  assert.deepEqual(many.map(entry => entry.nodeId), [4, 7, 9])
  assert.ok(many.every(entry => entry.evidence && entry.present))
  // ...and the nodes really are different experiments, not one repeated.
  assert.deepEqual(many.map(entry => entry.node.status), ['evaluated', 'failed', 'evaluated'])
})

test('a Card can own zero nodes, and that is a state rather than a missing value', () => {
  assert.deepEqual(cardAttempts(STATE, STATE.cards['card-empty']), [])
  const roll = cardAttemptSummary([])
  assert.equal(roll.total, 0)
  assert.equal(roll.missing, 0)
})

test('a node reserved by its mint stamp joins even before it becomes evidence', () => {
  const building = cardAttempts(STATE, STATE.cards['card-building'])
  // `evidence` is empty for this card, so an evidence-only join reports zero work in flight while a
  // node for it is running. The union is what makes the count true.
  assert.equal(STATE.cards['card-building'].evidence.length, 0)
  assert.equal(building.length, 1)
  assert.equal(building[0].nodeId, 12)
  assert.equal(building[0].owned, true)
  assert.equal(building[0].evidence, false, 'a reservation must not be reported as settled evidence')
  assert.equal(cardAttemptSummary(building).ownedOnly, 1)
})

test('the node -> card edge is read from idea.card_id, never a top-level card_id', () => {
  // `core/models.py:349` puts `card_id` on the IDEA. A top-level read returns `undefined` for every
  // node in every run — it does not throw, it just silently reports that nothing belongs anywhere.
  assert.equal(nodeCardId({ idea: { card_id: 'card-many' } }), 'card-many')
  assert.equal(nodeCardId({ card_id: 'card-many' }), null)
  const decoy = {
    ...STATE,
    nodes: { 30: { id: 30, status: 'evaluated', card_id: 'card-building', idea: { operator: 'improve' } } },
  }
  assert.deepEqual(cardAttempts(decoy, STATE.cards['card-building']), [],
    'a node carrying the id only at the top level must not join')
})

test('an evidence node missing from the snapshot is retained and marked, never dropped', () => {
  const gappy = cardAttempts(STATE, STATE.cards['card-gappy'])
  assert.equal(gappy.length, 2, 'a historical fold must not silently shrink what the card cost')
  const absent = gappy.find(entry => entry.nodeId === 999)
  assert.equal(absent.present, false)
  assert.equal(absent.evidence, true)
  assert.equal(cardAttemptSummary(gappy).missing, 1)
})

test('the join is keyed by the mapping key, so a spoofed body id cannot redirect it', () => {
  const spoofed = {
    ...STATE,
    nodes: { 4: { id: 999, status: 'evaluated', idea: { card_id: 'card-many' } } },
  }
  assert.deepEqual(
    cardAttempts(spoofed, { id: 'card-many', evidence: [] }).map(entry => entry.nodeId), [4])
})

test('the detail pane renders one row per attempt and says the count in words', () => {
  const html = render({ selectedCardId: 'card-many' })
  const rows = html.match(/class="card-attempt[ "]/g) || []
  assert.equal(rows.length, 3, 'three attempts must produce three rows')
  for (const id of ['#4', '#7', '#9']) assert.ok(html.includes(id), `attempt ${id} is missing`)
  // The sentence is the correction the owner needs, so assert it is present in substance: the pane
  // must state that the work item is not itself an experiment.
  assert.match(html, /not itself an experiment/)
  assert.match(html, /these 3 experiments tested/)
})

test('a Card with no nodes explains that it is a reachable state, not a loading one', () => {
  const html = render({ selectedCardId: 'card-empty' })
  assert.equal((html.match(/class="card-attempt[ "]/g) || []).length, 0)
  assert.match(html, /No experiment has run for this work item yet/)
  assert.doesNotMatch(html, /these 0 experiments/)
})

test('lane cards carry the attempt count so the board contradicts card==node before it is opened', () => {
  const html = render()
  // Every card's own count, on its face. `card-many` has 3, `card-building` 1, `card-empty` 0.
  assert.ok(html.includes('>3</span> exp') || html.includes('3 exp'), 'the 3-attempt card must show 3')
  assert.match(html, /0 exp/)
  assert.match(html, /no experiment has run for this work item yet/)
})

test('the view layout renders the board without a modal dialog wrapper', () => {
  const html = render()
  // The board is a VIEW now: an aria-modal shell would make the header's own view-toggle inert.
  assert.doesNotMatch(html, /aria-modal/)
  assert.match(html, /class="card-board"/)
  assert.match(html, /card-detail/)
})

test('the pane hosts the node Inspector for the picked attempt instead of reimplementing it', () => {
  const marker = 'HOSTED-NODE-INSPECTOR'
  const html = render({
    selectedCardId: 'card-many', selectedNodeId: 7,
    renderInspector: nodeId => React.createElement('div', null, `${marker}:${nodeId}`),
  })
  assert.ok(html.includes(`${marker}:7`), 'the pane must mount the caller-supplied Inspector')
  // ...and it must say which object you are looking at rather than blurring card into node.
  assert.match(html, /one attempt at this work item/)
})

test('a node that is not one of the Card’s attempts does not hijack the pane', () => {
  const html = render({
    selectedCardId: 'card-many', selectedNodeId: 20,
    renderInspector: () => React.createElement('div', null, 'WRONG-NODE'),
  })
  assert.doesNotMatch(html, /WRONG-NODE/)
  assert.match(html, /not itself an experiment/, 'it must fall back to the Card record')
})

// ── Route contract ────────────────────────────────────────────────────────────────────────────

test('the retired ?panel=hypotheses link migrates to the Card view instead of being refused', () => {
  const parsed = route.parseRunRouteState(`#/run?gen=${'a'.repeat(64)}&panel=hypotheses`)
  assert.equal(parsed.state.view, 'cards')
  assert.equal(parsed.state.panel, null)
  // A migrated link is honoured, not diagnosed: reporting "Unknown panel" would tell the operator
  // their working bookmark was malformed.
  assert.deepEqual(parsed.issues, [])
})

test('an explicit view wins over the deprecated legacy panel signal', () => {
  const parsed = route.parseRunRouteState(`#/run?gen=${'a'.repeat(64)}&view=report&panel=hypotheses`)
  assert.equal(parsed.state.view, 'report')
})

test('a Card is linkable and round-trips through the URL', () => {
  const gen = 'b'.repeat(64)
  const encoded = route.encodeRunRouteState(
    { generation: gen, view: 'cards', cardId: 'card-many', nodeId: 7, nodeGeneration: 1 })
  assert.match(encoded, /view=cards/)
  assert.match(encoded, /card=card-many/)
  const back = route.parseRunRouteState(`#/run?${encoded}`)
  assert.equal(back.state.cardId, 'card-many')
  assert.equal(back.state.nodeId, 7)
  assert.deepEqual(back.issues, [])
})

test('a Card target outside the board view is refused rather than silently switching views', () => {
  const parsed = route.parseRunRouteState(`#/run?gen=${'c'.repeat(64)}&view=report&card=card-many`)
  assert.equal(parsed.state.view, 'report', 'the operator must not be yanked off the report')
  assert.equal(parsed.state.cardId, null)
  assert.ok(parsed.issues.length > 0)
})

test('a malformed Card id is dropped, and the board view survives it', () => {
  const parsed = route.parseRunRouteState(`#/run?gen=${'d'.repeat(64)}&view=cards&card=${'x'.repeat(300)}`)
  assert.equal(parsed.state.view, 'cards')
  assert.equal(parsed.state.cardId, null)
  assert.ok(parsed.issues.some(issue => /Card target/.test(issue)))
})

test('the Card board stays owner-only: a review bearer cannot reach it by link or by state', () => {
  const gen = 'e'.repeat(64)
  const parsed = route.parseRunRouteState(`#/run?gen=${gen}&view=cards&card=card-many`,
    { reviewMode: true })
  assert.notEqual(parsed.state.view, 'cards')
  assert.equal(parsed.state.cardId, null)
  // The legacy panel must not become a back door into the same surface either.
  const legacy = route.parseRunRouteState(`#/run?gen=${gen}&panel=hypotheses`, { reviewMode: true })
  assert.notEqual(legacy.state.view, 'cards')
  // ...and neither may the sanitizer, which is what an in-memory route update goes through.
  const sanitized = route.sanitizeRunRouteState(
    { generation: gen, view: 'cards', cardId: 'card-many' }, { reviewMode: true })
  assert.notEqual(sanitized.view, 'cards')
  assert.equal(sanitized.cardId, null)
  assert.ok(!route.REVIEW_SAFE_VIEWS.includes('cards'))
})

test('the board is no longer a panel, so nothing can open it as an overlay', () => {
  assert.ok(!route.RUN_ROUTE_PANELS.includes('hypotheses'))
  assert.equal(route.LEGACY_PANEL_VIEWS.hypotheses, 'cards')
})
