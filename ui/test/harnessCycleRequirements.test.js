import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { mountLive, fetchStub, jsonResponse, until, click } from './_mount.js'

const generation = 'a'.repeat(64)
const question = { checkpoint_id: 'original-question', node_id: 2, node_generation: 0,
  phase_id: 'train_monitor', stage: '', expectation: 'original operator expectation', observation: 'original measured log' }
function progress(language = 'ru') {
  return { generation, event_seq: 12, at_node: 3, complete: true,
    next_step: { language, code: 'answer_checkpoint', owner: 'external_agent',
      title: language === 'ru' ? 'Ответьте на вопрос о тренировке' : 'Answer the training monitor',
      detail: language === 'ru' ? 'Допустимые ответы: continue, watch. Этот вопрос не разрешает abort.' : 'Allowed verdicts: continue, watch.',
      reads: ['GET /api/runs/{run_id}/harness-checkpoints'], action: null, phase_id: 'monitor' },
    source_health: { events: { read_complete: true }, ...Object.fromEntries(['decisions', 'reviews', 'checkpoints']
      .map(kind => [kind, { accepted_rows: kind === 'checkpoints' ? 1 : 0, read_complete: true }])) },
    history: Object.fromEntries(['decisions', 'reviews', 'checkpoints']
      .map(kind => [kind, { total: kind === 'checkpoints' ? 1 : 0, offset: 0, limit: 20,
        items: kind === 'checkpoints' ? [{ question, status: 'pending' }] : [], has_more: false }])),
    candidate_requirements: { effective_concepts: true, hypothesis_statement: true },
    candidate_blockers_if_expanding: [{ phase_id: 'report', action: 'command:report_generated' }],
    candidate_decisions_per_idea: { novelty: 2 }, finish_report_due: true,
    finish_reviews_due: ['skills', 'lessons'], finish_pending_nodes: [2],
    pending_checkpoint_count: 1, pending_checkpoints: [{ question }], pending_checkpoints_truncated: false }
}

test('full Russian cycle preserves evidence and hides actionable requirements on damaged and stale reads', async () => {
  const harness = await mountLive({ visible: true })
  try {
    localStorage.setItem('looplab.language', 'ru')
    const { HarnessProgressPanel } = await harness.load('/src/HarnessProgressPanel.jsx')
    let payload = progress(), failed = false
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-progress': () => jsonResponse(payload, failed ? 503 : 200) })
    const props = { runId: 'demo', expectedGeneration: generation, seq: 12, externalMode: true, configStatus: 'ready' }
    const view = await harness.mount(HarnessProgressPanel, props)
    await until(() => view.container.textContent.includes('Перед следующим экспериментом'), 'Russian requirements')
    assert.match(view.container.textContent, /Незавершённые эксперименты: 2/)
    assert.match(view.container.textContent, /Ответ монитору должен быть разрешён конкретным вопросом/)
    assert.match(view.container.textContent, /novelty: вариантов для разбора: 2/)
    assert.match(view.container.textContent, /report.*command:report_generated/)
    assert.match(view.container.textContent, /skills, lessons/)
    assert.match(view.container.textContent, /original-question/)
    assert.match(view.container.textContent, /original measured log/)
    assert.match(view.container.textContent, /ожидает ответа/)
    payload = { ...progress(), event_seq: 13, complete: false,
      source_health: { ...progress().source_health, events: { read_complete: false, corrupt_line: 14 } },
      next_step: { ...progress().next_step, code: 'inspect_sources', title: 'Проверьте неполные источники' } }
    await view.rerender({ ...props, seq: 13 })
    await until(() => view.container.textContent.includes('Проверьте неполные источники'), 'damaged source diagnostic')
    assert.match(view.container.textContent, /Актуальные требования недоступны/)
    assert.match(view.container.textContent, /повреждённые записи/)
    assert.ok(!view.container.textContent.includes('Перед следующим экспериментом'))
    assert.match(view.container.textContent, /original-question/)
    payload = { ...progress(), event_seq: 14 }
    await view.rerender({ ...props, seq: 14 })
    await until(() => view.container.textContent.includes('Перед завершением запуска'), 'recovered source')
    failed = true
    await view.rerender({ ...props, seq: 15 })
    await until(() => view.container.textContent.includes('обновление не удалось'), 'failed read')
    assert.match(view.container.textContent, /Актуальные требования недоступны/)
    assert.ok(!view.container.textContent.includes('Перед завершением запуска'))
    assert.match(view.container.textContent, /original-question/)
    failed = false
    payload = { ...progress(), event_seq: 15 }
    await click([...view.container.querySelectorAll('button')].find(el => el.textContent === 'Повторить чтение'))
    await until(() => view.container.textContent.includes('Перед завершением запуска'), 'explicit read retry')
    await React.act(async () => {
      localStorage.setItem('looplab.language', 'en')
      window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'en' }))
    })
    payload = { ...progress('en'), event_seq: 15 }
    await until(() => view.container.textContent.includes('Unavailable.'), 'wrong cached locale withheld')
    await click([...view.container.querySelectorAll('button')].find(el => el.textContent === 'Retry'))
    await until(() => view.container.textContent.includes('Before finalizing'), 'English requirements')
    assert.match(view.container.textContent, /original-question/)
    assert.ok(globalThis.fetch.calls.every(row => row.method === 'GET'))
  } finally { localStorage.removeItem('looplab.language'); await harness.close() }
})

test('incomplete full envelopes and inconsistent pages are unavailable, not empty obligations or render crashes', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { validHarnessProgress } = await harness.load('/src/harnessProgressModel.js')
    const full = progress('en')
    assert.ok(validHarnessProgress(full))
    const older = { ...full, history: Object.fromEntries(Object.entries(full.history)
      .map(([kind, page]) => [kind, { ...page, offset: 20, total: kind === 'checkpoints' ? 21 : 0 }])) }
    assert.ok(validHarnessProgress(older, 20))
    assert.equal(validHarnessProgress(older, 0), false)
    const truncated = { ...full, pending_checkpoint_count: 101, pending_checkpoints_truncated: true,
      pending_checkpoints: Array.from({ length: 100 }, (_, i) => ({ question: { ...question, checkpoint_id: `question-${i}` } })) }
    assert.ok(validHarnessProgress(truncated))
    const bad = [
      { ...full, finish_reviews_due: undefined }, { ...full, finish_report_due: undefined },
      { ...full, complete: undefined }, { ...full, pending_checkpoint_count: 2 },
      { ...full, pending_checkpoints_truncated: true },
      { ...full, pending_checkpoints: [{ question: null }] },
      { ...full, source_health: { ...full.source_health, events: { read_complete: false } } },
      { ...full, candidate_decisions_per_idea: { novelty: 0 } },
      { ...full, candidate_requirements: {} },
      ...['total', 'offset', 'limit', 'has_more'].map(key => ({ ...full,
        history: { ...full.history, checkpoints: { ...full.history.checkpoints, [key]: undefined } } })),
      { ...full, history: { ...full.history, checkpoints: { ...full.history.checkpoints, items: [{ question: null }] } } },
      { ...full, history: { ...full.history, checkpoints: { ...full.history.checkpoints, items: [{ question, answer: { reason: {} } }] } } },
    ]
    for (const value of bad) assert.equal(validHarnessProgress(value), false)
    const { HarnessProgressPanel } = await harness.load('/src/HarnessProgressPanel.jsx')
    let payload
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-progress': () => payload })
    const props = { runId: 'demo', expectedGeneration: generation, seq: 12, externalMode: true, configStatus: 'ready' }
    for (const value of [bad[0], bad[3], bad.at(-1)]) {
      payload = value
      const view = await harness.mount(HarnessProgressPanel, props)
      await until(() => view.container.textContent.includes('Agent cycle: Unavailable'), 'invalid full envelope')
      assert.ok(!view.container.textContent.includes('No knowledge reviews due'))
      assert.ok(!view.container.textContent.includes('Before another candidate'))
      await view.unmount()
    }
    assert.ok(globalThis.fetch.calls.every(row => row.method === 'GET'))
  } finally { await harness.close() }
})
