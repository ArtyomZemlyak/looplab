import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// doc 76 §4: an external agent's published verdict carried a GFM pipe table which the hand-written
// renderer printed as `| … |<br/>|---|` prose. This is the shape of that real verdict (drift-q05-
// cache-v3 seq 451): a Cyrillic header, a right-aligned numeric delimiter and inline code.
const VERDICT = [
  'Итог по четырём метрикам:',
  '',
  '| Модель после w06 | IC200 | IC20 | NB200 | NB20 |',
  '|---|---:|---:|---:|---:|',
  '| A0 (база) | 0.1812 | 0.0921 | 0.2410 | 0.1302 |',
  '| A5 `lr=2e-4` | **0.1907** | 0.0955 | 0.2466 | 0.1331 |',
  '',
  'Вывод: A5 лучше базы.',
].join('\n')

test('GFM pipe tables render as bounded, escaped HTML tables', async t => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: Markdown, stripMd } = await vite.ssrLoadModule('/src/markdown.jsx')
    const html = text => renderToStaticMarkup(React.createElement(Markdown, { text }))
    const tables = s => (s.match(/<table/g) || []).length

    await t.test('the real verdict shape becomes one table with aligned numeric columns', () => {
      const out = html(VERDICT)
      assert.equal(tables(out), 1)
      assert.match(out, /<div class="md-table-wrap" role="region" tabindex="0"/)
      assert.match(out, /<th scope="col">Модель после w06<\/th>/)
      assert.match(out, /<th scope="col" style="text-align:right">IC200<\/th>/)
      assert.match(out, /<td style="text-align:right"><strong>0.1907<\/strong><\/td>/)
      assert.match(out, /<td>A5 <code>lr=2e-4<\/code><\/td>/)
      assert.equal((out.match(/<tr>/g) || []).length, 3)
      // Numbers are passed through unchanged and no pipe leaks as text.
      for (const n of ['0.1812', '0.0921', '0.2410', '0.1302', '0.0955', '0.2466', '0.1331'])
        assert.ok(out.includes(n), n)
      assert.ok(!out.includes('|---'), out)
      assert.match(out, /<p class="md-p">Итог по четырём метрикам:<\/p>/)
      assert.match(out, /<p class="md-p">Вывод: A5 лучше базы.<\/p>/)
    })

    await t.test('a table directly after a paragraph line still starts a table', () => {
      const out = html('Results:\n| a | b |\n|---|---|\n| 1 | 2 |')
      assert.equal(tables(out), 1)
      assert.match(out, /<p class="md-p">Results:<\/p>/)
    })

    await t.test('escaped pipes and pipes in inline code stay inside one cell', () => {
      const out = html('| expr | note |\n|:-:|:--|\n| `a|b` | x \\| y |')
      assert.match(out, /<td style="text-align:center"><code>a\|b<\/code><\/td>/)
      assert.match(out, /<td style="text-align:left">x \| y<\/td>/)
    })

    await t.test('ragged rows are padded or cut to the header width', () => {
      const out = html('| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |')
      const rows = out.split('<tbody>')[1].split('</tr>').filter(r => r.includes('<td'))
      assert.deepEqual(rows.map(r => (r.match(/<td/g) || []).length), [3, 3])
      assert.ok(!out.includes('>4<'))
    })

    await t.test('no table without a valid delimiter row', () => {
      for (const src of ['not | a table\n---', '| a | b |\n| c | d |', '| a | b |\n|---|',
        '| a | b |\n|-x-|---|', 'a | b\nplain text', '---\n| a |'])
        assert.equal(tables(html(src)), 0, src)
      // The setext-looking `---` alone is still a horizontal rule, not a table.
      assert.match(html('text\n\n---'), /<hr class="md-hr"\/>/)
    })

    await t.test('HTML and unsafe links in cells stay inert', () => {
      const out = html('| x | y |\n|---|---|\n| <img src=x onerror=alert(1)> | [go](javascript:alert(1)) |')
      assert.ok(!out.includes('<img'), out)
      assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/)
      assert.ok(!/href="javascript/i.test(out), out)
      assert.match(out, /<span>go<\/span>/)
    })

    await t.test('large tables are bounded and say so', () => {
      const header = '|' + Array.from({ length: 40 }, (_, i) => ` h${i} `).join('|') + '|'
      const delim = '|' + Array.from({ length: 40 }, () => '---').join('|') + '|'
      const row = '|' + Array.from({ length: 40 }, (_, i) => ` ${i} `).join('|') + '|'
      const out = html([header, delim, ...Array.from({ length: 500 }, () => row)].join('\n'))
      assert.equal(tables(out), 1)
      assert.equal((out.match(/<th /g) || []).length, 24)
      const bodyRows = (out.split('<tbody>')[1].match(/<tr>/g) || []).length
      assert.ok(bodyRows <= 200, `rows ${bodyRows}`)
      assert.ok((out.match(/<td/g) || []).length <= 4_096)
      assert.match(out, /role="status"/)
      const huge = html(`| a |\n|---|\n| ${'x'.repeat(10_000)} |`)
      assert.ok(huge.length < 4_000, `cell bounded: ${huge.length}`)
      assert.match(huge, /role="status"/)
    })

    await t.test('an ordinary small table carries no limit notice', () => {
      assert.ok(!html(VERDICT).includes('role="status"'))
    })

    await t.test('one-line surfaces flatten a table without raw pipes', () => {
      assert.equal(stripMd('| a | b |\n|---|---:|\n| 1 | 2 |'), 'a · b 1 · 2')
      assert.equal(stripMd('| x \\| y | z |'), 'x | y · z')
      // Prose that merely mentions a pipe is left alone.
      assert.equal(stripMd('use a | b in the shell'), 'use a | b in the shell')
    })
  } finally {
    await vite.close()
  }
})
