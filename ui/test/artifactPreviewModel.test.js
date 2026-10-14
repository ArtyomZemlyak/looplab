import assert from 'node:assert/strict'
import test from 'node:test'
import { CSV_MAX_ROWS, parseDelimited, prettyJson, previewKind } from '../src/artifactPreviewModel.js'

// doc 77 §3: one representation per file kind; binaries are fetched as image bytes or described.
test('the preview kind follows the server verdict first, the extension second', () => {
  assert.equal(previewKind('plots/loss.png', { is_text: false }), 'image')
  assert.equal(previewKind('plots/loss.svg', { is_text: false }), 'meta')      // SVG is never drawn
  assert.equal(previewKind('model.safetensors', { is_text: false }), 'meta')
  assert.equal(previewKind('metrics.json', { is_text: true, content: '{}' }), 'json')
  assert.equal(previewKind('runs.tsv', { is_text: true, content: 'a\tb' }), 'table')
  assert.equal(previewKind('notes.txt', { is_text: true, content: 'x' }), 'text')
  assert.equal(previewKind('x.png', null), 'none')
})

test('CSV parsing keeps quoted separators and newlines and stays bounded', () => {
  const parsed = parseDelimited('name,value\n"a, b",1\n"multi\nline","say ""hi"""\n')
  assert.deepEqual(parsed.header, ['name', 'value'])
  assert.deepEqual(parsed.rows, [['a, b', '1'], ['multi\nline', 'say "hi"']])
  const big = parseDelimited(['h1,h2', ...Array.from({ length: 500 }, (_, i) => `${i},x`)].join('\n'))
  assert.equal(big.rows.length, CSV_MAX_ROWS)
  assert.equal(big.truncated, true)
  assert.equal(parseDelimited('a\tb\n1\t2', '\t').rows[0][1], '2')
})

test('JSON is pretty-printed only when it parses', () => {
  assert.equal(prettyJson('{"a":1}'), '{\n  "a": 1\n}')
  assert.equal(prettyJson('{oops'), null)
  assert.equal(prettyJson('{"a":1}\n{"a":2}\n', true).split('\n').length, 6)
})
