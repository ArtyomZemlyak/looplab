// The in-flight AUTHORING overlay on the Card board.
//
// The question this answers: does a card appear on the Kanban board when work on it STARTS, or only
// once it is fully formed? On the speculative lane the fold stamps `status:'building'` from
// `node_building`, which `engine/speculation.py` appends only AFTER the Developer thread returns —
// measured on runs/rubertlite-dr-unified-v5, card-0's build ran 2,128 s while the board said
// "Proposed · work item is open and has not started", and the Building lane was occupied for 0.3 s.
//
// `state.card_authoring` (looplab/events/authoring_projection.py) is the derived, never-folded head
// state that closes that window. These tests drive the overlay's decisions, not its text.
import test from 'node:test'
import assert from 'node:assert/strict'

import { cardAuthoring, cardLanes, cardRows } from '../src/cardBoardModel.js'

const proposed = (id, extra = {}) => ({ status: 'proposed', statement: `work item ${id}`, ...extra })

const live = (overrides = {}) => ({
  engine_running: true,
  cards: { 'card-0': proposed('card-0') },
  card_authoring: [{
    card_id: 'card-0', generation: 0, index: 0, phase: 'building',
    started: 1700000000, folded_status: 'proposed',
  }],
  ...overrides,
})

test('a card whose Developer is running now leaves the Proposed lane for Building', () => {
  const rows = cardRows(live())
  assert.equal(rows.length, 1)
  assert.equal(rows[0].status, 'building')
  // The folded truth travels with the row: the overlay is never allowed to erase what replay says.
  assert.equal(rows[0].authoring.folded_status, 'proposed')
  assert.equal(rows[0].authoring.started, 1700000000)
  assert.equal(rows[0].authoring.index, 0)
})

test('an elected-but-not-yet-started head occupies the optional Speculating lane', () => {
  const state = live()
  state.card_authoring[0].phase = 'speculating'
  const rows = cardRows(state)
  assert.equal(rows[0].status, 'speculating')
  // `speculating` is a CARD_OPTIONAL_STATUS: it must appear as a column only now that it is occupied.
  assert.ok(cardLanes(rows).some(([status]) => status === 'speculating'))
  assert.ok(!cardLanes(cardRows(live({ card_authoring: [] }))).some(([s]) => s === 'speculating'))
})

test('the fold wins whenever it has anything to say', () => {
  // A stale/duplicated head must never pull a card BACK out of a replay-derived lane. Each of these
  // is a fact about the log; the overlay is a fact about a process that is running right now.
  for (const folded of ['building', 'running', 'evaluated', 'gated', 'dropped', 'coded']) {
    const rows = cardRows(live({ cards: { 'card-0': proposed('card-0', { status: folded }) } }))
    assert.equal(rows[0].status, folded, `overlay overrode the folded ${folded} lane`)
    assert.equal(rows[0].authoring, undefined)
  }
})

test('a dead engine stops the clock instead of breathing forever', () => {
  // Same rule as buildingModel.js::withBuilding: a run whose engine died mid-build keeps its open
  // head in the log for good, and a card that says "being written" for a build nobody is running is
  // exactly the phantom that guard exists to prevent.
  assert.equal(cardRows(live({ engine_running: false }))[0].status, 'proposed')
  assert.equal(cardRows(live({ finished: true }))[0].status, 'proposed')
  // ...but an engine whose liveness is simply unknown (an old server sends no flag) keeps the overlay.
  assert.equal(cardRows(live({ engine_running: undefined }))[0].status, 'building')
})

test('an unknown phase from a newer server is dropped, never rendered as a lane', () => {
  const state = live()
  state.card_authoring[0].phase = 'awaiting-fabrication'
  assert.equal(cardRows(state)[0].status, 'proposed')
  assert.equal(cardAuthoring(state).size, 0)
})

test('malformed or duplicated authoring rows cannot change a join', () => {
  const state = live({
    cards: { 'card-0': proposed('card-0'), 'card-1': proposed('card-1') },
    card_authoring: [
      null,
      { card_id: 'card-0', phase: 'building', started: 'not-a-number', index: -1 },
      { card_id: 'card-0', phase: 'speculating' },       // second row for the same card: first wins
      { card_id: '', phase: 'building' },
      { phase: 'building' },
    ],
  })
  const map = cardAuthoring(state)
  assert.equal(map.size, 1)
  assert.equal(map.get('card-0').phase, 'building')
  assert.equal(map.get('card-0').started, null)          // a non-numeric clock is absent, not zero
  assert.equal(map.get('card-0').index, null)
  const rows = cardRows(state)
  assert.equal(rows.find(row => row.id === 'card-0').status, 'building')
  assert.equal(rows.find(row => row.id === 'card-1').status, 'proposed')
})

test('a server that sends no authoring key behaves exactly as before', () => {
  const state = { engine_running: true, cards: { 'card-0': proposed('card-0') } }
  assert.equal(cardRows(state)[0].status, 'proposed')
  assert.equal(cardRows(state)[0].authoring, undefined)
  assert.equal(cardAuthoring(state).size, 0)
  assert.equal(cardAuthoring(null).size, 0)
})

test('the mapping key still beats a spoofed body id under the overlay', () => {
  const state = live({ cards: { 'card-0': proposed('card-0', { id: 'card-9' }) } })
  const rows = cardRows(state)
  assert.equal(rows[0].id, 'card-0')
  assert.equal(rows[0].status, 'building')
})
