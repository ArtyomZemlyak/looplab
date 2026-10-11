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
