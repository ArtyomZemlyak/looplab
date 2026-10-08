import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// The Russian catalogue was machine-translated, and three defect shapes survived into copy an
// operator reads: a number the English never said ("…timed out after {1}ms" read "после {1}40",
// "Shared chat loaded. {0}…" read "…9{0}…9" — a stray 9 before a placeholder), a `::` the English
// never had ("by {0}" read ":: :: {0}"), and a keyboard shortcut that stopped naming its key
// ("Press Ctrl+C" read "нажмите на KDE+C", "Esc cancels" read "ЭСК отменяет"). Each one is a
// property of a (key, value) pair alone, so it is checked over the whole catalogue here.
const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
const entries = Object.entries(catalogue.messages)

// Placeholders are user data, never copy: `{12}` is not the number 12.
const literal = text => text.replace(/\{\d+\}/g, ' ')

// A digit run in the value must occur in the key's own digits. Russian groups thousands with a
// space ("1 000 000 000 000") and uses a decimal comma in prose ("0,5"), so a run is looked up in
// the key's digit sequence rather than matched number for number.
function strayDigitRuns(key, value) {
  const keyDigits = literal(key).replace(/\D/g, '')
  return (literal(value).match(/\d+/g) || []).filter(run => !keyDigits.includes(run))
}

function strayDoubleColons(key, value) {
  const count = text => (text.match(/::/g) || []).length
  return Math.max(0, count(value) - count(key))
}

// "Enter" is also an English verb ("Enter a value", "Enter an API key", "Enter the token"); only
// the key name must survive translation. Every other token is only ever a key name in this copy.
const KEY_TOKENS = [
  ['Ctrl', /\bCtrl\b/], ['Cmd', /\bCmd\b/], ['⌘', /⌘/], ['Shift', /\bShift\b/], ['Alt', /\bAlt\b/],
  ['Tab', /\bTab\b/], ['Esc', /\bEsc\b/], ['Enter', /\bEnter\b(?!\s+(?:a|an|the)\b)/],
]
function droppedKeyTokens(key, value) {
  return KEY_TOKENS.filter(([token, pattern]) => pattern.test(key) && !value.includes(token))
    .map(([token]) => token)
}

test('the guards see the defects they were written for', () => {
  assert.deepEqual(strayDigitRuns('{0}: request timed out after {1}ms', '{0}: заявка отсрочена после {1}40'), ['40'])
  assert.deepEqual(strayDigitRuns('Shared chat loaded. {0} {1}.{2}', 'Совместный чат загружен. 9{0} {1}.9{2}'), ['9', '9'])
  assert.deepEqual(strayDigitRuns('no greater than 1,000,000,000,000 seconds.', 'не более 1 000 000 000 000 секунд.'), [])
  assert.deepEqual(strayDigitRuns('0.5 = the median', '0,5 = медиана'), [])
  assert.equal(strayDoubleColons('by {0}', ':: :: {0}'), 2)
  assert.equal(strayDoubleColons('core/evidence.py::fence', 'core/evidence.py::fence'), 0)
  assert.deepEqual(droppedKeyTokens('Payload selected. Press Ctrl+C to copy it.', 'Нажмите на KDE+C.'), ['Ctrl'])
  assert.deepEqual(droppedKeyTokens('Plain text · Ctrl/⌘+Enter posts', 'Простой текст · </,+Входящие посты'), ['Ctrl', '⌘', 'Enter'])
  assert.deepEqual(droppedKeyTokens('Plain text · Esc cancels', 'Прямой текст · ЭСК отменяет'), ['Esc'])
  assert.deepEqual(droppedKeyTokens('Enter a finite number.', 'Введите конечное число.'), [])
})

test('no Russian value invents a number its English key does not have', () => {
  const bad = entries.map(([key, value]) => [key, value, strayDigitRuns(key, value)]).filter(([, , runs]) => runs.length)
  assert.deepEqual(bad, [])
})

test('no Russian value carries a "::" its English key does not have', () => {
  assert.deepEqual(entries.filter(([key, value]) => strayDoubleColons(key, value) > 0), [])
})

test('a keyboard shortcut named in the English copy is named in the Russian copy', () => {
  const bad = entries.map(([key, value]) => [key, value, droppedKeyTokens(key, value)]).filter(([, , tokens]) => tokens.length)
  assert.deepEqual(bad, [])
})
