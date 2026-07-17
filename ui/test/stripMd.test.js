import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// The Researcher now authors rationale as Markdown; the one-line surfaces that show it RAW (DAG card
// caption, feed row narration, pending-experiment queue) run it through stripMd so `**`/`` ` ``/`- ` don't
// leak as literal characters. Verify the flattening is correct and safe on odd input.
test('stripMd flattens inline Markdown for plain-text one-line surfaces', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { stripMd } = await vite.ssrLoadModule('/src/markdown.jsx')
    assert.equal(stripMd('Switch to **histogram-based** splitting'), 'Switch to histogram-based splitting')
    assert.equal(stripMd('use `AdamW` and _cosine_ decay'), 'use AdamW and cosine decay')
    assert.equal(stripMd('# Heading\n- one\n- two'), 'Heading one two')
    assert.equal(stripMd('> quoted rationale'), 'quoted rationale')
    assert.equal(stripMd('see [the paper](https://x.test/y)'), 'see the paper')
    assert.equal(stripMd('```\ncode fence\n```after'), 'after')
    assert.equal(stripMd('keep intra_word_underscores intact'), 'keep intra_word_underscores intact')
    // Safe on non-string / empty input (rationale is a string in practice, but be defensive).
    assert.equal(stripMd(null), '')
    assert.equal(stripMd(undefined), '')
    assert.equal(stripMd(7), '7')
  } finally {
    await vite.close()
  }
})
