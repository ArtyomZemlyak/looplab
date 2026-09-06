// `RunView` — the run workspace, the largest component in the tree — rendered through the shared
// harness (`_mount.js`). Before its state arrives the workspace is the "Opening run…" resource
// state; the gate under test is `reviewMode`, which must mark the root read-only at render time so
// every owner control it wraps is styled and announced as unavailable from the first paint.
import test from 'node:test'
import assert from 'node:assert/strict'

import { mountHarness } from './_mount.js'

let harness
let RunView

test.before(async () => {
  harness = await mountHarness({ routes: {} })
  ;({ default: RunView } = await harness.load('/src/RunView.jsx'))
})

test.after(async () => {
  await harness?.close()
})

test('RunView mounts in owner mode as the opening-run resource state with no review class', () => {
  const markup = harness.render(RunView, { runId: 'demo', onBack() {} })
  assert.match(markup, /^<div class="app">/)
  assert.doesNotMatch(markup, /review-mode/)
  assert.match(markup, /<main class="run-resource-state" data-route-main="true"[^>]*aria-live="polite"/)
  assert.match(markup, /<h1 id="run-state">Opening run…<\/h1>/)
  assert.match(markup, /<button class="btn sm ghost">← runs<\/button>/)
  assert.deepEqual(harness.fetch.calls, [], 'a static render reads nothing: the run state is polled from an effect')
})

test('RunView gate flip: reviewMode marks the workspace root read-only before any state arrives', () => {
  const markup = harness.render(RunView, {
    runId: 'demo', onBack() {}, reviewMode: true, reviewMeta: { id: 'review-1', scopes: ['summary'] },
  })
  assert.match(markup, /^<div class="app review-mode">/)
  assert.match(markup, /<h1 id="run-state">Opening run…<\/h1>/)
})
