// `RunList` — 4,000-odd lines named by dozens of tests and, until doc 52 row 26, mounted by none —
// rendered through the shared harness (`_mount.js`). The gate under test is the one a source pin
// cannot see: which view the restored navigation state SELECTS, published as `aria-pressed` on the
// view buttons, and the Compare gate closed while fewer than two runs are selected.
import test from 'node:test'
import assert from 'node:assert/strict'

import { mountHarness } from './_mount.js'

let harness
let RunList

test.before(async () => {
  harness = await mountHarness({ routes: { '/api/runs': [] } })
  ;({ default: RunList } = await harness.load('/src/RunList.jsx'))
})

test.after(async () => {
  await harness?.close()
})

const viewPressed = (markup, label) => {
  const button = markup.match(new RegExp(
    `<button[^>]*aria-pressed="(true|false)"[^>]*>(?:(?!</button>).)*? ${label}</button>`, 's'))
  assert.ok(button, `the ${label} view button must render`)
  return button[1] === 'true'
}

test('RunList mounts as the Runs landmark with List selected and Compare closed on an empty list', () => {
  const markup = harness.render(RunList, { onOpen() {}, onGlobalNavigate() {} })
  assert.match(markup, /<main class="app" data-route-main="true" tabindex="-1" aria-busy="false">/)
  assert.match(markup, /<h1 class="sr-only">Runs<\/h1>/)
  assert.equal(viewPressed(markup, 'List'), true)
  assert.equal(viewPressed(markup, 'Lineage'), false)
  assert.equal(viewPressed(markup, 'Concepts'), false)
  assert.match(markup,
    /<button[^>]*aria-pressed="false"[^>]*disabled=""[^>]*title="Select at least two runs from List">Compare · 0<\/button>/,
    'the Compare view stays closed while fewer than two runs are checked')
  assert.deepEqual(harness.fetch.calls, [], 'a static render reads nothing: every fetch lives in an effect')
})

test('RunList gate flip: a restored navigation state selects the Lineage view', () => {
  const markup = harness.render(RunList, {
    onOpen() {}, onGlobalNavigate() {}, initialNavigationState: { view: 'map' },
  })
  assert.equal(viewPressed(markup, 'Lineage'), true)
  assert.equal(viewPressed(markup, 'List'), false)
})

test('RunList refuses an unknown restored view and falls back to List', () => {
  const markup = harness.render(RunList, {
    onOpen() {}, onGlobalNavigate() {}, initialNavigationState: { view: 'leaderboard' },
  })
  assert.equal(viewPressed(markup, 'List'), true)
  assert.equal(viewPressed(markup, 'Lineage'), false)
})
