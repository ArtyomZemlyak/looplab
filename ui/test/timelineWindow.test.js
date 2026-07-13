import test from 'node:test'
import assert from 'node:assert/strict'

import {
  anchoredScrollTop, buildVirtualLayout, tailScrollTop, timelineBottomGap, timelineViewportAtTail,
  virtualIndexAt, virtualRange,
} from '../src/timelineWindow.js'

test('variable row measurements produce exact offsets and a bounded visible range', () => {
  const rows = [{ seq: 1 }, { seq: 2 }, { seq: 3 }, { seq: 4 }]
  const measured = new Map([['2', 80], ['4', 10]])
  const layout = buildVirtualLayout(rows, measured, row => row.seq, 20)
  assert.deepEqual([...layout.offsets], [0, 20, 100, 120, 130])
  assert.equal(virtualIndexAt(layout, 99), 1)
  assert.deepEqual(virtualRange(layout, 90, 20, 0), { start: 1, end: 3 })
})

test('a measured visible anchor stays at the same pixel after older rows prepend', () => {
  const measured = new Map([['10', 80], ['11', 24], ['8', 50], ['9', 30]])
  const before = buildVirtualLayout([{ seq: 10 }, { seq: 11 }], measured, row => row.seq, 32)
  const anchorOffset = 17
  const beforeTop = anchoredScrollTop(before, '10', anchorOffset)
  const after = buildVirtualLayout([{ seq: 8 }, { seq: 9 }, { seq: 10 }, { seq: 11 }], measured, row => row.seq, 32)
  const afterTop = anchoredScrollTop(after, '10', anchorOffset)
  assert.equal(beforeTop, 17)
  assert.equal(afterTop, 97)
  assert.equal(afterTop - after.offsets[2], anchorOffset)
})

test('tail pin recomputes when a resizable viewport changes height', () => {
  assert.equal(tailScrollTop(2_000, 300), 1_700)
  assert.equal(tailScrollTop(2_000, 700), 1_300)
  assert.equal(tailScrollTop(200, 700), 0)
})

test('a generation replacement is not visibly at tail until the replacement scroll geometry is pinned', () => {
  assert.equal(timelineBottomGap(7_352, 0, 556), 6_796)
  assert.equal(timelineViewportAtTail(7_352, 0, 556), false)
  const replacementTop = tailScrollTop(7_352, 556)
  assert.equal(replacementTop, 6_796)
  assert.equal(timelineViewportAtTail(7_352, replacementTop, 556), true)
})

test('50k generated rows window to a small DOM slice', () => {
  const rows = Array.from({ length: 50_000 }, (_, seq) => ({ seq }))
  const started = performance.now()
  const layout = buildVirtualLayout(rows, new Map(), row => row.seq, 32)
  const range = virtualRange(layout, 800_000, 640, 8)
  const elapsed = performance.now() - started
  assert.equal(layout.totalHeight, 1_600_000)
  assert.ok(range.end - range.start <= 38, `render range was ${range.end - range.start} rows`)
  assert.ok(elapsed < 5_000, `50k layout took ${Math.round(elapsed)}ms`)
})
