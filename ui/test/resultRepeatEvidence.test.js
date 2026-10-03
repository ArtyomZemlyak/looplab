import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { resultSpreadText } from '../src/resultMeasurement.js'
import { resultNoticeText, validResultNotices } from '../src/resultNoticeModel.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'
import { fetchStub, mountLive, until } from './_mount.js'

const cases = JSON.parse(await readFile(new URL('../../tests/fixtures/repeat_result_cases_v1.json', import.meta.url), 'utf8')).cases
const nodeFor = item => ({ ...node, id: `node:1:${item.reset ? 1 : 0}`, node_id: 1, attempt: item.reset ? 1 : 0,
  direction: 'min', objective: item.retarget ? 'latency' : 'task metric', score: item.score,
  confirmed_mean: item.confirmed ? .8 : null, confirmed_std: item.confirmed ? item.std : null,
  confirmed_seeds: item.confirmed ? item.seeds : null,
  score_comparison: { version: 1, parent_count: 1, status: item.comparison },
  parents: [{ node_id: 0, attempt: 0, score: item.parent_score, comparability: 'same' }] })
const runFor = item => {
  const selectedChild = item.selected_node === 1, measured = nodeFor(item)
  return { id: 'run', kind: 'run', status: 'finished', evaluated: 2, failed: 0, direction: 'min',
    objective: measured.objective, selected_node: item.selected_node, attempt: selectedChild ? measured.attempt : 0,
    score: selectedChild ? item.score : item.parent_score, confirmed_mean: selectedChild ? measured.confirmed_mean : null,
    confirmed_std: selectedChild ? measured.confirmed_std : null, confirmed_seeds: selectedChild ? measured.confirmed_seeds : null,
    trust_advisory: false, caveats: item.retarget ? ['retargeted_objective'] : [], reason: 'done',
    evidence_token: 'c'.repeat(64), commentary: null }
}
const summaryFor = item => {
  const run = runFor(item)
  return { run_id: item.name, generation, phase: 'finished', finished: true, engine_running: false,
    direction: 'min', objective_key: item.retarget ? 'latency' : '', source_integrity: { complete: true },
    best_metric_caveats: run.caveats, result_summary: {
      first: { node_id: 0, attempt: 0, value: item.parent_score, score: item.parent_score,
        confirmed: false, seeds: null, confirmed_std: null, trust_advisory: false },
      selected: { node_id: run.selected_node, attempt: run.attempt, value: run.confirmed_mean ?? run.score,
        score: run.score, confirmed: run.confirmed_mean !== null, seeds: run.confirmed_seeds,
        confirmed_std: run.confirmed_std, trust_advisory: false },
    } }
}

test('zero spread is recorded evidence; absent or invalid spread never becomes zero', () => {
  assert.equal(resultSpreadText(0), 'Spread (std): 0.')
  assert.equal(resultSpreadText(.1, 'ru'), 'Разброс (std): 0,1.')
  for (const value of [null, undefined, true, false, '0', -1, NaN, Infinity]) {
    assert.equal(resultSpreadText(value), 'Spread not recorded.')
    assert.equal(resultSpreadText(value, 'ru'), 'Разброс не записан.')
  }
})

test('repeat receipts retain primary-score comparisons and explicit spread in both languages', () => {
  for (const item of cases) {
    const rows = [nodeFor(item), runFor(item)]
    assert.equal(validResultNotices(payload(rows), generation), true, item.name)
    for (const language of ['en', 'ru']) {
      const brief = resultNoticeText(rows[0], language)
      if (item.confirmed) {
        assert.ok(brief.caution.includes(resultSpreadText(item.std, language)))
        assert.match(brief.outcome, language === 'ru' ? /среднее повторных запусков 0,8.*Основная оценка: 1,2/ : /confirmation mean 0.8.*Evaluation score: 1.2/)
        if (item.seeds === 1) assert.match(brief.caution, language === 'ru' ? /повторов не подтверждены/ : /repeats not established/)
      } else assert.doesNotMatch(brief.caution, /Spread|Разброс/)
      if (item.comparison === 'same' && item.score > item.parent_score)
        assert.match(brief.comparison, language === 'ru' ? /хуже, чем/ : /is worse than/)
      if (item.comparison !== 'same') assert.doesNotMatch(brief.comparison, /improves on|лучше, чем/)
    }
  }
  for (const std of [undefined, true, '0', -1, Infinity, NaN])
    assert.equal(validResultNotices(payload([{ ...nodeFor(cases[0]), confirmed_std: std }]), generation), false)
  assert.equal(validResultNotices(payload([{ ...node, confirmed_std: 0 }]), generation), false)
})

test('chat and run card expose primary scores and spread without treating means as improvements', async () => {
  const h = await mountLive()
  try {
    const { default: Card } = await h.load('/src/AssistantRunResult.jsx')
    const { default: Feed } = await h.load('/src/AssistantResults.jsx')
    const backend = fetchStub(Object.fromEntries(cases.map(item => [
      `GET /api/runs/${item.name}/result-notices`, payload([nodeFor(item), runFor(item)])])))
    globalThis.fetch = backend
    for (const language of ['en', 'ru']) {
      localStorage.clear(); localStorage.setItem('looplab.language', language)
      for (const item of cases) {
        const card = await h.mount(Card, { run: summaryFor(item) })
        const feed = await h.mount(Feed, { runId: item.name, generation })
        try {
          await until(() => feed.container.querySelectorAll('article').length === 2, item.name)
          if (item.confirmed) {
            assert.ok(card.container.textContent.includes(resultSpreadText(item.std, language)))
            assert.match(card.container.querySelectorAll('dd')[1].textContent, language === 'ru' ? /0.8.*Основная оценка: 1.2/ : /0.8.*Evaluation score: 1.2/)
          } else assert.doesNotMatch(card.container.textContent, /Spread \(std\)|Разброс \(std\)/)
          for (const row of [nodeFor(item), runFor(item)])
            assert.ok(feed.container.textContent.includes(resultNoticeText(row, language).caution))
          assert.doesNotMatch(card.container.textContent, /Improvement:|improves on|Улучшение:/)
        } finally { await card.unmount(); await feed.unmount() }
      }
    }
    assert.ok(backend.calls.length > 0 && backend.calls.every(call => call.method === 'GET'))
  } finally { await h.close() }
})

test('legacy summary metadata never invents an evaluation score or a zero spread', async () => {
  const h = await mountLive()
  try {
    const { default: Card } = await h.load('/src/AssistantRunResult.jsx')
    localStorage.clear(); localStorage.setItem('looplab.language', 'en')
    const legacy = summaryFor(cases[0])
    const view = await h.mount(Card, { run: legacy })
    try {
      for (const value of [undefined, null, true, '1.2', NaN, Infinity]) {
        await view.rerender({ run: { ...legacy, result_summary: { ...legacy.result_summary,
          selected: { ...legacy.result_summary.selected, score: value, confirmed_std: value } } } })
        assert.match(view.container.textContent, /confirmation mean.*0.8.*Evaluation score not recorded/)
        assert.match(view.container.textContent, /Spread not recorded/)
        assert.doesNotMatch(view.container.textContent, /Spread \(std\): 0|Evaluation score: (true|1.2|Infinity)/)
      }
      assert.equal(h.fetch.calls.length, 0)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
