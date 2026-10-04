import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { mountLive, fetchStub, jsonResponse, until, click } from './_mount.js'

const generation = 'a'.repeat(64)
const step = language => ({ language, code: 'inspect_lifecycle', owner: 'external_agent',
  title: language === 'ru' ? 'Проверьте завершённый запуск' : 'Inspect recorded finish',
  detail: language === 'ru' ? 'Возобновление не предлагается. Подключение агента не измеряется.' : 'No resume is suggested. Agent connection: not measured.',
  reads: ['GET /api/runs/{run_id}/result-notices?expected_generation=TOKEN'], action: null, phase_id: null })

function selectLanguage(language) {
  localStorage.setItem('looplab.language', language)
  window.dispatchEvent(new CustomEvent('looplab:language', { detail: language }))
}

test('language changes fence late responses and cached wrong-locale advice on the main status', async () => {
  const harness = await mountLive({ visible: true })
  try {
    localStorage.removeItem('looplab.language')
    const { HarnessProgressPanel } = await harness.load('/src/HarnessProgressPanel.jsx')
    const requests = []
    let delayedRu, wrongLocale = false, legacy = false
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-progress': request => {
      requests.push(request)
      const language = request.url.searchParams.get('language')
      if (language === 'ru' && !wrongLocale && !legacy) return new Promise(resolve => { delayedRu = resolve })
      const advice = step(wrongLocale || legacy ? 'en' : language)
      if (legacy) delete advice.language
      return { generation, event_seq: 12, next_step: advice,
        execution: { engine_running: false } }
    } })
    const props = { compact: true, runId: 'demo', expectedGeneration: generation, seq: 12,
      externalMode: true, engineRunning: false }
    const view = await harness.mount(HarnessProgressPanel, props)
    await until(() => view.container.textContent.includes('Inspect recorded finish'), 'English advice')
    await React.act(async () => selectLanguage('ru'))
    await until(() => !!delayedRu, 'Russian read')
    assert.ok(!view.container.textContent.includes('Inspect recorded finish'), 'old locale must withdraw immediately')
    assert.match(view.container.textContent, /Следующий шаг недоступен/)
    assert.ok(requests.at(-1).init.signal.aborted === false)
    await React.act(async () => selectLanguage('en'))
    await until(() => view.container.textContent.includes('Inspect recorded finish'), 'new English advice')
    const oldRequest = requests.find(row => row.url.searchParams.get('language') === 'ru')
    assert.ok(oldRequest.init.signal.aborted)
    await React.act(async () => delayedRu(jsonResponse({ generation, event_seq: 12,
      next_step: step('ru'), execution: { engine_running: false } })))
    assert.ok(!view.container.textContent.includes('Проверьте завершённый запуск'), 'late old scope must not commit')
    wrongLocale = true
    await React.act(async () => selectLanguage('ru'))
    await until(() => !view.container.querySelector('button')?.disabled, 'wrong locale settles')
    assert.match(view.container.textContent, /Следующий шаг недоступен/)
    assert.ok(!view.container.textContent.includes('Inspect recorded finish'))
    legacy = true
    wrongLocale = false
    await click([...view.container.querySelectorAll('button')].find(el => el.textContent === 'Повторить чтение'))
    await until(() => view.container.textContent.includes('Inspect recorded finish'), 'explicit legacy read')
    assert.match(view.container.textContent, /Подсказка старого сервера приведена на исходном языке/)
    assert.ok(requests.every(row => row.url.searchParams.get('expected_generation') === generation))
    assert.ok(globalThis.fetch.calls.every(row => row.method === 'GET'))
  } finally {
    localStorage.removeItem('looplab.language')
    await harness.close()
  }
})

test('Russian next-step labels preserve server reads and actions; legacy wording is explicit', async () => {
  const harness = await mountLive({ visible: true })
  try {
    localStorage.setItem('looplab.language', 'ru')
    const { default: HarnessNextStep, validHarnessNextStep } = await harness.load('/src/HarnessNextStep.jsx')
    assert.ok(validHarnessNextStep(step('ru'), 'ru'))
    assert.ok(!validHarnessNextStep(step('en'), 'ru'))
    assert.ok(!validHarnessNextStep({ ...step('ru'), language: null }, 'ru'))
    assert.ok(!validHarnessNextStep({ ...step('ru'), detail: '' }, 'ru'))
    const advice = { ...step('ru'), code: 'answer_checkpoint', phase_id: 'deadline_grace',
      action: 'POST /api/runs/{run_id}/harness-checkpoints' }
    const view = await harness.mount(HarnessNextStep, { step: advice, runId: 'demo', fresh: true })
    assert.match(view.container.textContent, /Следующий шаг · Проверьте/)
    assert.match(view.container.textContent, /Ответственный: внешний агент/)
    assert.match(view.container.textContent, /POST \/api\/runs\/demo\/harness-checkpoints/)
    assert.match(view.container.textContent, /phase_info: deadline_grace/)
    const legacy = step('en')
    delete legacy.language
    await view.rerender({ step: legacy, runId: 'demo', fresh: true })
    assert.match(view.container.textContent, /Подсказка старого сервера приведена на исходном языке/)
    assert.match(view.container.textContent, /Inspect recorded finish/)
    await view.rerender({ step: advice, runId: 'demo', fresh: false })
    assert.match(view.container.textContent, /недоступен до успешного обновления/)
    assert.ok(!view.container.textContent.includes('Проверьте завершённый запуск'))
    assert.deepEqual(harness.fetch.calls, [])
  } finally {
    localStorage.removeItem('looplab.language')
    await harness.close()
  }
})
