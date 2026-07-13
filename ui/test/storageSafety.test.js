import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { storageGet, storageRemove, storageSet } from '../src/util.js'

test('blocked localStorage degrades to defaults without throwing during UI boot', () => {
  const previous = globalThis.window
  globalThis.window = { localStorage: {
    getItem() { throw new DOMException('blocked', 'SecurityError') },
    setItem() { throw new DOMException('blocked', 'SecurityError') },
    removeItem() { throw new DOMException('blocked', 'SecurityError') },
  } }
  try {
    assert.equal(storageGet('missing', 'fallback'), 'fallback')
    assert.equal(storageSet('x', '1'), false)
    assert.equal(storageRemove('x'), false)
  } finally {
    if (previous === undefined) delete globalThis.window
    else globalThis.window = previous
  }
})

test('command-bearing component boot paths use the safe storage boundary', async () => {
  for (const name of ['RunView.jsx', 'AssistantBar.jsx', 'Dock.jsx', 'Dag.jsx']) {
    const source = await readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')
    assert.doesNotMatch(source, /\blocalStorage\.(?:getItem|setItem|removeItem)\(/, name)
  }
})
