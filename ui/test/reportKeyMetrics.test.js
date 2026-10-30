import assert from 'node:assert/strict'
import test from 'node:test'
import { normalizeRunReport, reportAuthor, reportKeyMetricRows } from '../src/reportModel.js'

// Review 2026-10-14 section 6: an external agent's report carries a GFM body and the metric/node
// REFERENCES of its key results; every VALUE in the table comes from the recorded nodes.
test('key-metric rows read values from the recorded nodes, never from the report', () => {
  const report = normalizeRunReport({
    headline: 'h', trigger: 'external', body_markdown: '| a | b |\n|---|---|\n| 1 | 2 |',
    publisher: { kind: 'external_agent', author_claim: 'Codex CLI' },
    key_metrics: { metrics: [{ key: 'IC200', label: 'InCart@200' }, { key: 'NB20' }, { key: 7 }],
      nodes: [{ node_id: 3, label: 'D3' }, { node_id: 1, label: 'base' }, { node_id: 99 }],
      baseline_node_id: 1 },
  })
  assert.equal(report.body_markdown.startsWith('| a | b |'), true)
  assert.deepEqual(reportAuthor(report), { kind: 'external_agent', claim: 'Codex CLI' })
  const nodes = { 1: { id: 1, metric: 0.2, extra_metrics: { IC200: 0.2, NB20: 0.1 } },
    3: { id: 3, metric: 0.25, extra_metrics: { IC200: 0.25 } } }
  const rows = reportKeyMetricRows(report.key_metrics, nodes)
  assert.equal(report.key_metrics.metrics.length, 2)
  assert.deepEqual(rows[0].values.map(v => v.value), [0.25, null])   // NB20 not measured: null, not 0
  assert.ok(Math.abs(rows[0].values[0].delta - 0.05) < 1e-12)
  assert.equal(rows[1].baseline, true)
  assert.equal(rows[2].missing, true)
})

test('without named nodes the champion is the row; legacy reports keep their author', () => {
  const report = normalizeRunReport({ headline: 'h', key_metrics: { metrics: ['metric'] } })
  assert.equal(report.key_metrics, null)   // a bare string list is not the documented shape
  const shaped = normalizeRunReport({ headline: 'h', key_metrics: { metrics: [{ key: 'metric' }] } })
  const rows = reportKeyMetricRows(shaped.key_metrics, { 4: { metric: 1.5 } }, 4)
  assert.deepEqual(rows.map(r => [r.node_id, r.values[0].value]), [[4, 1.5]])
  assert.equal(reportAuthor(normalizeRunReport({ headline: 'h', trigger: 'cadence' })).kind, 'looplab_model')
  assert.equal(reportAuthor(normalizeRunReport({ headline: 'h', trigger: 'external' })).kind, 'external_agent')
})


test('recorded extra metrics remain available when the objective scalar is missing', () => {
  const refs = normalizeRunReport({ key_metrics: {
    metrics: [{ key: 'IC ProductRecall@200', label: 'Product' }, { key: 'metric' }],
    nodes: [{ node_id: 31 }, { node_id: 39 }], baseline_node_id: 31,
  } }).key_metrics
  const rows = reportKeyMetricRows(refs, {
    31: { metric: null, extra_metrics: { 'IC ProductRecall@200': .6 } },
    39: { metric: null, extra_metrics: { 'IC ProductRecall@200': .59 } },
  })
  assert.equal(rows[0].values[0].value, .6)
  assert.equal(rows[0].values[1].value, null)
  assert.equal(rows[1].values[0].value, .59)
  assert.equal(rows[1].values[1].value, null)
})

test('bound old attempt cannot silently display current attempt or its difference', () => {
  const refs = normalizeRunReport({ key_metrics: {
    metrics: [{ key: 'Recall', role: 'primary', test_id: 'test-1', cohort: 'users', policy: 'beam' }],
    nodes: [{ node_id: 1, node_generation: 0 }, { node_id: 2, node_generation: 1 }], baseline_node_id: 1,
  } }).key_metrics
  const rows = reportKeyMetricRows(refs, { 1: { attempt: 1, extra_metrics: { Recall: .3 } },
    2: { attempt: 1, extra_metrics: { Recall: .4 } } })
  assert.equal(refs.metrics[0].test_id, 'test-1')
  assert.equal(rows[0].attemptMismatch, true)
  assert.equal(rows[0].values[0].value, null)
  assert.equal(rows[1].values[0].value, .4)
  assert.equal(rows[1].values[0].delta, null)
})
