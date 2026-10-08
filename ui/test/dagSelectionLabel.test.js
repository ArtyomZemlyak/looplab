// The node card's screen-reader label is translated PART BY PART (critic 2026-10-08): joined first,
// it was one English sentence no catalogue entry could ever match.
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { readFileSync } from 'node:fs'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('selection label parts translate one by one', async () => {
  const vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true } })
  try {
    const { selectionLabelText } = await vite.ssrLoadModule('/src/Dag.jsx')
    const lang = await vite.ssrLoadModule('/src/uiLanguage.js')
    const parts = [['Experiment #{0}', [7]], 'artifact node, never ranked', 'some concept text', 'Select to inspect']
    assert.equal(selectionLabelText(parts),
      'Experiment #7, artifact node, never ranked, some concept text, Select to inspect')
    // Every static part and template the label uses has a catalogue entry, so the Russian UI
    // (whose catalogue loads asynchronously, outside this SSR load) translates each of them.
    const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8')).messages
    for (const key of ['Experiment #{0}', 'metric {0}', 'metric unavailable', 'current champion',
      'currently working', 'artifact node, never ranked', 'uses artifacts {0}', 'failure reason {0}',
      'Select to inspect']) assert.ok(catalogue[key], key)
    assert.equal(typeof lang.uiMessage, 'function')
  } finally {
    await vite.close()
  }
})
