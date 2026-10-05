import test from 'node:test'
import assert from 'node:assert/strict'
import { click, fetchStub, mountLive, until } from './_mount.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'
import { parseRunRouteState } from '../src/runRouteState.js'

test('internal explanations and interrupted work are plain Assistant turns with visible facts', async () => {
  const harness = await mountLive()
  const { default: Results } = await harness.load('/src/AssistantResults.jsx')
  const ready = { ...node, commentary: 'Сравнение требует повторения.', commentary_source: 'assistant', commentary_status: 'published' }
  const pending = { ...node, id: 'node:3:1', node_id: 3, commentary: null, commentary_source: null, commentary_status: 'generating' }
  const failed = { ...node, id: 'node:4:1', node_id: 4, commentary: null, commentary_source: null, commentary_status: 'interrupted' }
  const backend = fetchStub({ 'GET /api/runs/demo/result-notices': payload([ready, pending, failed]) })
  globalThis.fetch = backend
  localStorage.clear(); localStorage.setItem('looplab.language', 'ru')
  const view = await harness.mount(Results, { runId: 'demo', generation })
  try {
    await until(() => view.container.querySelectorAll('article').length === 3, 'automatic explanations')
    const text = view.container.textContent
    assert.match(text, /Ассистент · интерпретация/)
    assert.doesNotMatch(text, /Внешний агент · интерпретация/)
    assert.match(text, /готовит пояснение/)
    assert.match(text, /измеренный итог сохранён.*Автоповтор выключен/)
    assert.equal(view.container.querySelectorAll('article a[href]').length, 6)
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
  } finally { await view.unmount(); await harness.close() }
})

test('completion messages reconnect once, use safe prose and withdraw stale evidence; links retain attempt', async () => {
  const harness = await mountLive()
  const { default: Results } = await harness.load('/src/AssistantResults.jsx')
  const rows = [{ ...node, commentary: '<button onclick="resume()">Do not execute me</button>' }]
  const backend = fetchStub({ 'GET /api/runs/demo/result-notices': () => payload(rows),
    'GET /api/runs/other/result-notices': () => ({ ...payload([]), generation: 'c'.repeat(64) }) })
  globalThis.fetch = backend
  localStorage.clear()
  const asked = []
  const mounted = await harness.mount(Results, { runId: 'demo', generation, onAsk: question => asked.push(question) })
  try {
    await until(() => mounted.container.querySelector('.asst-result-notice'), 'node result')
    const { container } = mounted
    assert.equal(container.querySelectorAll('article').length, 1)
    assert.match(container.textContent, /External agent · interpretation/)
    assert.match(container.textContent, /<button onclick/)
    assert.equal(container.querySelectorAll('.asst-result-commentary button').length, 0)
    const target = parseRunRouteState(container.querySelector('article a').getAttribute('href')).state
    assert.equal(target.generation, generation)
    assert.equal(target.nodeGeneration, 1)
    assert.equal(target.nodeId, 2)
    await click(container.querySelector('article button'))
    assert.match(asked[0], /experiment #2, attempt 1/)
    await mounted.rerender({ runId: 'demo', generation, onAsk: question => asked.push(question), askDisabled: true })
    assert.equal(container.querySelector('article button').disabled, true)
    await click(container.querySelector('article button'))
    assert.equal(asked.length, 1, 'cannot overwrite an existing draft')
    await mounted.unmount()
    const reopened = await harness.mount(Results, { runId: 'demo', generation })
    try {
      await until(() => reopened.container.querySelector('article'), 'reconnected results')
      assert.equal(reopened.container.querySelectorAll('article').length, 1)
      await reopened.rerender({ runId: 'other', generation: 'c'.repeat(64) })
      await until(() => /after an experiment/.test(reopened.container.textContent), 'new run context')
      assert.doesNotMatch(reopened.container.textContent, /Do not execute me|Experiment #2/)
    } finally { await reopened.unmount() }
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false, 'reading never writes chat or starts work')
  } finally {
    await harness.close()
  }
})

test('measured, stopped and run summaries separate comparison, reliability and next steps', async () => {
  const harness = await mountLive()
  const { default: Results } = await harness.load('/src/AssistantResults.jsx')
  const stopped = { ...node, id: 'node:3:0', node_id: 3, attempt: 0, status: 'aborted', score: null,
    confirmed_mean: null, parents: [], score_comparison: { version: 1, parent_count: 0, status: 'no_parent' } }
  const measured = { ...node, confirmed_mean: 0.8, confirmed_seeds: 3, score: 0.3,
    score_comparison: { version: 1, parent_count: 1, status: 'same' },
    parents: [{ ...node.parents[0], comparability: 'same' }] }
  const finished = { id: 'run', kind: 'run', status: 'finished', objective: 'accuracy', direction: 'max',
    evaluated: 1, failed: 1, selected_node: 2, attempt: 1, score: 0.3, confirmed_mean: 0.8, trust_advisory: false,
    confirmed_seeds: 3, confirmed_std: .01, caveats: ['mixed_comparability'], reason: 'done', commentary: null,
    evidence_token: 'd'.repeat(64) }
  const backend = fetchStub({ 'GET /api/runs/demo/result-notices': payload([measured, stopped, finished]) })
  globalThis.fetch = backend
  localStorage.clear(); localStorage.setItem('looplab.language', 'ru')
  const asked = []
  const mounted = await harness.mount(Results, { runId: 'demo', generation, onAsk: value => asked.push(value) })
  try {
    await until(() => mounted.container.querySelectorAll('article').length === 3, 'structured results')
    const [result, abort, run] = mounted.container.querySelectorAll('article')
    assert.match(result.textContent, /Сравнение:.*хуже.*Надёжность:.*Дальше:/)
    assert.match(result.querySelector('a').textContent, /^Открыть метрики$/)
    assert.match(abort.textContent, /Остановлен.*Завершённого результата нет/)
    const target = parseRunRouteState(abort.querySelector('a').getAttribute('href')).state
    assert.equal(target.nodeId, 3); assert.equal(target.nodeGeneration, 0); assert.equal(target.inspectTab, 'Trace')
    await click(abort.querySelector('button'))
    assert.match(asked[0], /причину остановки.*Завершённой метрики.*Не запускай/)
    assert.match(run.textContent, /условия оценки отличаются/)
    await click(run.querySelector('button'))
    assert.match(asked[1], /прочитай отчёт.*не смешивай основные оценки/)
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
  } finally { await mounted.unmount(); await harness.close() }
})

test('chat explains guarded comparison outcomes in both languages without executing work', async () => {
  const harness = await mountLive()
  const { default: Results } = await harness.load('/src/AssistantResults.jsx')
  const statuses = ['base_different', 'base_unknown', 'retargeted', 'ineligible', 'multiple_parents']
  const rows = statuses.map((status, index) => ({ ...node, id: `node:${index + 2}:1`, node_id: index + 2,
    parents: [{ ...node.parents[0], comparability: 'same' }],
    score_comparison: { version: 1, status, parent_count: status === 'multiple_parents' ? 2 : 1 } }))
  const backend = fetchStub({ 'GET /api/runs/demo/result-notices': payload(rows) })
  globalThis.fetch = backend
  try {
    for (const language of ['en', 'ru']) {
      localStorage.clear(); sessionStorage.clear(); localStorage.setItem('looplab.language', language)
      const view = await harness.mount(Results, { runId: 'demo', generation })
      try {
        await until(() => view.container.querySelectorAll('article').length === 5, 'guarded result briefs')
        const text = view.container.textContent
        assert.match(text, language === 'ru' ? /Базы кода.*отличаются/ : /code bases differ/)
        assert.match(text, language === 'ru' ? /баз кода не подтверждено/ : /code bases cannot be matched/)
        assert.match(text, language === 'ru' ? /Цель оценки изменена/ : /objective changed/)
        assert.match(text, language === 'ru' ? /обоих экспериментов/ : /both experiments/)
        assert.match(text, language === 'ru' ? /Несколько исходных/ : /Multiple parents/)
        assert.doesNotMatch(text, /improves on|лучше, чем/)
      } finally { await view.unmount() }
    }
    assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
  } finally { await harness.close() }
})
