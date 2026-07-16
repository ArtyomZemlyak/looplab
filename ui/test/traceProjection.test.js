import test from 'node:test'
import assert from 'node:assert/strict'
import { traceDetailState, tracePartial, unavailableTraceDetail } from '../src/traceProjection.js'

test('trace omission truth reconciles malformed server counters and local caps', () => {
  assert.equal(tracePartial({ total_spans: 20, visible_spans: 7, omitted_spans: -9 }), true)
  assert.equal(tracePartial({ total_spans: 1, visible_spans: 8 }), false)
  assert.equal(tracePartial({ truncated: true }), true)
})

test('trace detail transport failure stays distinct from a successful empty projection', () => {
  assert.deepEqual(traceDetailState({ attributes: {}, projection: {} }), {
    status: 'ready', attributes: {}, partial: false,
  })
  assert.deepEqual(traceDetailState({ attributes: [], projection: { truncated: true } }), {
    status: 'ready', attributes: {}, partial: true,
  })
  assert.deepEqual(unavailableTraceDetail(), {
    status: 'unavailable', attributes: {}, partial: false,
  })
})
