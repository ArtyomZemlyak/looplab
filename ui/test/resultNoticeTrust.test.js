import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { resultNoticeText, validResultNotices } from '../src/resultNoticeModel.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'
import { click, fetchStub, mountLive, until } from './_mount.js'

const cases = JSON.parse(await readFile(new URL('../../tests/fixtures/trust_notice_cases_v1.json', import.meta.url), 'utf8')).cases
const rowFor = item => ({ ...node, node_id: 1, attempt: item.reset ? 1 : 0,
  id: `node:1:${item.reset ? 1 : 0}`, direction: 'min', score: .5, confirmed_mean: .4, confirmed_seeds: 3,
  trust_flagged: item.trust_flagged, trust_advisory: item.trust_advisory,
  parent_trust_advisory: item.parent_trust_advisory,
  score_comparison: { version: 1, parent_count: 1, status: item.comparison },
  parents: [{ node_id: 0, attempt: 0, score: 1, comparability: 'same' }] })

test('node briefs separate Trust warnings from enforced exclusion in English and Russian', () => {
  for (const item of cases) {
    const row = rowFor(item)
    assert.equal(validResultNotices(payload([row]), generation), true, item.name)
    for (const language of ['en', 'ru']) {
      const text = resultNoticeText(row, language)
      assert.equal(/Trust/.test(text.outcome), item.trust_flagged || item.trust_advisory, item.name)
      assert.equal(/Trust/.test(text.comparison), item.parent_trust_advisory, item.name)
      if (item.trust_flagged) assert.match(text.outcome, language === 'ru' ? /Исключён из отбора/ : /Excluded from selection/)
      if (item.trust_advisory) assert.match(text.outcome, language === 'ru' ? /не исключает.*Проверьте Trust/ : /does not exclude.*Review Trust/)
      assert.equal(/improves on|лучше, чем/.test(text.comparison), item.comparison === 'same', item.name)
      assert.match(text.comparison, language === 'ru' ? /а не средние повторных/ : /not confirmation means/)
      if (item.parent_trust_advisory) assert.match(text.comparison, language === 'ru' ? /не подтверждает надёжность/ : /does not establish result reliability/)
    }
  }
})

test('incomplete or contradictory Trust receipt fields refuse rather than render clean results', () => {
  for (const field of ['trust_flagged', 'trust_advisory', 'parent_trust_advisory']) {
    for (const value of [undefined, null, 0, 'false']) {
      assert.equal(validResultNotices(payload([{ ...node, [field]: value }]), generation), false)
    }
  }
  assert.equal(validResultNotices(payload([{ ...node, trust_flagged: true, trust_advisory: true }]), generation), false)
  assert.equal(validResultNotices(payload([{ ...node, parents: [], parent_trust_advisory: true,
    score_comparison: { version: 1, parent_count: 0, status: 'no_parent' } }]), generation), false)
})

test('Assistant chat displays current advisory warnings beside measured comparisons without starting work', async () => {
  const h = await mountLive()
  try {
    const { default: Results } = await h.load('/src/AssistantResults.jsx')
    const backend = fetchStub(Object.fromEntries(cases.map(item => [
      `GET /api/runs/${item.name}/result-notices`, payload([rowFor(item)])])))
    globalThis.fetch = backend
    for (const language of ['en', 'ru']) {
      localStorage.clear(); localStorage.setItem('looplab.language', language)
      for (const item of cases) {
        const asked = []
        const view = await h.mount(Results, { runId: item.name, generation, onAsk: question => asked.push(question) })
        try {
          await until(() => view.container.querySelector('article'), item.name)
          const expected = resultNoticeText(rowFor(item), language)
          assert.ok(view.container.textContent.includes(expected.outcome))
          assert.ok(view.container.textContent.includes(expected.comparison))
          await click(view.container.querySelector('article button'))
          assert.equal(asked.length, 1)
          assert.match(asked[0], language === 'ru' ? /Не запускай новые эксперименты/ : /Do not start experiments/)
        } finally { await view.unmount() }
      }
    }
    assert.ok(backend.calls.length > 0)
    assert.ok(backend.calls.every(call => call.method === 'GET'))
  } finally { await h.close() }
})
