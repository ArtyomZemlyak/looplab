// A `running` card whose evaluation a pause WITHHELD reads `coded`, not Running (doc 69 69.12b).
//
// The fold keeps the lifecycle admitted — the `eval_attempt_withheld` row is diagnostic and never
// folded — so `card_ledger.py` split the card into `running` while the server's own node activity,
// in the same /state frame, said the node waits for its evaluation (critic 2026-09-30, driven: node
// `{status: 'queued', evidence: 'eval_attempt_withheld'}` beside card `status: running`). Only that
// server evidence moves the card, only to `coded`, and the folded status travels with the row.
import test from 'node:test'
import assert from 'node:assert/strict'

import { cardRows } from '../src/cardBoardModel.js'

const withheld = generation => ({ schema: 1, status: 'queued', generation,
  evidence: 'eval_attempt_withheld' })
const evaluating = generation => ({ schema: 1, status: 'evaluating', generation,
  evidence: 'node_eval_started' })

const state = (nodes, statusNodes = [0]) => ({
  engine_running: true,
  cards: { 'card-0': { status: 'running', status_nodes: statusNodes, statement: 'w' } },
  nodes,
})

test('every status node withheld: the card waits in Coded, and says what the fold said', () => {
  const [row] = cardRows(state({ 0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) } }))
  assert.equal(row.status, 'coded')
  assert.deepEqual(row.withheld, { folded_status: 'running' })
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
  // A card that names no status node has no server statement at all: an empty `every` is true.
  // MUTATION: drop `ids.length > 0` -> Coded on no evidence.
  assert.equal(cardRows(state({ 0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) } },
    []))[0].status, 'running')
  // …and a card that is not `running` is never touched, whatever its nodes say.
  const evaluated = { ...state({ 0: { id: 0, attempt: 0, status: 'pending', activity: withheld(0) } }) }
  evaluated.cards['card-0'].status = 'evaluated'
  assert.equal(cardRows(evaluated)[0].status, 'evaluated')
})
