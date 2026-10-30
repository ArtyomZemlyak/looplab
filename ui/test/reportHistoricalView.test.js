import test from 'node:test'
import React from 'react'
import assert from 'node:assert/strict'
import { fetchStub, mountLive, until, click } from './_mount.js'

test('opens the full earlier report from history without replacing the current report', async () => {
  const h = await mountLive()
  try {
    const { ReportVersions } = await h.load('/src/ReportExtras.jsx')
    globalThis.fetch = fetchStub({
      'GET /api/runs/r/report/versions': { total: 2, distinct: 2, versions: [
        { seq: 2, headline: 'Latest', current: true }, { seq: 1, headline: 'Earlier' },
      ] },
      'GET /api/runs/r/report/versions/1': { seq: 1, content: {
        headline: 'Earlier', summary: 'Measured comparison', body_markdown: 'Original result',
        caveats: ['Observed test reused'], learnings: ['A distinct lesson'], figures: [],
      } },
    })
    const view = await h.mount(ReportVersions, { runId: 'r' })
    try {
      const details = view.container.querySelector('details')
      await React.act(async () => { details.open = true; details.dispatchEvent(new window.Event('toggle')) })
      await until(() => view.container.querySelectorAll('button').length === 2, 'versions')
      await click(view.container.querySelectorAll('button')[1])
      await until(() => view.container.textContent.includes('Original result'), 'historical content')
      assert.match(view.container.textContent, /Measured comparison/)
      assert.match(view.container.textContent, /Observed test reused/)
      assert.match(view.container.textContent, /A distinct lesson/)
      assert.match(view.container.textContent, /Latest/)
      assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})
