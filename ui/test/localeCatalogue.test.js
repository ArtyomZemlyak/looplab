// The Russian catalogue's PATTERN half: a placeholder key also translates a string some helper already
// formatted. Every server message, file path and already-translated label reaches `uiText` too, so a
// key that is almost all placeholder (`{0} of {1}`, `by {0}`, `Run {0}`) must not bind prose.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { decodeCatalogue } from '../src/localeCatalogue.js'
import { localizeSource } from '../scripts/localize-copy.mjs'

const page = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
const translate = decodeCatalogue(page)

test('a short placeholder key never rewrites prose, server text or a file path', () => {
  for (const text of [
    'Best of the batch', // `{0} of {1}` read "Best от the batch"
    'by the way', // `by {0}` read ":: :: the way"
    'Waiting for the eval to finish', // AssistantBar `uiText(item.waitingFor)`: `{0} for {1}`
    'Session expired for user abc', // OwnerAuth `uiText(resource.error)`
    'src/train.py in node 3', // CodeViewer `uiText(label)`: `{0} in {1}` read "src/train.py; node 3"
    'Run the eval again', // `Run {0}`
    'Best of batch', 'Node 4 of 12',
  ]) assert.equal(translate(text), text, text)
})

test('a short placeholder key still translates a formatted value, and a long one binds prose', () => {
  // The control half: without it the test above passes by disabling patterns altogether.
  assert.equal(translate('3 of 10'), page.messages['{0} of {1}'].replace('{0}', '3').replace('{1}', '10'))
  assert.equal(translate('Run e5small-v9'), page.messages['Run {0}'].replace('{0}', 'e5small-v9'))
  assert.equal(translate('5s ago'), page.messages['{0}s ago'].replace('{0}', '5'))
  assert.equal(translate('Save failed: network down'),
    page.messages['Save failed: {0}'].replace('{0}', 'network down'))
  assert.equal(translate('Runs'), page.messages.Runs)
})

test('a shell command is never a catalogue key and is never rewritten by the localizer', () => {
  // `looplab finalize <runs>/{0}` once read `Завершить процесс <unes>/{0}` in the copyable command.
  const commands = Object.keys(page.messages).filter(key => /^(looplab |git |python |npm )/.test(key))
  assert.deepEqual(commands, [])
  const source = 'export function f(id) { return `looplab finalize <runs>/${id}` }\n'
    + 'export function g(id) { return `The run ${id} needs a finalize step` }\n'
  const result = localizeSource(source, 'runIndex.js')
  assert.ok(!result.source.includes('uiMessage("looplab finalize'), result.source)
  assert.ok(!result.messages.includes('looplab finalize <runs>/{0}'))
  // Prose returned by the same helper file is still localized, so the exemption is not vacuous.
  assert.ok(result.source.includes('uiMessage("The run {0} needs a finalize step", [id])'), result.source)
})
