import test from 'node:test'
import assert from 'node:assert/strict'
import { click, mountLive, until } from './_mount.js'

test('legacy confirmation metadata never infers successful repeats from individual rows', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { Metrics } = await h.load('/src/Inspector.jsx')
    const node = { id: 1, attempt: 0, status: 'evaluated', feasible: true, metric: 1.2,
      confirmed_mean: .8, confirmed_std: null, confirmed_seeds: null, parent_ids: [] }
    const detail = { ...node, confirm_seeds_detail: { 10: .7, 11: .9 } }
    const view = await h.mount(Metrics, { n: node, detail, state: { nodes: { 1: node } }, runId: 'r' })
    try {
      assert.match(view.container.textContent, /Mean recorded; multiple successful repeats not established/)
      assert.match(view.container.textContent, /Spread is not recorded/)
      assert.doesNotMatch(view.container.textContent, /over 2 seeds/)
      const table = [...view.container.querySelectorAll('table')]
        .find(table => table.querySelector('caption')?.textContent === 'Per-seed confirmation metrics')
      assert.equal(table?.rows.length, 3)
      for (const seeds of [undefined, '2', true, -2, 2.5, NaN, Infinity]) {
        const legacy = { ...node, confirmed_seeds: seeds }
        await view.rerender({ n: legacy, detail: { ...detail, confirmed_seeds: seeds }, state: {}, runId: 'r' })
        assert.match(view.container.textContent, /multiple successful repeats not established/)
        assert.doesNotMatch(view.container.textContent, /over .* seeds|confirmation mean ± std/)
      }
      await view.rerender({ n: { ...node, confirmed_seeds: 1, confirmed_std: 0 },
        detail: { ...detail, confirmed_seeds: 1, confirmed_std: 0 }, state: {}, runId: 'r' })
      assert.match(view.container.textContent, /Standard deviation: 0/)
      assert.match(view.container.textContent, /multiple successful repeats not established/)
      await view.rerender({ n: { ...node, confirmed_seeds: 3, confirmed_std: .1 },
        detail: {}, state: {}, runId: 'r' })
      assert.match(view.container.textContent, /3 repeat checks.*Standard deviation: 0.1/)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('metric tag prefixes cannot collide with object prototype properties', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { MetricLines } = await h.load('/src/MetricLines.jsx')
    const names = ['constructor', '__proto__', 'toString', 'hasOwnProperty', 'train']
    const series = Object.fromEntries(names.map(name => [`${name}/loss`, [{ step: 1, value: .5 }]]))
    const view = await h.mount(MetricLines, { series })
    try {
      const controls = [...view.container.querySelectorAll('.metric-group-toggle')]
      assert.equal(controls.length, names.length)
      for (const control of controls) {
        await click(control)
        assert.equal(control.getAttribute('aria-expanded'), 'true')
      }
      assert.equal(view.container.querySelectorAll('svg').length, names.length)
      assert.doesNotMatch(view.container.innerHTML, /NaN|Infinity/)
      assert.equal(h.fetch.calls.length, 0)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('invalid metric series are unavailable evidence, with explicit retry for a terminal node', async () => {
  let metrics = { 'train/loss': 'not a point array' }
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1/metrics': () => ({ node_id: 1, attempt: 0, metrics }),
  } })
  try {
    const { MetricCurves } = await h.load('/src/Inspector.jsx')
    const view = await h.mount(MetricCurves, { runId: 'r', nodeId: 1, attempt: 0, status: 'evaluated' })
    try {
      await until(() => view.container.textContent.includes('Metric curves unavailable'), 'string series refused')
      for (const value of [null, {}, [null], [{ step: null, value: 1 }],
        [{ step: 0, value: true }], [{ step: '1', value: .5 }], [{ step: 1, value: '0.5' }]]) {
        metrics = { 'train/loss': value }
        await click(view.container.querySelector('button'))
        await until(() => view.container.textContent.includes('Metric curves unavailable'), 'malformed points refused')
        assert.doesNotMatch(view.container.textContent, /no metric curves logged yet|1 metric/)
      }
      metrics = { 'train/loss': [{ step: 0, value: 0 }, { step: 1, value: -.5 }] }
      await click(view.container.querySelector('button'))
      await until(() => view.container.querySelector('.metric-group-toggle'), 'valid zero and negative values recover')
      await click(view.container.querySelector('.metric-group-toggle'))
      assert.match(view.container.textContent, /Latest -0.5.*2 points/)
      assert.doesNotMatch(view.container.innerHTML, /NaN|Infinity/)
      assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})
