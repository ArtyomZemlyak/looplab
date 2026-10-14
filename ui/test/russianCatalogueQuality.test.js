import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

// The Russian catalogue was raw machine translation (review 2026-10-14 section 5): «Назад к бегу» for "Back to
// runs", «наружный жгут» for "external harness", «код раствора» for "solution code". The glossary
// is docs/guide/glossary.ru.md; this STOP-LIST keeps the known failure shapes from coming back.
const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
const entries = [...Object.entries(catalogue.messages)]
for (const [key, forms] of Object.entries(catalogue.plurals)) {
  for (const form of Object.values(forms)) entries.push([key, form])
}

const KNOWN_BAD = ['Назад к бегу', 'наружный жгут', 'код раствора', 'Обед не доступен', 'Остановите бегство',
  'Беги не найден', 'Здесь не бегать', 'Символ владельца', 'Кодекс / Клод', 'Клод Код', 'show noted',
  'Призрак Btn', 'Линейный холст', 'Карточное доска', 'Обзор потребностей', 'Клею...', 'Эвальный трубопровод',
  'Здание узла', 'улиочное', 'концептуальной пули', 'судебн', 'Взлом?', 'возможный награда',
  'водном знаке', 'освежает', 'Агент читает']
// Words a machine translation produced for run / claim / evidence / harness / seed / solution / fork /
// refresh / trial. Matched at a word start; `\bNo {` is "#{n}" rendered as "No {n}".
const MARKERS = /(^|[^А-Яа-яЁё])(бег|забег|бегств|пробег|пробеж|претензи|улик|жгут|семян|семечк|раствор|вилк|освеж|бухгалтерск|стюард|судебн|Клод|Кодекс)|\bNo \{/

test('no known machine-translation phrase survives in the Russian catalogue', () => {
  for (const [key, value] of entries) {
    for (const bad of KNOWN_BAD) assert.ok(!value.includes(bad), `${key} => ${value} contains «${bad}»`)
  }
})

test('no catalogue value carries a machine-translation marker word', () => {
  const hits = entries.filter(([, value]) => MARKERS.test(value))
  assert.deepEqual(hits, [], 'translate run as «запуск», claim as «утверждение», evidence as «доказательства», '
    + 'harness as «внешний агент», solution as «решение», refresh as «обновить» (docs/guide/glossary.ru.md)')
})

test('identifiers, numbering and arrows survive translation', () => {
  for (const [key, value] of entries) {
    if (key.includes('#{')) assert.ok(value.includes('#{'), `${key} => ${value} lost the # before {n}`)
    for (const arrow of ['→', '›', '←']) {
      if (key.includes(arrow)) assert.ok(value.includes(arrow), `${key} => ${value} lost ${arrow}`)
    }
    const placeholders = text => [...text.matchAll(/\{\d+\}/g)].map(m => m[0]).sort()
    assert.deepEqual(placeholders(value), placeholders(key), key)
  }
})

test('product names are not translated', () => {
  // Short UI strings only: a long help paragraph may legitimately be summarised without the name.
  for (const [key, value] of entries.filter(([key]) => key.length <= 200)) {
    for (const name of ['Codex', 'Claude Code', 'LoopLab', 'MLflow']) {
      if (key.includes(name)) assert.ok(value.includes(name), `${key} => ${value} translated ${name}`)
    }
  }
})
