import test from 'node:test'
import assert from 'node:assert/strict'
import { resultCaveatText, resultNoticeQuestion, resultNoticeText, validResultNotices } from '../src/resultNoticeModel.js'

import { generation, node, payload } from './_resultNoticesFixtures.js'

test('numbers do not become an improvement without comparison evidence; both directions and constraints', () => {
  assert.match(resultNoticeText(node).comparison, /improvement not established.*conditions are not established/)
  assert.match(resultNoticeText(node).caution, /exploratory/)
  assert.match(resultNoticeText(node, 'ru').comparison, /улучшение не установлено/)
  const comparable = { ...node, score_comparison: { version: 1, parent_count: 1, status: 'same' }, parents: [{ ...node.parents[0], comparability: 'same' }] }
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
    score_comparison: { version: 1, parent_count: 1, status: 'same' },
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

test('chat comparisons require explicit single-parent evidence, including in Russian', () => {
  const same = { ...node, score_comparison: { version: 1, parent_count: 1, status: 'same' },
    parents: [{ ...node.parents[0], comparability: 'same' }] }
  assert.equal(validResultNotices(payload([same]), generation), true)
  for (const [status, en, ru] of [
    ['base_different', /code bases differ/, /Базы кода.*отличаются/],
    ['base_unknown', /code bases cannot be matched/, /баз кода не подтверждено/],
    ['retargeted', /objective changed/, /Цель оценки изменена/],
    ['ineligible', /both experiments/, /обоих экспериментов/],
  ]) {
    const row = { ...same, score_comparison: { ...same.score_comparison, status } }
    assert.equal(validResultNotices(payload([row]), generation), true)
    for (const [language, reason] of [['en', en], ['ru', ru]]) {
      assert.match(resultNoticeText(row, language).comparison, reason)
      assert.doesNotMatch(resultNoticeText(row, language).comparison, /improves on|лучше, чем/)
    }
  }
  const merge = { ...same, score_comparison: { version: 1, parent_count: 2, status: 'multiple_parents' } }
  assert.equal(validResultNotices(payload([merge]), generation), true)
  assert.match(resultNoticeText(merge).comparison, /Multiple parents/)
  assert.match(resultNoticeText(merge, 'ru').comparison, /Несколько исходных/)
  for (const changes of [
    { score_comparison: undefined }, { score_comparison: { ...same.score_comparison, version: true } },
    { score_comparison: { ...same.score_comparison, parent_count: 2 } },
    { score_comparison: { ...same.score_comparison, status: 'future' } },
    { parents: [] }, { parents: [same.parents[0], same.parents[0]] },
    { feasible: false }, { salvaged: true }, { trust_flagged: true },
    { score: 1e308, parents: [{ ...same.parents[0], score: -1e308 }] },
  ]) {
    const row = { ...same, ...changes }
    assert.equal(validResultNotices(payload([row]), generation), false)
    assert.doesNotMatch(resultNoticeText(row).comparison, /improves on/)
  }
})

test('incomplete, wrong generation, duplicate and invalid scalar receipts are rejected', () => {
  assert.equal(validResultNotices(payload([node]), generation), true)
  for (const value of [payload([node, node]), { ...payload([node]), generation: 'c'.repeat(64) },
    payload([{ ...node, score: Infinity }]), payload([{ ...node, node_id: true }]),
    payload([{ ...node, status: 'running' }]), payload([{ ...node, evidence_token: '' }])]) {
    assert.equal(validResultNotices(value, generation), false)
  }
})

test('result pages require explicit pagination and a cursor bound to the oldest returned evidence', () => {
  const scope = 'd'.repeat(64)
  const cursor = `rn1.${scope}.${node.id}.${node.evidence_token}`
  const page = { ...payload([node]), total: 205, has_more: true, next_cursor: cursor }
  assert.equal(validResultNotices(page, generation), true)
  for (const changed of [
    { ...page, next_cursor: undefined }, { ...page, next_cursor: null }, { ...page, next_cursor: 'bad' },
    { ...page, next_cursor: cursor.replace(node.evidence_token, 'e'.repeat(64)) },
    { ...page, next_cursor: cursor.replace('node:2:1', 'node:1:1') },
    { ...page, items: [] }, { ...page, total: 1 }, { ...page, has_more: false },
    { ...payload([node]), next_cursor: undefined },
    { ...payload([node]), total: 205 },
  ]) assert.equal(validResultNotices(changed, generation), false)
  const missing = payload([node]); delete missing.next_cursor
  assert.equal(validResultNotices(missing, generation), false)
  assert.equal(validResultNotices(page, generation, cursor), false, 'older reads cannot loop back to their input cursor')
  const input = `rn1.${'e'.repeat(64)}.node:5:0.${'f'.repeat(64)}`
  assert.equal(validResultNotices(page, generation, input), false, 'cursor scope cannot change across pages')
  assert.equal(validResultNotices(payload([node]), generation, cursor), false, 'input anchor is exclusive')
  assert.equal(validResultNotices(payload([null]), generation, cursor), false, 'malformed rows refuse without throwing')
  assert.equal(validResultNotices(payload([{ ...node, parents: [null] }]), generation), false)
  assert.equal(validResultNotices({ ...payload([node]), total: 205 }, generation,
    `rn1.${scope}.node:5:0.${'f'.repeat(64)}`), true, 'oldest page still has a larger run total')
  const oversized = Array.from({ length: 51 }, (_, id) => ({ ...node, node_id: id, id: `node:${id}:1` }))
  assert.equal(validResultNotices(payload(oversized), generation, null, 50), false, 'respect the requested page bound')
})
