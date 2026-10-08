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
import { russianPluralCategory, russianPluralForm, uiPlural } from '../src/uiLanguage.js'
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
    '{0} new event{1}; jump to live', '+{0} experiment node{1}', '{0} {1} {2} {3} as read{4}',
    // The suffix fragments the later conversions retired (2026-10-08).
    's are', 's remain', 's exist', 'is']) {
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

// CLDR `one` is not "exactly one": it is also 21, 31, 101… The first catalogue wrote several `one`
// forms as exactly-one sentences — a singular pronoun pointing back at the counted noun ("21 запись;
// итоги могут её учитывать", "21 рабочая задача уже существует. Она…") or no printed count at all
// ("удалён узел #1, …, #21"). `exact1` (ICU's `=1`) now carries such a sentence, and `one` must
// read right for 21.
const SINGULAR_REFERENCE = /(?<!\p{L})(он|она|оно|его|её|ее|него|неё|нее|ему|ей|ней|нему|нём|им|ним|этот|эта|это|этого|этой|этому|этим|этом|эту|тот|та|то|того|той|тому|том|ту)(?!\p{L})/giu
const references = text => new Set([...text.matchAll(SINGULAR_REFERENCE)].map(m => m[0].toLowerCase()))
function sourcePluralSlots() {
  const slots = {}
  for (const file of readdirSync(new URL('../src/', import.meta.url))) {
    if (!/\.(jsx|js)$/.test(file) || /^(uiLanguage|useAssistantLanguage|locale)/.test(file)) continue
    const result = localizeSource(readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8'), file)
    for (const [key, list] of Object.entries(result.pluralSlots)) (slots[key] ??= []).push(...list)
  }
  return slots
}
const render = (form, slot, n) => form.replace(/\{(\d+)\}/g, (_, i) => Number(i) === slot ? String(n) : `‹${i}›`)

test('exact1 is the form for exactly one; one/few/many stay CLDR for every other count', () => {
  const forms = { exact1: 'удалён узел {1}', one: 'удалены узлы {1}', few: 'удалены узлы {1}', many: 'удалены узлы {1}' }
  assert.ok(validPluralForms(forms))
  assert.equal(russianPluralForm(forms, 1), forms.exact1)
  assert.equal(russianPluralForm(forms, '1'), forms.exact1, 'a numeric string is read as its number')
  for (const n of [21, 31, 101, -1, -21]) assert.equal(russianPluralForm(forms, n), forms.one, String(n))
  assert.equal(russianPluralForm(forms, 2), forms.few)
  assert.equal(russianPluralForm(forms, 1.5), forms.few, 'a fraction still reads `few`')
  const { exact1, ...plain } = forms
  assert.equal(russianPluralForm(plain, 1), plain.one, 'with no exact1, one is the form for 1 too')
  assert.ok(!validPluralForms({ ...forms, exact1: ' ' }), 'an empty exact1 is ill-formed')
  assert.ok(!validPluralForms({ ...forms, exact2: 'x' }), 'no other explicit-count form exists')
  const base = { schema: 1, language: 'ru', count: 2, messages: { Runs: 'Запуски', Settings: 'Настройки' } }
  assert.deepEqual(decodeCatalogue({ ...base, plurals: { 'deleted nodes {1}': forms } }).plural('deleted nodes {1}'), forms)
})

test('every shipped `one` form reads right at 21 and 101: count printed, no singular back-reference', () => {
  const slots = sourcePluralSlots()
  const defects = []
  for (const [key, forms] of Object.entries(page.plurals)) {
    const keySlots = [...new Set(slots[key] || [])]
    assert.equal(keySlots.length, 1, `${key}: every call prints its count at one values index`)
    const [slot] = keySlots
    assert.notEqual(slot, null, `${key}: values must be an array literal`)
    const printed = slot >= 0 && key.includes(`{${slot}}`)
    for (const n of [21, 101]) {
      const said = render(russianPluralForm(forms, n), slot, n)
      const plural = render(forms.few, slot, n)
      // A count the sentence never prints cannot be read as 21's agreement: it reads singular.
      if (printed ? !said.includes(String(n)) : said !== plural) defects.push(`${key} @${n}: ${said}`)
      // A pronoun the plural form does not also say points back at the counted noun.
      const extra = [...references(said)].filter(word => !references(plural).has(word))
      if (extra.length) defects.push(`${key} @${n} (${extra.join(', ')}): ${said}`)
    }
  }
  assert.deepEqual(defects, [])
  // The sentences the review found, rendered as the dock / run list / Authoring now say them.
  const P = page.plurals, at = (key, n, values = [n]) => russianPluralForm(P[key], n)
    .replace(/\{(\d+)\}/g, (_, i) => String(values[Number(i)] ?? ''))
  assert.equal(at('deleted nodes {1}', 21, [21, '#1, …, #21']), 'удалены узлы #1, …, #21')
  assert.equal(at('deleted nodes {1}', 1, [1, '#3']), 'удалён узел #3')
  assert.equal(at('{0} records; totals may include them.', 21), '21 запись; итоги могут их учитывать.')
  assert.equal(at('{0} records; totals may include them.', 1), '1 запись; итоги могут её учитывать.')
  assert.equal(at('{0} drafts retained. Switching is safe; closing loses them.', 21),
    'Сохранён 21 черновик. Переключаться безопасно; при закрытии черновики будут потеряны.')
  assert.equal(at('The remaining {0} runs were not touched.', 21), 'Остальные 21 запуск не затронуты.')
  assert.equal(at('The remaining {0} runs were not touched.', 1), 'Оставшийся 1 запуск не затронут.')
  assert.equal(at('Mark all {0} unread items as read{1}', 21, [21, '']),
    'Отметить все 21 непрочитанный элемент как прочитанные')
  assert.match(at('{0} save outcomes may be unknown. Leave Authoring?', 21), /^Результаты 21 сохранения могут быть неизвестны/)
  assert.match(at('{0} save outcomes may be unknown. Leave Authoring?', 1), /^Результат 1 сохранения может быть неизвестен/)
})

test('the collector refuses a uiPlural it cannot see and English forms that disagree on placeholders', () => {
  const defects = source => localizeSource(source, 'x.js').pluralDefects
  assert.match(defects(`import { uiPlural as p } from './uiLanguage.js'\nexport const a = n => p(n, '{0} dog', '{0} dogs')\n`).join(),
    /imported under its own name/)
  assert.match(defects(`import * as L from './uiLanguage.js'\nexport const a = n => L.uiPlural(n, '{0} cat', '{0} cats')\n`).join(),
    /called by its bare name/)
  assert.match(defects(`import { uiPlural } from './uiLanguage.js'\nconst say = uiPlural\nexport const a = n => say(n, '{0} hen', '{0} hens')\n`).join(),
    /not passed as a value/)
  assert.match(defects(`import { uiPlural } from './uiLanguage.js'\nexport const a = n => uiPlural(n, '{1} ram', '{0} rams')\n`).join(),
    /different placeholders/)
  assert.deepEqual(defects(`import { uiPlural } from './uiLanguage.js'\nexport const a = n => uiPlural(n, '{0} ram', '{0} rams')\n`), [])
  const slots = source => localizeSource(source, 'x.js').pluralSlots
  assert.deepEqual(slots(`import { uiPlural } from './uiLanguage.js'\nexport const a = (n, x) => [uiPlural(n, '{0} a', '{0} as'), uiPlural(n.length, 'b {1}', 'bs {1}', [n.length, x]), uiPlural(n, '{1} c {0}', '{1} cs {0}', [x, n])]\n`),
    { '{0} as': [0], 'bs {1}': [0], '{1} cs {0}': [1] })
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

    // A count agrees with its own noun or verb, never with a neighbour's count (review of 1fd23d9):
    // the CrossRunPanel coverage line nests the runs' verb inside the groups' noun phrase.
    const coverage = (groups, runs) => locale.uiPlural(groups, '{1} in {0} comparable group', '{1} in {0} comparable groups',
      [groups, locale.uiPlural(runs, '{0} of them sit', '{0} of them sit')])
    assert.equal(coverage(3, 1), '1 из них входит в 3 сопоставимые группы')
    assert.equal(coverage(1, 5), '5 из них входят в 1 сопоставимую группу')
    assert.equal(coverage(21, 21), '21 из них входит в 21 сопоставимую группу')
    const built = n => locale.uiPlural(n, '{0} built something else — not a test of it', '{0} built something else — not a test of it')
    assert.equal(built(1), '1 собрал что-то другое — это не проверка задачи')
    assert.equal(built(3), '3 собрали что-то другое — это не проверка задачи')

    // Dock narration: no English suffix fragment left in the event line.
    const line = (type, data) => narration.eventNarration({ type, data })
    assert.equal(line('node_tombstoned', { node_ids: [3] }), 'удалён узел #3')
    assert.equal(line('node_tombstoned', { node_ids: [3, 4] }), 'удалены узлы #3, #4')
    const ids21 = Array.from({ length: 21 }, (_, i) => i + 1)
    assert.equal(line('node_tombstoned', { node_ids: ids21 }), 'удалены узлы ' + ids21.map(i => '#' + i).join(', '),
      'CLDR `one` is also 21: the count is not printed, so 21 nodes read plural')
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

// Text a helper composes in the UI language and a component then KEEPS — a memo keyed on the
// stores, a notice in state — kept the old language after a switch. The retained-work leave
// message is memoized on the language revision too; the run list's deletion notice is stored with
// its producer and composed at render. Driven RU -> EN -> RU through the real language store.
test('kept plural text follows a language switch RU -> EN -> RU', async () => {
  const harness = await mountLive(); localStorage.clear()
  const backend = fetchStub()
  globalThis.fetch = (...args) => String(args[0]).endsWith('/locales/ru.json')
    ? Promise.resolve(jsonResponse(page)) : backend(...args)
  try {
    const locale = await harness.load('/src/uiLanguage.js')
    const bulk = await harness.load('/src/bulkDeleteModel.js')
    const cascade = await harness.load('/src/memoryCascadeModel.js')
    const { useRetainedWork } = await harness.load('/src/useRetainedWork.js')
    const switchTo = language => React.act(async () => { locale.setUILanguage(language); await locale.loadUILanguage() })
    await switchTo('ru')

    // The run list's deletion notice: stored once, read at every render.
    const batch = bulk.languageFollowingNotice(() => bulk.bulkOutcomeNotice({ done: ['a', 'b'], total: 2 }))
    const purge = bulk.languageFollowingNotice(() => cascade.cascadeOutcome({ ok: true, deleted: 21 }, 'r'))
    const said = () => [bulk.deletionNoticeText(batch), bulk.deletionNoticeText(purge)]
    const russian = ['2 запуска удалены безвозвратно: “a”, “b”.',
      'Запуск удалён вместе с 21 строкой межзапусковой памяти, принадлежавшей только ему.']
    assert.deepEqual(said(), russian)
    assert.equal(batch.kind, 'status'); assert.equal(purge.retryRunId, '')
    await switchTo('en')
    assert.deepEqual(said(), ['2 runs permanently deleted: “a”, “b”.',
      'The run was deleted, along with 21 cross-run memory rows only it owned.'])
    await switchTo('ru')
    assert.deepEqual(said(), russian)
    assert.equal(bulk.languageFollowingNotice(() => null), null, 'no outcome, no notice')
    assert.equal(bulk.deletionNoticeText({ kind: 'error', text: 'plain' }), 'plain', 'a plain notice is its text')

    // The retained-work leave message: memoized, so only a memo keyed on the language follows it.
    const run = 'demo', scope = `comment-composer:${run}@gen-a:3:1`
    const store = { entries: () => [[scope, { text: 'draft' }]], readField: () => null, clear() {} }
    const Probe = () => React.createElement('p', null, useRetainedWork({
      runId: run, generation: 'gen-a', reviewMode: false, panel: null, routeFenceActive: false,
      inspectorDraftStore: store, activePanelNavigationGuard: null,
      panelNavigationGuardRef: { current: null }, setPanelNavigationGuard() {},
      commentRecoveryRevision: 0, inspectorDraftRevision: 0, onBack() {},
    }).retainedRunLeaveMessage)
    const view = await harness.mount(Probe)
    await settle()
    const ruLeave = view.container.textContent
    assert.match(ruLeave, /1 несохранённый черновик комментария покинет/)
    await switchTo('en'); await settle()
    assert.match(view.container.textContent, /1 unsaved comment draft will leave/)
    assert.doesNotMatch(view.container.textContent, /[А-Яа-яЁё]/)
    await switchTo('ru'); await settle()
    assert.equal(view.container.textContent, ruLeave)
  } finally { await harness.close() }

  // The run list stores the producer at all three sites that store a composed outcome.
  const runList = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  for (const stored of ['setDeletionNotice(bulkOutcomeNotice(', 'setDeletionNotice(cascadeOutcome(',
    'const cascade = cascadeOutcome(', '{uiText(deletionNotice.text)}'])
    assert.ok(!runList.includes(stored), stored)
})

// The glue shape itself, refused at the source: a conditional on a count being 1 that picks between
// two English WORD forms (`n === 1 ? 'item' : 'items'`, `n === 1 ? uiText(' is') : uiText('s are')`)
// is how a Russian sentence ended up with an English suffix or a fixed number form. A whole-sentence
// branch on exactly one (`n === 1 ? uiText('This is the only attempt…') : uiPlural(…)`) is allowed —
// it is a different sentence, not a glued word.
test('no source file picks an English word form by comparing a count with 1', async () => {
  const { parse } = await import('@babel/parser')
  const PAIRS = [['is', 'are'], ['was', 'were'], ['it', 'them'], ['it', 'those'], ['this', 'these'],
    ['has', 'have'], ['does', 'do'], ['one', 'all']]
  const literal = node => node?.type === 'StringLiteral' ? node.value
    : node?.type === 'CallExpression' && ['uiText', 'uiMessage'].includes(node.callee?.name)
      && node.arguments[0]?.type === 'StringLiteral' ? node.arguments[0].value : null
  const words = text => text.trim().toLowerCase().split(/\s+/)
  const glued = (a, b) => {
    if (a == null || b == null) return false
    if (/^(s|es)(\s|$)/.test(b.trim())) return true  // a bare suffix glued onto the count's noun
    const [x, y] = [words(a), words(b)]
    if (x.length !== y.length || x.length > 3) return false
    return x.some((w, i) => w !== y[i] && (y[i] === w + 's' || y[i] === w + 'es'
      || y[i] === w.replace(/y$/, 'ies') || PAIRS.some(([p, q]) => w === p && y[i] === q)))
  }
  const oneTest = t => t?.type === 'BinaryExpression' && ['===', '!==', '==', '!=', '>'].includes(t.operator)
    && [t.left, t.right].some(x => x.type === 'NumericLiteral' && x.value === 1)
  const found = []
  for (const file of readdirSync(new URL('../src/', import.meta.url))) {
    if (!/\.(jsx|js)$/.test(file)) continue
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
    const visit = node => {
      if (!node || typeof node !== 'object') return
      if (Array.isArray(node)) return node.forEach(visit)
      if (node.type === 'ConditionalExpression' && oneTest(node.test)) {
        const [a, b] = [literal(node.consequent), literal(node.alternate)]
        if (glued(a, b) || glued(b, a)) found.push(`${file}:${source.slice(0, node.start).split('\n').length} ${a} / ${b}`)
      }
      for (const [key, value] of Object.entries(node)) if (!['loc', 'extra', 'leadingComments', 'trailingComments', 'innerComments'].includes(key)) visit(value)
    }
    visit(parse(source, { sourceType: 'module', plugins: ['jsx'] }).program)
  }
  assert.deepEqual(found, [])
  // The detector sees the shapes it was written for.
  assert.ok(glued('item', 'items') && glued(' is', 's are') && glued('this experiment', 'these experiments')
    && glued('entry was', 'entries were') && !glued('created', 'edited') && !glued('single', 'ranked'))
})
