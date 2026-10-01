import test from 'node:test'
import assert from 'node:assert/strict'
import { resultCaveatText, resultNoticeQuestion, resultNoticeText, validResultNotices } from '../src/resultNoticeModel.js'

import { generation, node, payload } from './_resultNoticesFixtures.js'

test('numbers do not become an improvement without comparison evidence; both directions and constraints', () => {
  assert.match(resultNoticeText(node).comparison, /improvement not established.*conditions are not established/)
  assert.match(resultNoticeText(node).caution, /exploratory/)
  assert.match(resultNoticeText(node, 'ru').comparison, /улучшение не установлено/)
  const comparable = { ...node, parents: [{ ...node.parents[0], comparability: 'same' }] }
  assert.match(resultNoticeText(comparable).comparison, /improves on parent/)
  assert.match(resultNoticeText({ ...comparable, direction: 'min' }).comparison, /worse than parent/)
  assert.match(resultNoticeText({ ...comparable, feasible: false }).comparison, /not established.*eligibility/)
  assert.match(resultNoticeText({ ...node, confirmed_mean: 0.6, confirmed_seeds: 1 }).caution, /not established/)
  assert.match(resultNoticeText({ ...node, status: 'failed', score: null }).outcome, /Evaluation failed/)
  assert.match(resultNoticeText({ ...node, status: 'aborted', score: null }, 'ru').outcome, /Завершённого результата нет/)
  assert.match(resultNoticeText({ ...node, status: 'failed', score: null, failure: 'crash' }, 'ru').outcome, /ошибка выполнения команды/)
  assert.match(resultNoticeText(node, 'ru').caution, /Нет подтверждения повторными/)
  assert.match(resultNoticeQuestion(node), /experiment #2, attempt 1.*Do not start experiments/)
  assert.match(resultNoticeQuestion({ kind: 'run' }, 'ru'), /итог этого запуска.*Не запускай/)
  assert.match(resultNoticeQuestion({ ...node, status: 'failed' }, 'ru'), /Trace и логи.*минимальное исправление.*Не запускай/)
})

test('confirmation means cannot silently become the score compared with a parent', () => {
  const row = { ...node, score: 0.3, confirmed_mean: 0.8, confirmed_seeds: 3,
    parents: [{ ...node.parents[0], comparability: 'same' }] }
  for (const language of ['en', 'ru']) {
    const result = resultNoticeText(row, language)
    assert.match(result.outcome, language === 'ru' ? /среднее повторных запусков 0,8.*Основная оценка: 0,3/ : /confirmation mean 0.8.*Evaluation score: 0.3/)
    assert.match(result.comparison, language === 'ru' ? /хуже.*а не средние/ : /worse than.*not confirmation means/)
    assert.doesNotMatch(result.comparison, /0[.,]8/)
  }
  assert.equal(resultNoticeText({ ...row, salvaged: true }).stateLabel, 'Recovered')
  assert.doesNotMatch(resultNoticeText({ ...row, salvaged: true }).comparison, /improves on/)
  assert.match(resultNoticeText({ ...row, parents: [] }).comparison, /No usable parent/)
  const meanOnly = resultNoticeText({ ...row, score: null })
  assert.match(meanOnly.comparison, /This attempt has no evaluation score/)
  assert.doesNotMatch(meanOnly.comparison, /worse than|improves on|No usable parent/)
  assert.match(resultNoticeText({ ...row, parents: [row.parents[0], { ...row.parents[0], node_id: 1 }] }).comparison, /Multiple parents/)
  assert.match(resultCaveatText('mixed_comparability', 'ru'), /условия оценки отличаются/)
  assert.match(resultCaveatText('new_flag', 'ru'), /Неизвестное ограничение: new_flag/)
})

test('incomplete, wrong generation, duplicate and invalid scalar receipts are rejected', () => {
  assert.equal(validResultNotices(payload([node]), generation), true)
  for (const value of [payload([node, node]), { ...payload([node]), generation: 'c'.repeat(64) },
    payload([{ ...node, score: Infinity }]), payload([{ ...node, node_id: true }]),
    payload([{ ...node, status: 'running' }]), payload([{ ...node, evidence_token: '' }])]) {
    assert.equal(validResultNotices(value, generation), false)
  }
})
