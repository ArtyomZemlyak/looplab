// Plural copy (`uiLanguage.js::uiPlural`). English source glued `s` onto a placeholder
// (`{0} run{1}`, `deleted node{0}`), which Russian cannot carry: it has three integer forms
// (1 запуск, 2 запуска, 5 запусков; 21 запуск, 11 запусков), so the Russian copy read English
// fragments or the wrong grammatical number. The Russian forms now live in the catalogue's `plurals`
// section, keyed by the English `other` text, and the collector refuses a missing/unused/ill-formed one.
import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { readFileSync, readdirSync } from 'node:fs'
import { decodeCatalogue, validPluralForms } from '../src/localeCatalogue.js'
import { russianPluralCategory, uiPlural } from '../src/uiLanguage.js'
import { localizeSource } from '../scripts/localize-copy.mjs'
import { fetchStub, jsonResponse, mountLive, settle } from './_mount.js'

const page = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
const placeholders = text => [...text.matchAll(/\{\d+\}/g)].map(m => m[0]).sort()

test('the Russian plural selector follows CLDR one/few/many on integers', () => {
  const table = { 0: 'many', 1: 'one', 2: 'few', 5: 'many', 11: 'many', 21: 'one', 22: 'few',
    25: 'many', 111: 'many', 12: 'many', 14: 'many', 101: 'one', 104: 'few', 1000: 'many' }
  for (const [n, category] of Object.entries(table)) assert.equal(russianPluralCategory(Number(n)), category, n)
  assert.equal(russianPluralCategory(1.5), 'other', 'a fraction is CLDR `other`')
  assert.equal(russianPluralCategory(-21), 'one')
  assert.equal(russianPluralCategory('22'), 'few', 'a numeric string is read as its number')
  // The control: the hand-written rule IS the platform's CLDR rule over a wide integer range.
  const rules = new Intl.PluralRules('ru')
  if (rules.resolvedOptions().locale.startsWith('ru')) {
    for (let n = 0; n <= 1200; n++) assert.equal(russianPluralCategory(n), rules.select(n), String(n))
  }
})

test('English output is the old `n === 1 ? "" : "s"` choice, byte for byte', () => {
  assert.equal(uiPlural(1, '{0} run', '{0} runs'), '1 run')
  assert.equal(uiPlural(0, '{0} run', '{0} runs'), '0 runs')
  assert.equal(uiPlural(21, '{0} run', '{0} runs'), '21 runs')
  assert.equal(uiPlural(2, 'deleted node {1}', 'deleted nodes {1}', [2, '#3, #4']), 'deleted nodes #3, #4')
  assert.equal(uiPlural(1, ' {0} run is shown.', ' {0} runs are shown.'), ' 1 run is shown.',
    'the source edges are kept')
})

test('the catalogue decoder validates plural forms and the translator looks them up', () => {
  const base = { schema: 1, language: 'ru', count: 2, messages: { Runs: 'Запуски', Settings: 'Настройки' } }
  const forms = { one: '{0} запуск', few: '{0} запуска', many: '{0} запусков' }
  const translate = decodeCatalogue({ ...base, plurals: { '{0} runs': forms } })
  assert.deepEqual(translate.plural('{0} runs'), forms)
  assert.equal(translate.plural('{0} nodes'), undefined)
  assert.equal(translate.plural('toString'), undefined, 'an inherited key is not an entry')
  assert.equal(typeof decodeCatalogue(base).plural, 'function', 'the section is optional')
  for (const bad of [[], 'x', { '{0} runs': { one: 'a', few: 'b' } },
    { '{0} runs': { ...forms, two: 'x' } }, { '{0} runs': { ...forms, many: '' } }]) {
    assert.throws(() => decodeCatalogue({ ...base, plurals: bad }), /Invalid language catalogue/)
  }
})

test('every shipped plural entry is well formed and named by exactly one live uiPlural key', () => {
  const plurals = page.plurals || {}
  assert.ok(Object.keys(plurals).length > 100)
  for (const [key, forms] of Object.entries(plurals)) {
    assert.ok(validPluralForms(forms), key)
    for (const form of Object.values(forms)) {
      assert.deepEqual(placeholders(form), placeholders(key), `${key} -> ${form}`)
      assert.doesNotMatch(form, /[а-яё]\((?:ы|и|а|я|е|ов|ей|ие|ые)\)/u, `${key}: no "(ы)"-style glued form`)
    }
  }
  const used = new Set()
  for (const file of readdirSync(new URL('../src/', import.meta.url))) {
    if (!/\.(jsx|js)$/.test(file) || /^(uiLanguage|useAssistantLanguage|locale)/.test(file)) continue
    const result = localizeSource(readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8'), file)
    assert.deepEqual(result.pluralDefects, [], file)
    for (const key of result.plurals) {
      used.add(key)
      assert.ok(Object.hasOwn(plurals, key), `${file}: plural "${key}" has no Russian forms`)
    }
  }
  assert.deepEqual(Object.keys(plurals).filter(key => !used.has(key)), [], 'unused plural entries')
  // The glued keys the converted sites used are gone: each one is now a plural entry instead.
  for (const glued of ['{0} run{1} of task {2}', 'deleted node{0} {1}', '{0} {1}{2} marked as read.{3}',
    'Shared chat loaded. {0} {1}.{2}', '{0} permanently deleted: {1}.{2}{3}', '{0} experiment{1}',
    '{0} pending Assistant approval{1}', 'memory: {0} settled skill{1} promoted', '{0} span{1}',
    '{0} new event{1}; jump to live', '+{0} experiment node{1}', '{0} {1} {2} {3} as read{4}']) {
    assert.ok(!Object.hasOwn(page.messages, glued), glued)
  }
})

test('the collector reads uiPlural as ONE plural key, also inside a localized call, and refuses dynamic forms', () => {
  const source = `import { uiMessage, uiPlural } from './uiLanguage.js'
export const a = n => uiPlural(n, '{0} lesson', '{0} lessons')
export const b = n => uiMessage('memory: {0}', [uiPlural(n, '{0} skill', '{0} skills')])
export const c = (n, w) => uiPlural(n, w, w + 's')\n`
  const result = localizeSource(source, 'narration.js')
  assert.deepEqual(result.plurals.sort(), ['{0} lessons', '{0} skills'])
  assert.ok(!result.messages.includes('{0} lesson') && !result.messages.includes('{0} lessons'))
  assert.equal(result.pluralDefects.length, 1)
  assert.equal(result.changed, false)
})

test('converted call sites render correct Russian number forms', async () => {
  const harness = await mountLive(); localStorage.clear()
  const backend = fetchStub()
  globalThis.fetch = (...args) => String(args[0]).endsWith('/locales/ru.json')
    ? Promise.resolve(jsonResponse(page)) : backend(...args)
  try {
    const locale = await harness.load('/src/uiLanguage.js')
    const bulk = await harness.load('/src/bulkDeleteModel.js')
    const cascade = await harness.load('/src/memoryCascadeModel.js')
    const narration = await harness.load('/src/narration.js')
    const extra = await harness.load('/src/extraMetrics.js')
    const { MetricLines } = await harness.load('/src/MetricLines.jsx')
    await React.act(async () => { locale.setUILanguage('ru'); await locale.loadUILanguage() })
    const runs = n => locale.uiPlural(n, '{0} run', '{0} runs')
    assert.deepEqual([0, 1, 2, 5, 11, 21, 22, 25, 111].map(runs), ['0 запусков', '1 запуск', '2 запуска',
      '5 запусков', '11 запусков', '21 запуск', '22 запуска', '25 запусков', '111 запусков'])
    assert.equal(locale.uiPlural(1.5, '{0} run', '{0} runs'), '1.5 запуска', 'a fraction reads `few`')
    // A source with edge whitespace (a JSX sentence continuing a paragraph) finds its trimmed key.
    assert.equal(locale.uiPlural(5, ' {0} memo', ' {0} memos '), ' 5 записок ')

    // Run list: the bulk-deletion receipt (it used to read "3 runs безвозвратно удалены…").
    const ready = ids => ({ ready: ids.map(runId => ({ runId })), blocked: [] })
    assert.equal(bulk.bulkDeletionSummary(ready(['a', 'b'])), 'Удалить 2 запуска.')
    assert.equal(bulk.bulkDeletionSummary(ready(['a', 'b', 'c', 'd', 'e'])), 'Удалить 5 запусков.')
    assert.equal(bulk.bulkOutcomeNotice({ done: ['a'], total: 1 }).text, '1 запуск удалён безвозвратно: “a”.')
    const many = bulk.bulkOutcomeNotice({ done: ['a', 'b', 'c', 'd', 'e', 'f', 'g'], total: 7 }).text
    assert.equal(many, '7 запусков удалены безвозвратно: “a”, “b”, “c”, “d”, “e” и ещё 2.')
    assert.equal(cascade.cascadeLabel({ deletable: 21, kept: 0 }),
      'Также удалить собственную межзапусковую память этого запуска (21 строка)')

    // Dock narration: no English suffix fragment left in the event line.
    const line = (type, data) => narration.eventNarration({ type, data })
    assert.equal(line('node_tombstoned', { node_ids: [3] }), 'удалён узел #3')
    assert.equal(line('node_tombstoned', { node_ids: [3, 4] }), 'удалены узлы #3, #4')
    assert.equal(line('skills_promoted', { count: 3 }), 'память: повышено 3 устоявшихся навыка')
    assert.equal(line('reflection_note', { n_lessons: 5, n_skills: 1 }), 'память: 5 уроков, 1 навык')
    assert.equal(line('budget_extend', { add_nodes: 11 }), 'бюджет запуска расширен — +11 узлов эксперимента')

    // A reconstructed metric's help is translated sentence by sentence, precision note included.
    const node = { extra_metrics: { k: 1 }, extra_metrics_provenance: { k: 'declared' },
      extra_metrics_backfill: { backfilled: true, keys: ['k'], sources: { k: 'svc' }, precision_decimals: { k: 2 } } }
    const help = extra.extraMetricSourceHelp(node, 'k')
    assert.doesNotMatch(help, /[A-Za-z]{4,} [a-z]{3,}/, help)
    assert.match(help, /источник: svc/); assert.match(help, /точность может быть грубее/)
    assert.match(help, /до 2 знаков после запятой/)

    // A rendered component: the metric-group count.
    const series = { 'train/loss': [{ step: 1, value: 1 }], 'train/acc': [{ step: 1, value: 1 }] }
    const view = await harness.mount(MetricLines, { series })
    await settle()
    assert.match(view.container.textContent, /train · 2 метрики/)
    assert.doesNotMatch(view.container.textContent, /metric/)
  } finally { await harness.close() }
})
