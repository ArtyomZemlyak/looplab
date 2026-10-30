import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchStub, mountLive, until, click } from './_mount.js'

test('shows saved duplicate and self-attested resolution with historical caveat', async () => {
  const h = await mountLive()
  try {
    const { default: ResearchNotices } = await h.load('/src/ResearchNotices.jsx')
    globalThis.fetch = fetchStub({ ['GET /api/runs/r/research-notices']: { offset: 0, limit: 20, has_more: false, unavailable_receipts: 0,
      items: [{ notice_id: 'n1', command_id: 'cmd_1', disposition: 'resolved', reason: 'corrected', notice: { kind: 'report_unchanged' } }] } })
    const view = await h.mount(ResearchNotices, { runId: 'r', generation: 'a'.repeat(64) })
    try {
      await click(view.container.querySelector('button'))
      await until(() => view.container.textContent.includes('report_unchanged'), 'notice')
      assert.match(view.container.textContent, /resolved/)
      assert.match(view.container.textContent, /self-reported/)
      assert.match(view.container.textContent, /historical warnings may be missing/)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
