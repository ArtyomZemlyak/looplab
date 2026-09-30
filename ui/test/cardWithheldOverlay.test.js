// A `running` card whose evaluation a pause WITHHELD reads `held`, not Running (doc 69 69.12b).
//
// The fold keeps the lifecycle admitted — the `eval_attempt_withheld` row is diagnostic and never
// folded — so `card_ledger.py` split the card into `running` while the server's own node activity,
// in the same /state frame, said the node waits for its evaluation (critic 2026-09-30, driven: node
// `{status: 'queued', evidence: 'eval_attempt_withheld'}` beside card `status: running`). Only that
// server evidence moves the card, only to `held` — never `coded`, whose "it has NOT started" is
// false for an evaluation held after its canary or its failed attempt ran (critic 2026-09-30) — and
// the folded status travels with the row and into the chip's title.
import test from 'node:test'
import assert from 'node:assert/strict'

import { CARD_COLUMNS, CARD_OPTIONAL_STATUSES, cardLanes, cardRows, cardSelectionBlock }
  from '../src/cardBoardModel.js'

const withheld = generation => ({ schema: 1, status: 'queued', generation,
  evidence: 'eval_attempt_withheld' })
const evaluating = generation => ({ schema: 1, status: 'evaluating', generation,
  evidence: 'node_eval_started' })

const state = (nodes, statusNodes = [0]) => ({
  engine_running: true,
  cards: { 'card-0': { status: 'running', status_nodes: statusNodes, statement: 'w' } },
  nodes,
})

test('every status node withheld: the card waits in Held, and says what the fold said', () => {
  const [row] = cardRows(state({ 0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) } }))
  assert.equal(row.status, 'held')
  assert.deepEqual(row.withheld, { folded_status: 'running' })
})

test('the Held lane and its chip never say the evaluation has not started', () => {
  // MUTATION: overlay onto `coded` again -> the lane hint and the chip read "has NOT started".
  const hint = CARD_COLUMNS.find(([status]) => status === 'held')[2]
  assert.doesNotMatch(hint, /not started/i)
  assert.ok(CARD_OPTIONAL_STATUSES.has('held'), 'a lane only a withheld card occupies')
  const [row] = cardRows(state({ 0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) } }))
  const block = cardSelectionBlock({ ...row, selection_ready: false,
    selection_blockers: ['work_in_flight'] })
  assert.equal(block.label, 'its evaluation is held by a pause')
  // The ledger's own word is on the chip, beside the overlay (MUTATION: drop it from the title).
  assert.match(block.title, /the ledger still folds it running/)
  assert.ok(cardLanes([row]).some(([status]) => status === 'held'))
})

test('a lifecycle number is an integer, not whatever `Number` coerces', () => {
  // `Number(null)`, `Number('')` and `Number(true)` are integers; none is a generation.
  // MUTATION: the old `Number.isInteger(Number(x))` guard -> each of these moves the card.
  for (const bad of [null, '', true, false]) {
    const node = { id: 0, attempt: 0, status: 'pending', activity: withheld(bad) }
    const zero = { id: 0, attempt: bad, status: 'pending', activity: withheld(0) }
    assert.equal(cardRows(state({ 0: node }))[0].status, 'running', String(bad))
    assert.equal(cardRows(state({ 0: zero }))[0].status, 'running', `attempt ${bad}`)
  }
})

test('one node really running keeps the card Running', () => {
  // MUTATION: `some` instead of `every` -> Coded while an experiment trains.
  const [row] = cardRows(state({
    0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) },
    1: { id: 1, attempt: 0, status: 'pending', activity: evaluating(0) },
  }, [0, 1]))
  assert.equal(row.status, 'running')
})

test("only the server's generation-matched evidence moves it", () => {
  // A stale activity row (a reset's previous lifecycle) and a plain queued node are no answer.
  for (const node of [
    { id: 0, attempt: 1, status: 'pending', activity: withheld(0) },
    { id: 0, attempt: 0, status: 'pending', activity: { ...withheld(0), evidence: 'node_created_boundary' } },
    { id: 0, attempt: 0, status: 'pending' },
  ]) {
    assert.equal(cardRows(state({ 0: node }))[0].status, 'running', JSON.stringify(node))
  }
  // The withhold evidence with any status but `queued` is no withhold: a re-dispatch that already
  // started evaluating again (MUTATION: drop the `queued` test -> Held while it trains).
  assert.equal(cardRows(state({ 0: { id: 0, attempt: 0, status: 'pending',
    activity: { ...withheld(0), status: 'evaluating' } } }))[0].status, 'running')
  // A card that names no status node has no server statement at all: an empty `every` is true.
  // MUTATION: drop `ids.length > 0` -> Coded on no evidence.
  assert.equal(cardRows(state({ 0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) } },
    []))[0].status, 'running')
  // …and a card that is not `running` is never touched, whatever its nodes say.
  const evaluated = { ...state({ 0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) } }) }
  evaluated.cards['card-0'].status = 'evaluated'
  assert.equal(cardRows(evaluated)[0].status, 'evaluated')
})
