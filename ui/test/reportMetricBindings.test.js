import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchStub, mountLive, until, click } from './_mount.js'

test('report bindings display upload consistency without claiming policy matches', async () => {
  const h = await mountLive()
  try {
    const { default: ReportMetricBindings } = await h.load('/src/ReportMetricBindings.jsx')
    globalThis.fetch = fetchStub({ ['GET /api/runs/r/report/versions/5/metric-bindings']: {
      upload_history_complete: true, bindings: [{ node_id: 2, metric: 'Product@200', test_id: 'test1',
        uploads: [{ upload_id: 'upload1', uploaded_metric: 'product', n_queries: 100, attempt: 0, attempt_verified: true, consistency: 'matches_measured' }] }],
    } })
    const view = await h.mount(ReportMetricBindings, { runId: 'r', generation: 'a'.repeat(64), seq: 5 })
    try {
      await click(view.container.querySelector('button'))
      await until(() => view.container.textContent.includes('upload1'), 'upload')
      assert.match(view.container.textContent, /matches_measured/)
      assert.match(view.container.textContent, /does not establish matching GT/)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
