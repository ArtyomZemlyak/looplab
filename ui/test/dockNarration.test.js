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
    // A non-string rationale (malformed data) no longer throws to the generic fallback: the feed narration
    // now runs rationale through `stripMd`, which coerces it to a string, so node_created still renders its
    // node info gracefully (rationale is always a string in practice — models.py Idea.rationale: str).
    assert.equal(eventNarration({ type: 'node_created', data: {
      node_id: 3, operator: 'improve', idea: { rationale: 7 },
    } }), 'node #3 via improve — 7')
    assert.equal(eventNarration({ type: 'node_failed', data: {
      node_id: 4, reason: 'guard against undefined behavior',
    } }), 'node #4 failed (guard against undefined behavior)')
    for (const [type, data] of [
      ['node_building', { operator: 'improve' }],
      ['data_leakage', {}],
      ['run_setup_finished', {}],
      ['node_confirmed', { node_id: 2, mean: 1, seeds: 3 }],
      ['strategy_decision', { strategy: {} }],
      ['train_monitor_alert', { node_id: 3 }],           // missing status -> no coerced verdict
    ]) {
      assert.equal(eventNarration({ type, data }),
        `${type} — details could not be summarized`, `${type} must not coerce a missing field`)
    }
    assert.equal(eventNarration({ type: 'run_started', data: {
      task_id: 'task-a', direction: 'max',
    } }), 'run started — task-a (max)')
    assert.equal(eventNarration({ type: 'train_monitor_alert', data: {
      node_id: 3, status: 'broken', reason: 'loss diverged', confidence: 0.9,
    } }), 'training monitor: #3 looks broken — loss diverged (90% conf)')
    assert.equal(eventNarration({ type: 'asha_rank', data: {
      node_id: 3, intermediate: 0.42, quantile: 0.5, population: 4,
    } }), 'ASHA: #3 intermediate 0.42 ranks below the 50% bar of 4 finished siblings')
    assert.equal(eventNarration({ type: 'future_event', data: {
      text: 'params.x was undefined at eval',
    } }), '{"text":"params.x was undefined at eval"}')
    assert.match(eventNarration({ type: 'future_event', data: { text: 'bounded' },
      _log_page: { truncated: true, raw_bytes: 2048 } }),
    /details omitted \(2.048 source bytes exceed page limit\)/)
  } finally {
    await vite.close()
  }
})
