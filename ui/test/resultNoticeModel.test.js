import test from 'node:test'
import assert from 'node:assert/strict'
import { resultNoticeQuestion, resultNoticeText, validResultNotices } from '../src/resultNoticeModel.js'

import { generation, node, payload } from './_resultNoticesFixtures.js'

test('numbers do not become an improvement without comparison evidence; both directions and constraints', () => {
  assert.match(resultNoticeText(node).outcome, /improvement not established/)
  assert.match(resultNoticeText(node).caution, /exploratory/)
  assert.match(resultNoticeText(node, 'ru').outcome, /улучшение не установлено/)
  const comparable = { ...node, parents: [{ ...node.parents[0], comparability: 'same' }] }
  assert.match(resultNoticeText(comparable).outcome, /improves on parent/)
  assert.match(resultNoticeText({ ...comparable, direction: 'min' }).outcome, /worse than parent/)
  assert.match(resultNoticeText({ ...comparable, feasible: false }).outcome, /check eligibility.*not established/)
  assert.match(resultNoticeText({ ...node, confirmed_mean: 0.6, confirmed_seeds: 1 }).caution, /not established/)
  assert.match(resultNoticeText({ ...node, status: 'failed', score: null }).outcome, /Evaluation failed/)
  assert.match(resultNoticeText({ ...node, status: 'aborted', score: null }, 'ru').outcome, /Завершённого результата нет/)
  assert.match(resultNoticeText({ ...node, status: 'failed', score: null, failure: 'crash' }, 'ru').outcome, /ошибка выполнения команды/)
  assert.match(resultNoticeText(node, 'ru').caution, /Нет подтверждения повторными/)
  assert.match(resultNoticeQuestion(node), /experiment #2, attempt 1.*Do not start experiments/)
  assert.match(resultNoticeQuestion({ kind: 'run' }, 'ru'), /итог этого запуска.*Не запускай/)
  assert.match(resultNoticeQuestion({ ...node, status: 'failed' }, 'ru'), /Trace и логи.*минимальное исправление.*Не запускай/)
})

test('incomplete, wrong generation, duplicate and invalid scalar receipts are rejected', () => {
  assert.equal(validResultNotices(payload([node]), generation), true)
  for (const value of [payload([node, node]), { ...payload([node]), generation: 'c'.repeat(64) },
    payload([{ ...node, score: Infinity }]), payload([{ ...node, node_id: true }]),
    payload([{ ...node, status: 'running' }]), payload([{ ...node, evidence_token: '' }])]) {
    assert.equal(validResultNotices(value, generation), false)
  }
})
