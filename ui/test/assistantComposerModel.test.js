// The assistant composer's pure rules, driven directly (review 2026-09-22, UI-06). They were
// module-private in `AssistantBar.jsx` until the split, so none of them had a direct test.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  NEW_CHAT_COMPOSER_KEY, NEW_RUN_DRAFT_RE, SECRET_RE, TEXT_EXT, composerRunKey, composerUsesRun,
  newComposerDraft, normalizeComposerMode, refNodes, uiRunContext,
} from '../src/assistantComposerModel.js'

test('#N names an experiment only as a whole token', () => {
  assert.deepEqual(refNodes('see #3 and #node-12, then #3 again'), [3, 12])
  assert.deepEqual(refNodes('a colour #3498db, a fragment page#5, a word x#7, a double ##4'), [])
  assert.deepEqual(refNodes(''), [])
  assert.deepEqual(refNodes(null), [])
})

test('a draft belongs to the open run unless it is a new-run command', () => {
  assert.equal(composerUsesRun(''), false)
  assert.equal(composerUsesRun('   '), false)
  assert.equal(composerUsesRun('why did #3 fail?'), true)
  for (const draft of ['/new tune the lr', '/genesis', '/run a thing']) {
    assert.equal(NEW_RUN_DRAFT_RE.test(draft), true, draft)
    assert.equal(composerUsesRun(draft), false, draft)
  }
  assert.equal(composerUsesRun('/new', [{ name: 'a.txt' }]), true, 'an attachment ties it to the run')
  assert.equal(composerUsesRun('', [], 1), true, 'so does a file still being read')
})

test('the run key and the run-context footer', () => {
  assert.equal(composerRunKey(null), '')
  assert.equal(composerRunKey(undefined), '')
  assert.equal(composerRunKey(5), '5')
  assert.equal(uiRunContext(null, [1]), '', 'no open run, no footer')
  assert.equal(uiRunContext('r"1]\nx', [2, 4]),
    '\n\n[UI context: run "r 1  x" is open. The user refers to #2, #4; read them with run tools.'
      + ' Use run tools if relevant.]', 'the run id cannot close the bracket or the quote')
  assert.ok(uiRunContext('x'.repeat(500), []).includes('x'.repeat(200) + '"'), 'bounded at 200')
})

test('a composer draft has one shape and a mode from the shared table', () => {
  assert.equal(normalizeComposerMode('auto'), 'auto')
  assert.equal(normalizeComposerMode('bogus'), 'plan')
  assert.equal(normalizeComposerMode(undefined), 'plan')
  assert.deepEqual(newComposerDraft(), {
    input: '', files: [], pendingFileReads: 0, runScope: null, mode: 'plan',
  })
  assert.equal(newComposerDraft('acceptEdits').mode, 'acceptEdits')
  assert.equal(newComposerDraft('bogus').mode, 'plan')
  assert.notEqual(newComposerDraft().files, newComposerDraft().files, 'a fresh container per draft')
  assert.equal(NEW_CHAT_COMPOSER_KEY, '__new__')
})

test('attachments: text-like files only, and secret-looking names refused', () => {
  for (const name of ['notes.md', 'train.py', 'data.CSV', 'cfg.yaml', 'log.jsonl']) {
    assert.equal(TEXT_EXT.test(name), true, name)
  }
  for (const name of ['photo.png', 'model.bin', 'archive.zip']) {
    assert.equal(TEXT_EXT.test(name), false, name)
  }
  for (const name of ['.env', 'config/.env.local', 'server.pem', 'tls.key', 'home/id_rsa',
    'my_secret.txt', 'credentials.json']) {
    assert.equal(SECRET_RE.test(name), true, name)
  }
  for (const name of ['notes.txt', 'environment.md', 'keyboard.py']) {
    assert.equal(SECRET_RE.test(name), false, name)
  }
})
