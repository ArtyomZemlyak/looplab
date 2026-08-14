// The browser half of the extra-metric CHANNEL rule (`ui/src/extraMetrics.js`), driven directly.
//
// What this holds: a number the experiment printed itself must never be presented to an operator as
// an operator-declared measurement, and a value from a run recorded before the channel existed must
// say so rather than borrowing the guarded channel's authority.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  EXTRA_METRIC_AUTO, EXTRA_METRIC_DECLARED, EXTRA_METRIC_UNKNOWN,
  EXTRA_METRIC_CHANNEL_LABEL, extraMetricChannel, extraMetricIsDeclared,
  unverifiedExtraMetricKeys,
} from '../src/extraMetrics.js'

const node = (extras, provenance) => ({ extra_metrics: extras, extra_metrics_provenance: provenance })

test('a tagged value reports the channel the record carries', () => {
  const n = node({ latency: 5, printed: 1 },
    { latency: EXTRA_METRIC_DECLARED, printed: EXTRA_METRIC_AUTO })
  assert.equal(extraMetricChannel(n, 'latency'), EXTRA_METRIC_DECLARED)
  assert.equal(extraMetricChannel(n, 'printed'), EXTRA_METRIC_AUTO)
  assert.equal(extraMetricIsDeclared(n, 'latency'), true)
  assert.equal(extraMetricIsDeclared(n, 'printed'), false)
})

test('an untagged historical value reads unknown, never declared', () => {
  // The 12 preserved values are exactly this shape, and all 12 were in fact auto-captured — so
  // `declared` here would be the one answer that is provably wrong.
  const n = node({ train_auc: 0.99 }, undefined)
  assert.equal(extraMetricChannel(n, 'train_auc'), EXTRA_METRIC_UNKNOWN)
  assert.equal(extraMetricIsDeclared(n, 'train_auc'), false)
  assert.equal(EXTRA_METRIC_CHANNEL_LABEL[EXTRA_METRIC_UNKNOWN], 'provenance unknown')
})

test('a key the map omits does not inherit its neighbours authority', () => {
  const n = node({ a: 1, b: 2 }, { a: EXTRA_METRIC_DECLARED })
  assert.equal(extraMetricChannel(n, 'b'), EXTRA_METRIC_UNKNOWN)
})

test('an untrusted or unrecognised map shape degrades to unknown', () => {
  assert.equal(extraMetricChannel(node({ a: 1 }, null), 'a'), EXTRA_METRIC_UNKNOWN)
  assert.equal(extraMetricChannel(node({ a: 1 }, ['declared']), 'a'), EXTRA_METRIC_UNKNOWN)
  assert.equal(extraMetricChannel(node({ a: 1 }, { a: 'trusted' }), 'a'), EXTRA_METRIC_UNKNOWN)
  assert.equal(extraMetricChannel(undefined, 'a'), EXTRA_METRIC_UNKNOWN)
})

test('a Pareto column is unverified when ANY reporting node reports it undeclared', () => {
  // A column heading is one label over many rows; it cannot be half-trustworthy. The declared node
  // does not launder the auto one.
  const declared = node({ latency: 5 }, { latency: EXTRA_METRIC_DECLARED })
  const printed = node({ latency: 9 }, { latency: EXTRA_METRIC_AUTO })
  const clean = node({ size: 2 }, { size: EXTRA_METRIC_DECLARED })
  const keys = ['latency', 'size']
  assert.deepEqual([...unverifiedExtraMetricKeys([declared, printed, clean], keys)], ['latency'])
  assert.deepEqual([...unverifiedExtraMetricKeys([declared, clean], keys)], [])
  // A node that does not report the key at all has no opinion about it.
  assert.deepEqual([...unverifiedExtraMetricKeys([clean, node({}, undefined)], ['size'])], [])
})

test('a legacy population marks every column it reports', () => {
  const legacy = [node({ train_auc: 0.99, test_auc: 0.92 }, undefined)]
  assert.deepEqual([...unverifiedExtraMetricKeys(legacy, ['train_auc', 'test_auc'])],
    ['train_auc', 'test_auc'])
})
