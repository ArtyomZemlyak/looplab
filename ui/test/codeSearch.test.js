import test from 'node:test'
import assert from 'node:assert/strict'
import { codeSearchRows } from '../src/codeSearch.js'

const found = (text, query) => codeSearchRows([{ line: text }], query).rows[0].ranges
const fragments = (text, ranges) => ranges.map(range => text.slice(range.from, range.to))

test('Unicode matching uses original UTF-16 positions and never matches generated lowercase characters', () => {
  assert.deepEqual(found('İ🚀x', 'x'), [{ from: 3, to: 4 }])
  assert.deepEqual(found('İx', '\u0307'), [])
  assert.deepEqual(found('🚀🚀', '🚀'), [{ from: 0, to: 2 }, { from: 2, to: 4 }])
  assert.deepEqual(found('🚀', '\ud83d'), [], 'a query cannot split a valid surrogate pair')
  const greek = 'ΟΣ σ ς Σ'
  assert.deepEqual(fragments(greek, found(greek, 'σ')), ['Σ', 'σ', 'ς', 'Σ'])
  assert.deepEqual(fragments('ПРИВЕТ привет', found('ПРИВЕТ привет', 'привет')), ['ПРИВЕТ', 'привет'])
})

test('every regex metacharacter is a literal search character, including a backslash', () => {
  const symbols = '$.*+?^{}()|[]\\'
  for (const query of [...symbols, '.*', '\\', '[', '$', '\\b']) {
    const text = 'prefix ' + query + ' suffix'
    assert.deepEqual(fragments(text, found(text, query)), [query], query)
  }
  assert.deepEqual(found('axb', 'a.b'), [])
  assert.deepEqual(found('aaa', 'a+'), [])
})

test('matching lines and non-overlapping ranges share one result without mutating diff rows', () => {
  const rows = [{ l: 'xxx', kind: 'del', oldNo: 7 }, { line: 'X', kind: 'add', newNo: 8 }, { line: '' }]
  const result = codeSearchRows(rows, 'x')
  assert.equal(result.matches, 2, 'three hits in one row count as one matching line')
  assert.equal(result.rows[0].row, rows[0])
  assert.equal(result.rows[1].row, rows[1])
  assert.equal(result.rows[0].ranges.length, 3)
  assert.deepEqual(found('aaaa', 'aa'), [{ from: 0, to: 2 }, { from: 2, to: 4 }])
  assert.equal(codeSearchRows(rows, '').matches, 0)
  assert.ok(codeSearchRows(rows, '').rows.every(row => row.ranges.length === 0))
  assert.deepEqual(found('', ' '), [], 'blank display scaffolding is not source text')
  assert.deepEqual(rows, [{ l: 'xxx', kind: 'del', oldNo: 7 }, { line: 'X', kind: 'add', newNo: 8 }, { line: '' }])
})
