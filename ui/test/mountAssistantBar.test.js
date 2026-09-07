// `AssistantBar` — the persistent Assistant, whose coverage was source pins and an
// "it still compiles" load (`assistantBarResourceTruth.test.js`) — rendered through the shared
// harness (`_mount.js`). The gate under test is `hidden`: the bar must collapse to NOTHING, not to
// an empty dock, and its default render must expose the dock, the mode control and the composer.
import test from 'node:test'
import assert from 'node:assert/strict'

import { mountHarness } from './_mount.js'

let harness
let AssistantBar

test.before(async () => {
  harness = await mountHarness({ routes: {} })
  ;({ default: AssistantBar } = await harness.load('/src/AssistantBar.jsx'))
})

test.after(async () => {
  await harness?.close()
})

test('AssistantBar mounts with the dock, the mode control, the composer and a disabled file input', () => {
  const markup = harness.render(AssistantBar, { runId: 'demo' })
  assert.match(markup, /^<input type="file" multiple="" style="display:none" disabled=""\/>/)
  assert.match(markup, /<button class="cmdbar-ic" aria-label="Open full Assistant"/)
  assert.match(markup, /<button type="button" class="cmdbar-mode mode-plan" aria-label="Assistant mode for the next message: Plan\./)
  assert.match(markup, /<input class="cmdbar-in" aria-label="Assistant command or question" role="combobox"/)
  assert.deepEqual(harness.fetch.calls, [], 'a static render reads nothing: the session is restored from an effect')
})

test('AssistantBar gate flip: hidden renders nothing at all', () => {
  assert.equal(harness.render(AssistantBar, { runId: 'demo', hidden: true }), '')
})
