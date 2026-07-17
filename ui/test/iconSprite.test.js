import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('versioned SVG sprite preserves every OpIcon glyph and the unknown-name fallback', async () => {
  const [source, sprite] = await Promise.all([
    readFile(new URL('../src/icons.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../public/looplab-icons-v1.svg', import.meta.url), 'utf8'),
  ])
  const declared = source.match(/'flag trending[^']+'/)?.[0].slice(1, -1).split(' ') || []
  const document = new JSDOM(sprite, { contentType: 'image/svg+xml' }).window.document
  const symbols = [...document.querySelectorAll('symbol')]
  const ids = symbols.map(symbol => symbol.id)

  assert.equal(declared.length, 37)
  assert.deepEqual(ids, declared)
  assert.equal(new Set(ids).size, ids.length)
  assert.ok(symbols.every(symbol => symbol.getAttribute('viewBox') === '0 0 16 16'))
  assert.equal(document.querySelector('script, foreignObject, use, image'), null)

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { OpIcon } = await vite.ssrLoadModule('/src/icons.jsx')
    const known = renderToStaticMarkup(React.createElement(OpIcon, {
      name: 'bell', size: 22, className: 'probe',
    }))
    const fallback = renderToStaticMarkup(React.createElement(OpIcon, { name: 'not-a-glyph' }))
    assert.match(known, /href="\.\/looplab-icons-v1\.svg#bell"/)
    assert.match(known, /width="22"[^>]*height="22"/)
    assert.match(known, /aria-hidden="true"[^>]*focusable="false"/)
    assert.match(fallback, /href="\.\/looplab-icons-v1\.svg#dot"/)
  } finally {
    await vite.close()
  }
})
