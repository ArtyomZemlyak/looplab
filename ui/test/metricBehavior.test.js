import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { mountLive } from './_mount.js'
const fixture = JSON.parse(readFileSync(new URL('../../tests/fixtures/metric_behavior_cases_v1.json', import.meta.url)))

test('native scalar observation mounts with original shifts, source binding and no execution actions', async () => {
  const h = await mountLive()
  try {
    const { default: View, metricBehaviorRows } = await h.load('/src/MetricBehavior.jsx')
    const rows = metricBehaviorRows(fixture.state)
    assert.equal(rows.length, 1)
    assert.equal(rows[0].current, true)
    const view = await h.mount(View, { state: fixture.state })
    try {
      assert.match(view.container.textContent, /Numerical regime shift observed/)
      assert.match(view.container.textContent, /neither cheating, faithfulness nor causal/)
      assert.match(view.container.textContent, /0.15/)
      assert.match(view.container.textContent, /Reference median: 1/)
      assert.equal(view.container.querySelectorAll('button,input,a').length, 0)
    } finally { await view.unmount() }
    const reset = structuredClone(fixture.state);reset.nodes[1].attempt = 1
    assert.equal(metricBehaviorRows(reset)[0].current, false)
    const historical = await h.mount(View, { state: reset })
    try { assert.match(historical.container.textContent, /Historical observation/) }
    finally { await historical.unmount() }
  } finally { await h.close() }
})

test('legacy absent, malformed metadata, unsupported version and nonfinite ratios never read as checked', async () => {
  const h = await mountLive()
  try {
    const { default: View, metricBehaviorRows } = await h.load('/src/MetricBehavior.jsx')
    for (const change of [s => { delete s.metric_behavior_scans }, s => { s.metric_behavior_scans[3].version = true },
      s => { s.metric_behavior_scans[3].version = 2 }, s => { s.metric_behavior_scans[3].generation = true },
      s => { s.metric_behavior_scans[3].strongest_shifts[0].ratio = Infinity },
      s => { s.metric_behavior_scans[3].evidence_digest = 'unknown' }]) {
      const state = structuredClone(fixture.state);change(state)
      assert.deepEqual(metricBehaviorRows(state), [])
      const view = await h.mount(View, { state })
      try { assert.match(view.container.textContent, /Absence does not prove stable metrics/) }
      finally { await view.unmount() }
    }
  } finally { await h.close() }
})
