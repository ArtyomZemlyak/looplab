import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { resultNoticeText, resultTrustAdvisoryText, validResultNotices } from '../src/resultNoticeModel.js'
import { generation, payload } from './_resultNoticesFixtures.js'
import { click, fetchStub, mountLive, until } from './_mount.js'

const fixture = JSON.parse(await readFile(new URL('../../tests/fixtures/trust_notice_cases_v1.json', import.meta.url), 'utf8'))
const rowFor = item => {
  const expected = fixture.run_expected[item.name], child = expected.selected_node === 1
  return { id: 'run', kind: 'run', status: 'finished', direction: 'min', objective: 'task metric',
    evaluated: 2, failed: 0, reason: 'done', attempt: child && item.reset ? 1 : 0,
    score: child ? .5 : 1, confirmed_mean: child ? .4 : null, confirmed_std: child ? .01 : null, confirmed_seeds: child ? 3 : null,
    evidence_token: 'b'.repeat(64), commentary: null, ...expected }
}
const summaryFor = item => {
  const row = rowFor(item)
  const selected = { node_id: row.selected_node, attempt: row.attempt, value: row.confirmed_mean ?? row.score,
    confirmed: row.confirmed_mean !== null, seeds: row.confirmed_seeds, trust_advisory: row.trust_advisory,
    score: row.score, confirmed_std: row.confirmed_std }
  return { run_id: item.name, generation, finished: true, phase: 'finished', engine_running: false,
    direction: row.direction, source_integrity: { complete: true }, best_metric_caveats: row.caveats,
    result_summary: { selected, first: item.name === 'gate_parent_hard' ? selected
      : { node_id: 0, attempt: 0, value: 1, confirmed: false, seeds: null, trust_advisory: item.parent_trust_advisory } } }
}

test('run notices warn only about the selected attempt, preserving scores and hard-caveat semantics', () => {
  for (const item of fixture.cases) {
    const row = rowFor(item)
    assert.equal(validResultNotices(payload([row]), generation), true, item.name)
    for (const language of ['en', 'ru']) {
      const text = resultNoticeText(row, language)
      assert.equal(text.outcome.includes(resultTrustAdvisoryText(language)), row.trust_advisory, item.name)
      assert.match(text.outcome, new RegExp(`#${row.selected_node}`))
      if (row.confirmed_mean !== null) assert.match(text.outcome, language === 'ru' ? /Основная оценка: 0,5/ : /Evaluation score: 0.5/)
    }
  }
})

test('missing or contradictory run evidence cannot become a clean selected result', () => {
  const row = rowFor(fixture.cases[0])
  for (const changed of [
    { trust_advisory: undefined }, { trust_advisory: null }, { trust_advisory: 0 }, { trust_advisory: 'false' },
    { selected_node: null }, { selected_node: true }, { attempt: undefined }, { score: null },
    { caveats: ['trust_flagged'], trust_advisory: false },
    { selected_node: null, attempt: null, score: null, confirmed_mean: null, confirmed_std: null, trust_advisory: true },
  ]) assert.equal(validResultNotices(payload([{ ...row, ...changed }]), generation), false)
  assert.equal(validResultNotices(payload([{ ...row, selected_node: null, attempt: null, score: null,
    confirmed_mean: null, confirmed_std: null, trust_advisory: false, caveats: [] }]), generation), true)
})

test('both Assistant result views retain selected warnings in RU/EN and withdraw them on replacement', async () => {
  const h = await mountLive()
  try {
    const { default: Card } = await h.load('/src/AssistantRunResult.jsx')
    const { default: Feed } = await h.load('/src/AssistantResults.jsx')
    const backend = fetchStub(Object.fromEntries(fixture.cases.map(item => [
      `GET /api/runs/${item.name}/result-notices`, payload([rowFor(item)])])))
    globalThis.fetch = backend
    for (const language of ['en', 'ru']) {
      localStorage.clear(); localStorage.setItem('looplab.language', language)
      let asked = 0
      const card = await h.mount(Card, { run: summaryFor(fixture.cases[0]), onAsk: () => { asked += 1 } })
      try {
        for (const item of fixture.cases) {
          await card.rerender({ run: summaryFor(item), onAsk: () => { asked += 1 } })
          const feed = await h.mount(Feed, { runId: item.name, generation, onAsk: () => { asked += 1 } })
          try {
            await until(() => feed.container.querySelector('article'), item.name)
            const warning = resultTrustAdvisoryText(language), expected = fixture.run_expected[item.name]
            assert.equal(card.container.textContent.includes(warning), expected.trust_advisory, item.name)
            assert.equal(feed.container.textContent.includes(warning), expected.trust_advisory, item.name)
            assert.ok(feed.container.textContent.includes(resultNoticeText(rowFor(item), language).outcome))
            const previous = asked
            await click(card.container.querySelector('button'))
            await click(feed.container.querySelector('article button'))
            assert.equal(asked, previous + 2)
          } finally { await feed.unmount() }
        }
      } finally { await card.unmount() }
    }
    assert.ok(backend.calls.length > 0 && backend.calls.every(call => call.method === 'GET'))
  } finally { await h.close() }
})
