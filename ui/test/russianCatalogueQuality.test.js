// Defects of the Russian catalogue itself, as opposed to render sites that skip `uiText`
// (`russianRenderSites.test.js`). Found reading the offline demo in Russian in a real browser
// (2026-10-11): the Report's chart axis read «unde id ¶», its legend «- границы: 1.343», the
// operator line «подниматься вокруг холмов»; ten entries carried literal `&quot;` (React prints an
// entity as-is), among them a paid-action notice that read «может быть запрошено обвинение».
//
// The catalogue still holds machine-translated text a reader would trip over, mostly in Settings
// descriptions; the doc 75 registry tracks that backlog. What is pinned here is what a rule can
// state: an entity never belongs in a value, and the entries fixed here stay fixed.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const { messages } = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
const ENTITY = /&(?:quot|amp|lt|gt|apos|nbsp|#\d+);/

test('no Russian entry carries an HTML entity its English key does not', () => {
  const offenders = Object.entries(messages)
    .filter(([key, value]) => ENTITY.test(value) && !ENTITY.test(key))
    .map(([key]) => key)
  assert.deepEqual(offenders, [])
})

test('the Report chart and verdict read as Russian, not as a garbled translation', () => {
  const expected = {
    'node id →': 'номер узла →',
    'node id': 'номер узла',
    'eligible frontier:': 'лучшее среди допустимых:',
    'improve — hill-climb around best': 'improve — локальный поиск рядом с лучшим',
    'better evaluation score': 'оценка лучше',
    'Run loading · Assistant paused': 'Запуск загружается · Ассистент приостановлен',
  }
  for (const [key, value] of Object.entries(expected)) assert.equal(messages[key], value, key)
  const garbled = /unde id|¶|вокруг холмов|^- границы|обвинение|галстук/
  for (const [key, value] of Object.entries(messages))
    if (!garbled.test(key)) assert.doesNotMatch(value, garbled, key)
})

test('a sub-option label keeps its ↳ marker in Russian', () => {
  // The Settings form draws a dependent option as "↳ <label>" under the one it refines; 31 Russian
  // labels had turned the marker into ", " (and their words into machine translation), so the
  // dependency vanished from the form (2026-10-11).
  const lost = Object.entries(messages).filter(([key, value]) => key.startsWith('↳') && !value.startsWith('↳ '))
  assert.deepEqual(lost.map(([key]) => key), [])
})

test('the essential Settings fields read as Russian (doc 75 UX-36)', () => {
  // What Settings shows before "Show all": the labels a first-run user sets the model and the
  // budgets with. «Параллельные мероприятия в рамках ЛММ», «Макс. часы работы в стене» and a
  // help text opening «С задницей игрушки» ("With backend=toy") were there on 2026-10-11.
  const expected = {
    'Eval parallel': 'Параллельные оценки',
    'LLM parallel': 'Параллельные вызовы LLM',
    'Max wall-clock (s)': 'Макс. реальное время (с)',
    'LLM token limit': 'Предел токенов LLM',
    'Per-eval timeout (s)': 'Тайм-аут одной оценки (с)',
    'Role backend': 'Исполнитель ролей',
    'Stream requests': 'Потоковые запросы',
  }
  for (const [key, value] of Object.entries(expected)) assert.equal(messages[key], value, key)
  const garbled = /задниц|ЛММ|в стене|JOSON|STRAEAM|Уловка|Почетный доверенный/
  for (const [key, value] of Object.entries(messages))
    if (!garbled.test(key)) assert.doesNotMatch(value, garbled, key)
})
