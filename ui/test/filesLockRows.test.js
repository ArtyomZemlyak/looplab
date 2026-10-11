import assert from 'node:assert/strict'
import test from 'node:test'

import React from 'react'

import { fetchStub, mountLive, settle } from './_mount.js'

// Measured 2026-10-11 on the offline demo: Lab → Files opened on 30 rows, 8 of them the engine's
// empty coordination locks (`engine.lock`, `events.jsonl.lock`, `.spans-writer.lock`, …), listed
// between the config snapshot and the event log a user came to read.

const GENERATION = 'c'.repeat(64)
const runRoot = { id: 'run', label: 'run directory', path: '/runs/demo', is_run_dir: true,
  truncated: false, visibility: 'run_workspace' }

let harness
let isRunLockFile
let ArtifactsPanel

test.before(async () => {
  harness = await mountLive({ visible: true })
  ;({ isRunLockFile, ArtifactsPanel } = await harness.load('/src/panels.jsx'))
})

test.after(async () => {
  await harness?.close()
})

test('only the run root’s top-level, empty *.lock files are engine locks', () => {
  assert.equal(isRunLockFile(runRoot, { path: 'events.jsonl.lock', size: 0 }), true)
  assert.equal(isRunLockFile(runRoot, { path: '.spans-writer.lock', size: 0 }), true)
  assert.equal(isRunLockFile(runRoot, { path: 'engine.lock', size: 3 }), false, 'a lock with content is shown')
  assert.equal(isRunLockFile(runRoot, { path: 'nodes/node_1/poetry.lock', size: 0 }), false,
    'a node’s own *.lock is the candidate’s file')
  assert.equal(isRunLockFile({ ...runRoot, is_run_dir: false }, { path: 'x.lock', size: 0 }), false,
    'a declared task path is the operator’s tree')
  assert.equal(isRunLockFile(runRoot, { path: 'events.jsonl', size: 0 }), false)
  assert.equal(isRunLockFile(runRoot, { path: 'engine.lock' }), false, 'an unknown size is shown')
  assert.equal(isRunLockFile(null, { path: 'engine.lock', size: 0 }), false)
})

test('Files folds the engine locks behind one button, and a search still finds them', async () => {
  const files = [
    { path: 'config.snapshot.json', size: 9800, is_text: true },
    { path: 'engine.lock', size: 0, is_text: true },
    { path: 'events.jsonl', size: 38100, is_text: true },
    { path: 'events.jsonl.lock', size: 0, is_text: true },
    { path: 'nodes/node_0/solution.py', size: 614, is_text: true },
  ]
  globalThis.fetch = fetchStub({
    '/api/runs/demo/artifacts': {
      run_id: 'demo', run_generation: GENERATION,
      roots: [{ ...runRoot, n_files: files.length, files }],
    },
  })
  const view = await harness.mount(ArtifactsPanel, { runId: 'demo', expectedGeneration: GENERATION })
  try {
    await settle()
    const rows = () => [...view.container.querySelectorAll('.art-file .art-name')].map(node => node.textContent)
    const toggle = () => [...view.container.querySelectorAll('.art-files > button.btn')]
    assert.deepEqual(rows(), ['config.snapshot.json', 'events.jsonl', 'nodes/node_0/solution.py'])
    assert.equal(toggle().length, 1)
    assert.equal(toggle()[0].textContent, 'Show 2 empty engine lock files')
    assert.equal(toggle()[0].getAttribute('aria-pressed'), 'false')
    assert.match(view.container.querySelector('.art-root-n').textContent, /^5/,
      'the root still counts every file the server listed')

    await React.act(async () => { toggle()[0].click() })
    assert.deepEqual(rows(), files.map(file => file.path))
    assert.equal(toggle()[0].textContent, 'Hide engine lock files')
    await React.act(async () => { toggle()[0].click() })

    const filter = view.container.querySelector('.art-filter')
    const setValue = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(filter), 'value').set
    await React.act(async () => {
      setValue.call(filter, 'lock')
      filter.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    assert.deepEqual(rows(), ['engine.lock', 'events.jsonl.lock'], 'a search is never folded')
    assert.equal(toggle().length, 0)
  } finally {
    await view.unmount()
  }
})
