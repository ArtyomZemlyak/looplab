import test from 'node:test'
import assert from 'node:assert/strict'
import React, { useState } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { readFileSync } from 'node:fs'
import { readdir } from 'node:fs/promises'
import { localizeSource } from '../scripts/localize-copy.mjs'
import { canonicalUISource } from './_localizedSource.js'
import { click, fetchStub, jsonResponse, mountLive, settle, until } from './_mount.js'

const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
const change = async (picker, value) => React.act(async () => {
  picker.value = value; picker.dispatchEvent(new window.Event('change', { bubbles: true }))
})

test('catalogue covers authored UI copy and preserves placeholders without a runtime model', async () => {
  assert.equal(catalogue.schema, 1); assert.equal(catalogue.language, 'ru')
  assert.equal(catalogue.count, Object.keys(catalogue.messages).length)
  for (const [key, value] of Object.entries(catalogue.messages)) {
    assert.ok(value.trim(), key)
    assert.deepEqual([...key.matchAll(/\{\d+\}/g)].map(m => m[0]).sort(),
      [...value.matchAll(/\{\d+\}/g)].map(m => m[0]).sort(), key)
    assert.doesNotMatch(value, /999900\d\d/)
    assert.doesNotMatch(value, /Аллах|многобожник|Вечная жизнь|Судный день|show le|show loc|(?:\b\w+[ -]){30}/u)
  }
  for (const file of await readdir(new URL('../src/', import.meta.url))) {
    if (!/\.(jsx|js)$/.test(file) || /^(uiLanguage|useAssistantLanguage|locale)/.test(file)) continue
    const result = localizeSource(readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8'), file)
    assert.equal(result.changed, false, `${file} has untranslated authored copy`)
    for (const key of result.messages) assert.ok(Object.hasOwn(catalogue.messages, key), `${file}: ${key}`)
  }
})

test('Russian exports localize authored explanation while preserving machine fields and historical prose', async () => {
  const harness = await mountLive(); localStorage.clear()
  const locale = await harness.load('/src/uiLanguage.js')
  const report = await harness.load('/src/report.js')
  const { default: ReportView } = await harness.load('/src/Report.jsx')
  globalThis.fetch = fetchStub()
  const state = { run_id: 'RAW-run', task_id: 'RAW-task', goal: 'USER goal $& /path', direction: 'min',
    phase: 'finished', best_node_id: 0, nodes: { 0: { id: 0, status: 'evaluated', feasible: true,
      operator: 'draft', metric: 0.125, parent_ids: [], idea: { params: { learning_rate: 0.001 }, rationale: 'USER rationale' } } } }
  try {
    await React.act(async () => { locale.setUILanguage('ru'); await locale.loadUILanguage() })
    const card = report.buildModelCard(state), markdown = report.toMarkdown(state)
    assert.equal(card.schema_id, 'looplab.model-card'); assert.equal(card.direction, 'min')
    assert.equal(card.goal, state.goal); assert.equal(card.champion.metric, 0.125)
    assert.deepEqual(card.champion.params, { learning_rate: 0.001 })
    assert.equal(card.champion.operator, 'draft'); assert.match(markdown, /## Итог/)
    assert.match(markdown, /RAW-run/); assert.match(markdown, /learning_rate/)
    assert.match(markdown, /0\.125/); assert.match(markdown, /Отчёт запуска LoopLab/)
    assert.doesNotMatch(markdown, /## Verdict|## First eligible metric|Optimization orientation/)
    const html = renderToStaticMarkup(React.createElement(ReportView, { state, runId: state.run_id }))
    assert.match(html, /Среднее|Итог|Выбранный результат/)
    assert.doesNotMatch(html, /No multi-seed confirmation|>Summary<|>Selected<|>Trajectory<|>Comparisons</)
    assert.doesNotMatch(html, />unconfirmed<|>not fully verified<|>first eligible result</)
    await React.act(async () => locale.setUILanguage('en'))
    assert.match(report.toMarkdown(state), /## Verdict/)
    assert.equal(report.buildModelCard(state).champion.metric, card.champion.metric)
  } finally { await harness.close() }
})

test('a failed Russian asset read offers retry and does not perform any owner or model operation', async () => {
  const harness = await mountLive(); localStorage.clear()
  const locale = await harness.load('/src/uiLanguage.js')
  const { default: Control } = await harness.load('/src/LanguageControl.jsx')
  let reads = 0
  const backend = fetchStub()
  globalThis.fetch = (...args) => String(args[0]).endsWith('/locales/ru.json')
    ? (++reads === 1 ? Promise.resolve(jsonResponse({}, 503)) : Promise.resolve(jsonResponse(catalogue)))
    : backend(...args)
  const view = await harness.mount(Control)
  try {
    await until(() => view.container.querySelector('select'), 'language picker loads')
    await change(view.container.querySelector('select'), 'ru')
    await until(() => view.container.querySelector('button'), 'visible catalogue failure')
    assert.match(view.container.textContent, /Не удалось загрузить русский интерфейс/)
    await click(view.container.querySelector('button'))
    await until(() => !view.container.querySelector('button'), 'catalogue retry succeeds')
    assert.equal(reads, 2); assert.equal(locale.uiText('Runs'), 'Запуски')
    assert.equal(backend.calls.length, 0)
  } finally { await harness.close() }
})

test('localization excludes raw code and LazyBoundary reset identity; structural projection preserves gates', () => {
  const source = `import React from 'react'; export default function Example({draft, mode}) {
    return <><LazyBoundary label="Optional section"><p>Try again</p></LazyBoundary>
    <pre>looplab run --direction min</pre><code>auto</code><textarea value={draft} />
    <button disabled={mode === 'plan'} onClick={() => execute('reset')}>Resume</button></>
  }`
  const transformed = localizeSource(source).source
  assert.match(transformed, /<LazyBoundary label="Optional section">/)
  assert.match(transformed, /<pre>looplab run --direction min<\/pre>/)
  assert.match(transformed, /value=\{draft\}/)
  const canonical = canonicalUISource(transformed)
  assert.match(canonical, /disabled=\{mode === 'plan'\} onClick=\{\(\) => execute\('reset'\)\}>Resume/)
  assert.equal(localizeSource(transformed).changed, false, 'safe reruns cannot add duplicate hooks')
})

test('RU/EN switching keeps draft, focus and user evidence; a late catalogue cannot reverse the choice', async () => {
  const harness = await mountLive()
  localStorage.clear()
  const locale = await harness.load('/src/uiLanguage.js')
  const { default: Control } = await harness.load('/src/LanguageControl.jsx')
  let release
  const backend = fetchStub()
  globalThis.fetch = async (...args) => String(args[0]).endsWith('/locales/ru.json')
    ? new Promise(resolve => { release = () => resolve(jsonResponse(catalogue)) }) : backend(...args)
  function Workspace() {
    locale.useUILanguage()
    const [draft, setDraft] = useState('USER text /raw/path {0} $&')
    return React.createElement('div', null, React.createElement(Control),
      React.createElement('h1', null, locale.uiText('Settings')),
      React.createElement('textarea', { value: draft, onChange: e => setDraft(e.target.value) }),
      React.createElement('pre', null, 'looplab run --direction min; loss=0.125'))
  }
  const view = await harness.mount(Workspace)
  try {
    await until(() => view.container.querySelector('select'), 'language picker loads')
    const input = view.container.querySelector('textarea'); input.focus()
    const picker = view.container.querySelector('select')
    await change(picker, 'ru'); await until(() => release, 'asset read')
    await change(picker, 'en'); await React.act(async () => release()); await settle()
    assert.equal(view.container.querySelector('h1').textContent, 'Settings')
    await change(picker, 'ru'); await until(() => view.container.querySelector('h1').textContent === 'Настройки', 'Russian heading')
    assert.equal(view.container.querySelector('textarea'), input)
    assert.equal(document.activeElement, input); assert.equal(input.value, 'USER text /raw/path {0} $&')
    assert.equal(view.container.querySelector('pre').textContent, 'looplab run --direction min; loss=0.125')
    assert.equal(locale.uiMessage('Runtime access for {0}', ['USER $& {1} /path']), 'Доступ во время работы: USER $& {1} /path')
    assert.equal(backend.calls.length, 0, 'public display preference never calls owner/model APIs')
    await change(picker, 'en'); assert.equal(view.container.querySelector('h1').textContent, 'Settings')
  } finally { await harness.close() }
})

test('owner language persistence serializes rapid choices with fresh revisions and only a sparse language patch', async () => {
  const harness = await mountLive(); localStorage.clear()
  const locale = await harness.load('/src/uiLanguage.js')
  const { default: Sync } = await harness.load('/src/OwnerLanguageSync.jsx')
  let saved = 'auto', revision = 1, release
  const snapshot = () => ({ settings_revision: `r${revision}`, settings: { output_language: saved, llm_model: 'RAW-model' } })
  const backend = fetchStub({
    '/api/settings': ({ method, init }) => {
      if (method === 'GET') return snapshot()
      const request = JSON.parse(init.body)
      assert.equal(request.expected_revision, `r${revision}`)
      assert.deepEqual(Object.keys(request.settings), ['output_language'])
      const accept = () => { saved = request.settings.output_language; revision++; return { ok: true, ...snapshot() } }
      if (saved === 'auto') return new Promise(resolve => { release = () => resolve(accept()) })
      return accept()
    },
  })
  globalThis.fetch = backend
  const view = await harness.mount(Sync)
  try {
    await settle(); assert.equal(backend.calls.filter(c => c.method === 'PUT').length, 0)
    await React.act(async () => locale.setUILanguage('ru')); await until(() => release, 'first save')
    await React.act(async () => locale.setUILanguage('en'))
    await React.act(async () => release())
    await until(() => saved === 'en', 'latest choice saved')
    assert.deepEqual(backend.calls.filter(c => c.method === 'PUT').map(c => JSON.parse(c.body)), [
      { settings: { output_language: 'ru' }, expected_revision: 'r1' },
      { settings: { output_language: 'en' }, expected_revision: 'r2' },
    ])
    assert.equal(view.container.querySelector('[role="alert"]'), null)
  } finally { await harness.close() }
})

test('an incomplete save is visible and retry reads current settings before writing', async () => {
  const harness = await mountLive(); localStorage.clear(); localStorage.setItem('looplab.language', 'en')
  const { default: Sync } = await harness.load('/src/OwnerLanguageSync.jsx')
  let revision = 1, valid = false
  globalThis.fetch = fetchStub({ '/api/settings': ({ method, init }) => {
    if (method === 'GET') return { settings_revision: `r${revision}`, settings: { output_language: 'auto' } }
    const body = JSON.parse(init.body); assert.equal(body.expected_revision, `r${revision}`)
    if (!valid) { revision++; return { ok: true } }
    return { ok: true, settings_revision: `r${++revision}`, settings: { output_language: 'en' } }
  } })
  const view = await harness.mount(Sync)
  try {
    await until(() => view.container.querySelector('[role="alert"]'), 'honest incomplete-save error')
    valid = true; await click(view.container.querySelector('button'))
    await until(() => !view.container.querySelector('[role="alert"]'), 'retry acknowledged')
  } finally { await harness.close() }
})
