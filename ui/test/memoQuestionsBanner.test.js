// Review 2026-10-14 section 4.2: research-memo questions the board does not hold. The server decides
// which are missing (`looplab/serve/memo_questions.py`); this file drives the browser half — the
// defensive reader, the exact filing command, and the banner on both Research branches.
import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import {
  MEMO_QUESTION_BANNER_ROWS, memoQuestionCommand, memoQuestionRows, memoQuestionSummary,
} from '../src/memoQuestionsModel.js'

const projection = {
  asked: 3, on_board: 1, not_on_board: 2, truncated: false,
  rows: [
    { id: 'q-cold', statement: 'Does replay help on cold users?', concepts: ['replay', 'cold-users', 'replay'],
      memo_ids: ['m1'], times_asked: 1, parent_statement: 'Does replay help?', parent_on_board: false },
    { id: 'q-replay', statement: 'Does replay help?', concepts: ['replay'], memo_ids: ['m1', 'm2'],
      times_asked: 2 },
  ],
}

test('rows are read defensively and keep the memo-authored concepts', () => {
  const rows = memoQuestionRows(projection)
  assert.deepEqual(rows.map(row => row.statement),
    ['Does replay help on cold users?', 'Does replay help?'])
  assert.deepEqual(rows[0].concepts, ['replay', 'cold-users'], 'deduplicated, order kept')
  assert.equal(rows[0].parentStatement, 'Does replay help?')
  assert.equal(rows[1].memos, 2)
  // An older server sends no key, junk sends junk: nothing throws and nothing is invented.
  for (const junk of [undefined, null, 7, 'x', { rows: 'x' }, { rows: [null, 7, { statement: '  ' }] }]) {
    assert.deepEqual(memoQuestionRows(junk), [])
    assert.equal(memoQuestionSummary(junk).missing, 0)
  }
  // A restated question (case/spacing) is one row.
  assert.equal(memoQuestionRows({ rows: [{ statement: 'A b' }, { statement: 'a  B' }] }).length, 1)
})

test('the summary keeps the server count when the list was cut', () => {
  const many = { not_on_board: 90, rows: Array.from({ length: 80 }, (_, i) => ({ statement: `Q ${i}?` })) }
  const summary = memoQuestionSummary(many)
  assert.equal(summary.rows.length, MEMO_QUESTION_BANNER_ROWS)
  assert.equal(summary.missing, 90)
  assert.equal(summary.truncated, true)
})

test('the filing command is the board command with the memo concepts, and no empty list', () => {
  const [cold] = memoQuestionRows(projection)
  assert.deepEqual(memoQuestionCommand(cold), {
    statement: 'Does replay help on cold users?', source: 'research_memo',
    concepts: ['replay', 'cold-users'],
  })
  const bare = memoQuestionRows({ rows: [{ statement: 'Bare?' }] })[0]
  assert.deepEqual(memoQuestionCommand(bare), { statement: 'Bare?', source: 'research_memo' })
  // A memo question may be 1 200 chars: the command files ALL of it, or the filed card's belief key
  // is the cut text's and the question never leaves the banner (review 2026-10-15).
  const long = `Does replay recover recall ${'x'.repeat(1100)}?`
  const [row] = memoQuestionRows({ rows: [{ statement: long }] })
  assert.equal(memoQuestionCommand(row).statement, long)
})

let _server = null
const load = async (path) => {
  if (!_server) {
    _server = await createServer({ server: { middlewareMode: true }, appType: 'custom', logLevel: 'silent' })
  }
  return _server.ssrLoadModule(path)
}
after(async () => { if (_server) await _server.close() })

test('the banner is drawn on the empty board and on a board with questions, and only when needed', async () => {
  const { default: ResearchView } = await load('/src/ResearchView.jsx')
  const renderCard = card => React.createElement('div', { key: card.id }, card.id)
  const empty = renderToStaticMarkup(React.createElement(ResearchView, {
    cards: [], state: { memo_questions: projection }, renderCard, onFileQuestion: () => null,
  }))
  assert.match(empty, /memo-questions-banner/)
  assert.match(empty, /2 open questions from research memos are not on the question board/)
  assert.match(empty, /File 2 questions on the board/)
  const withQuestions = renderToStaticMarkup(React.createElement(ResearchView, {
    cards: [{ id: 'q1', card_kind: 'direction', statement: 'Is week 7 worse?', concept_tags: [] }],
    state: { memo_questions: projection }, renderCard,
  }))
  assert.match(withQuestions, /memo-questions-banner/)
  assert.doesNotMatch(withQuestions, /File 2 questions/, 'no filing control without a handler (review/read-only)')
  const none = renderToStaticMarkup(React.createElement(ResearchView, {
    cards: [], state: { memo_questions: { rows: [], not_on_board: 0 } }, renderCard,
  }))
  assert.doesNotMatch(none, /memo-questions-banner/)
  const legacy = renderToStaticMarkup(React.createElement(ResearchView, { cards: [], state: {}, renderCard }))
  assert.doesNotMatch(legacy, /memo-questions-banner/, 'an older server without the key draws nothing')
})
