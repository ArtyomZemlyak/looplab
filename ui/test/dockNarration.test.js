import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('timeline narration stays renderable for malformed and forward-compatible events', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { eventNarration } = await vite.ssrLoadModule('/src/Dock.jsx')
    assert.equal(eventNarration({ type: 'future_event' }), '{}')
    assert.equal(eventNarration({ type: 'node_created', data: null }),
      'node_created — details could not be summarized')
    assert.equal(eventNarration({ type: 'node_created', data: {
      node_id: 3, operator: 'improve', idea: { rationale: 7 },
    } }), 'node_created — details could not be summarized')
    assert.match(eventNarration({ type: 'future_event', data: { text: 'bounded' },
      _log_page: { truncated: true, raw_bytes: 2048 } }),
    /details omitted \(2.048 source bytes exceed page limit\)/)
  } finally {
    await vite.close()
  }
})
