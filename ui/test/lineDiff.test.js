import test from 'node:test'
import assert from 'node:assert/strict'
import { diffLines } from '../src/lineDiff.js'

test('ordered diff keeps changes at their source position', () => {
  const diff = diffLines('a\nx\nb', 'a\ny\nb')
  assert.deepEqual(diff.map(line => [line.kind, line.line]), [
    ['same', 'a'], ['del', 'x'], ['add', 'y'], ['same', 'b'],
  ])
  assert.deepEqual(diff.map(line => [line.oldNo, line.newNo]), [
    [1, 1], [2, null], [null, 2], [3, 3],
  ])
})

test('ordered diff preserves duplicate lines and reordering', () => {
  const diff = diffLines('a\nx\nx\nb', 'a\nx\nb\nx')
  assert.equal(diff.filter(line => line.line === 'x').length, 3)
  assert.ok(diff.some(line => line.kind === 'del'))
  assert.ok(diff.some(line => line.kind === 'add'))
  assert.equal(diff.at(-1).line, 'x')
})
