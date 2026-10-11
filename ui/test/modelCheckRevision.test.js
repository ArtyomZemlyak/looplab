// A model check speaks only for the settings it was made against (code review of doc 75 UX-16): the
// Report's paid-action note read the bare last check, so a FAILED check outlived a fixed key ("the
// last connection check failed") and a PASSED one outlived a cleared key, suppressing the toy-run
// note. Settings now reports every saved revision pair, and a check for another pair is dropped.
import test from 'node:test'
import assert from 'node:assert/strict'
import { JSDOM } from 'jsdom'

const dom = new JSDOM('<!doctype html><html><body></body></html>')
globalThis.window = dom.window
globalThis.CustomEvent = dom.window.CustomEvent

const { noteSettingsRevisions, paidActionModelNote, publishModelCheck, readModelCheck } =
  await import('../src/modelConnection.js')

test('a check made against other settings stops speaking for the saved ones', () => {
  const seen = []
  window.addEventListener('ll:model-check', event => seen.push(event.detail))
  publishModelCheck('s1', 'k1', 'failed')
  assert.match(paidActionModelNote('llm', readModelCheck()), /last connection check failed/)
  noteSettingsRevisions('s1', 'k1')
  assert.equal(readModelCheck()?.outcome, 'failed', 'the same settings: the check still stands')
  noteSettingsRevisions('s2', 'k1')                          // the user fixed the key and saved
  assert.equal(readModelCheck(), null)
  assert.equal(seen.at(-1), null, 'listeners (Report, first-run status) hear the drop')
  assert.equal(paidActionModelNote('llm', readModelCheck()), '', 'no stale "check failed" note')

  publishModelCheck('s2', 'k1', 'passed')
  assert.equal(paidActionModelNote('toy', readModelCheck()), '')
  noteSettingsRevisions('s2', 'k2')                          // the key was cleared
  assert.match(paidActionModelNote('toy', readModelCheck()), /Needs a model: this run used none/)
})
