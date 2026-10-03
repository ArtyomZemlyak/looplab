import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { mountLive } from './_mount.js'
import { analyze, verdict, toMarkdown } from '../src/report.js'

const node = (id, metric, extra = {}) => ({ id, attempt: 0, metric, status: 'evaluated',
  feasible: true, operator: id ? 'improve' : 'draft', parent_ids: id ? [0] : [],
  parent_comparison: id ? { version: 1, node_id: 0, attempt: 0 } : null,
  idea: { params: {} }, metric_provenance: { comparability: { keys: { measured: 'same' } } }, ...extra })
const run = (child = node(1, 7), extra = {}) => ({ run_id: 'r', task_id: 't', direction: 'min',
  nodes: { 0: node(0, 10), 1: child }, best_node_id: 1, ...extra })
const base = (n, digest = 'a'.repeat(64)) => ({ ...n, metric_provenance: {
  ...n.metric_provenance, base_revision: { version: 1, complete: true, digest,
    node_id: n.id, generation: n.attempt, seed_event_seq: 1, file_count: 1, bytes: 10,
    scope: 'seeded_editables_before_mounts_and_overlay',
    archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } } } })
const click = async (_h, button) => React.act(async () => button.dispatchEvent(
  new window.MouseEvent('click', { bubbles: true, cancelable: true })))

test('trajectory preserves recorded excluded points but only eligible results move the frontier', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { Trajectory } = await h.load('/src/charts.jsx')
    for (const direction of ['min', 'max']) {
      const bad = direction === 'min' ? -100 : 100
      const rows = [node(0, 10), node(1, 12, { confirmed_mean: direction === 'min' ? 5 : 15 }),
        node(2, bad), node(3, bad, { feasible: null }), node(4, bad, { feasible: false }),
        node(5, bad, { status: 'pending' }), node(6, bad, { tombstoned: true }),
        node(7, bad), node(8, NaN), node(9, 0, { confirmed_mean: Infinity }),
        node(10, null, { confirmed_mean: 11 }), node(11, bad, { status: 'failed' })]
      const state = { direction, nodes: Object.fromEntries(rows.map(n => [n.id, n])),
        breed_excluded: [2], aborted_nodes: [7] }
      const mounted = await h.mount(Trajectory, { nodes: rows, direction, state })
      const root = mounted.container
      assert.equal(root.querySelectorAll('.chart-pt').length, 6)
      assert.match(root.querySelector('svg').textContent, /eligible frontier: (5|15)/)
      assert.match(root.querySelector('.chart-pt:nth-of-type(1)')?.textContent || root.textContent,
        /evaluation score/)
      await click(h, root.querySelector('button[aria-label="View Metric trajectory data"]'))
      const tableRows = [...root.querySelectorAll('tbody tr')]
      const excluded = tableRows.find(row => row.querySelector('th').textContent === '#2')
      assert.equal(excluded.querySelector('[data-label="Selection status"]').textContent, 'excluded or unknown')
      assert.equal(excluded.querySelector('[data-label="Eligible numeric frontier"]').textContent,
        direction === 'min' ? '5' : '15')
      const mean = tableRows.find(row => row.querySelector('th').textContent === '#1')
      assert.equal(mean.querySelector('[data-label="Measurement"]').textContent, 'confirmation mean')
      await mounted.rerender({ nodes: rows, state: { ...state, direction: null }, direction: null })
      assert.match(root.querySelector('.accessible-chart-description').textContent, /Unknown direction: no frontier/)
      assert.ok([...root.querySelectorAll('[data-label="Eligible numeric frontier"]')]
        .every(cell => cell.textContent === ''))
      await mounted.unmount()
    }
    assert.deepEqual(h.fetch.calls, [])
  } finally { await h.close() }
})

test('mixed means, bases and retargets stay numeric changes in the rendered report and exported chart', async () => {
  const h = await mountLive({ visible: true })
  const savedCreate = URL.createObjectURL, savedRevoke = URL.revokeObjectURL
  const blobs = [], downloads = []
  const captureDownload = event => { if (event.target.tagName === 'A') {
    event.preventDefault(); downloads.push(event.target.download)
  } }
  URL.createObjectURL = blob => { blobs.push(blob); return 'blob:https://looplab.test/csv' }
  URL.revokeObjectURL = () => {}
  document.addEventListener('click', captureDownload)
  try {
    const { ImprovementWaterfall } = await h.load('/src/charts.jsx')
    const { default: Report } = await h.load('/src/Report.jsx')
    const variants = [run(node(1, 12, { confirmed_mean: 5 })),
      run(undefined, { upstream_enabled: true, nodes: { 0: base(node(0, 10)), 1: base(node(1, 7), 'b'.repeat(64)) } }),
      run(undefined, { objective_key: 'accuracy' }), run(node(1, 7, { metric_provenance: null }))]
    for (const state of variants) {
      const analysis = analyze(state)
      const report = document.createElement('div')
      report.innerHTML = h.render(Report, { state, runId: 'r', readOnly: true })
      const trajectory = report.querySelector('#report-section-trajectory')
      assert.ok(trajectory)
      assert.doesNotMatch(report.querySelector('.report-steps-table').innerHTML, /improved|regressed/)
      const chart = await h.mount(ImprovementWaterfall, { steps: analysis.steps, direction: state.direction })
      assert.match(chart.container.textContent, /Numeric changes do not establish comparable improvement/)
      assert.ok([...chart.container.querySelectorAll('.waterfall-bar')]
        .every(bar => bar.getAttribute('fill') === '#4aa3ff'))
      assert.doesNotMatch([...chart.container.querySelectorAll('svg title')].map(el => el.textContent).join(' '),
        /improved|regressed|baseline/)
      await click(h, chart.container.querySelector('button[aria-label="View Numeric frontier changes data"]'))
      assert.equal(chart.container.querySelectorAll('tbody tr').length, analysis.steps.length)
      await click(h, chart.container.querySelector('button[aria-label="Export Numeric frontier changes data as CSV"]'))
      const csv = await blobs.at(-1).text()
      assert.match(csv, /"Measurement","Numeric change"/)
      assert.match(csv, /"evaluation score"/)
      if (state.nodes[1].confirmed_mean != null) assert.match(csv, /"confirmation mean"/)
      assert.equal(downloads.at(-1), 'numeric-frontier.csv')
      assert.match(toMarkdown(state), /recorded value \| measurement \| numeric change/)
      await chart.unmount()
    }
    assert.deepEqual(h.fetch.calls, [])
  } finally {
    document.removeEventListener('click', captureDownload)
    await h.close()
    URL.createObjectURL = savedCreate; URL.revokeObjectURL = savedRevoke
  }
})

test('Overview reuses the Report verdict through evidence changes and selection exclusions', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { OverviewPanel } = await h.load('/src/panels.jsx')
    let reportReads = 0
    const props = state => ({ state, onReadReport: () => reportReads++ })
    const mounted = await h.mount(OverviewPanel, props(run()))
    for (const state of [run(), run(node(1, 12, { confirmed_mean: 5 })),
      run(node(1, 7, { metric_provenance: null })), run(undefined, { objective_key: 'accuracy' }),
      run(undefined, { upstream_enabled: true, nodes: { 0: base(node(0, 10)), 1: base(node(1, 7), 'b'.repeat(64)) } }),
      run(undefined, { breed_excluded: [1] }), run(node(1, 7, { feasible: null })),
      run(node(1, 7, { tombstoned: true })), run(undefined, { aborted_nodes: [1] }),
      run(node(1, NaN)), run(undefined, { source_integrity: { complete: false } })]) {
      await mounted.rerender(props(state))
      const expected = verdict(state, analyze(state))
      const root = mounted.container
      assert.equal(root.querySelector('[aria-label="Result interpretation"] p').textContent, expected.headline)
      assert.equal(root.querySelector('.ov-best strong').textContent,
        expected.best ? String(expected.best.confirmed_mean ?? expected.best.metric) : '—')
      assert.match(root.querySelector('.ov-best .ov-label').textContent, /Selected result/)
    }
    await click(h, [...mounted.container.querySelectorAll('button')].find(button => button.textContent === 'Read Report'))
    assert.equal(reportReads, 1)
    assert.deepEqual(h.fetch.calls, [], 'result interpretation never contacts a model or command route')
  } finally { await h.close() }
})
