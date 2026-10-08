// AN UNFINISHED COPY-OUT REACHES THE INBOX.
//
// `serve/attention.py` emits an `artifact_sync_unfinished` item for every `eval.artifact_sync` copy
// whose `artifact_sync_started` row has no `artifact_synced` receipt, once no engine runs. Before the
// kind was in `ATTENTION_KINDS`, `normalizeRunAttention` dropped it as an unknown kind, so the one
// place a browser operator would learn that a checkpoint never left the box said nothing.
import assert from 'node:assert/strict'
import test from 'node:test'

import { ATTENTION_KINDS, normalizeRunAttention } from '../src/attentionModel.js'

const GEN = 'a'.repeat(64)
const row = (over = {}) => ({
  id: 'd'.repeat(64), kind: 'artifact_sync_unfinished', severity: 'warning',
  run_id: 'run-1', generation: GEN, seq: 9, created: 1, node_id: 4, node_generation: 2,
  active: false, browser: false, derived: false, ...over,
})

test('the kind is known, so the item is not dropped', () => {
  assert.ok(ATTENTION_KINDS.has('artifact_sync_unfinished'))
  const item = normalizeRunAttention(row())
  assert.ok(item, 'normalizes')
  assert.equal(item.title, 'Artifact copy-out did not finish')
  assert.match(item.detail, /eval\.artifact_sync/)
})

test('it is not needs-action and deep-links to the exact node lifecycle', () => {
  const item = normalizeRunAttention(row())
  assert.equal(item.needsAction, false, 're-running a copy is the operator\'s choice')
  assert.match(item.href, /node=4/)
})

test('the server detail does not replace the copy table', () => {
  const item = normalizeRunAttention(row({ detail: 'anything the server says' }))
  assert.notEqual(item.detail, 'anything the server says')
})
