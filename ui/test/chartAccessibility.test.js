import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const exportedFunction = (sourceText, name, nextName) => {
  const start = sourceText.indexOf(`export function ${name}`)
  const end = nextName ? sourceText.indexOf(`export function ${nextName}`, start + 1) : sourceText.length
  assert.ok(start >= 0 && end > start, `${name} source must exist`)
  return sourceText.slice(start, end)
}

test('analytical charts expose named table and CSV alternatives', async () => {
  const charts = await source('charts.jsx')
  const ordered = ['Trajectory', 'ImprovementWaterfall', 'Bars', 'Gantt', 'ParallelCoords', 'Scatter', 'Spark', 'MultiTrajectory', 'MetricLines']
  for (const [index, name] of ordered.entries()) {
    if (name === 'Spark' || name === 'MetricLines') continue
    const body = exportedFunction(charts, name, ordered[index + 1])
    assert.match(body, /<ChartFrame[\s\S]*?columns=\{columns\}[\s\S]*?rows=\{/,
      `${name} must publish exact chart data`)
    assert.match(body, /csvName=/, `${name} must offer CSV export`)
    assert.match(body, /aria-labelledby=\{labelledBy\}/, `${name} visual needs a persistent name`)
  }
  assert.match(exportedFunction(charts, 'Spark', 'MultiTrajectory'), /role="img"[\s\S]*?aria-label=/)
  assert.match(charts, /<button type="button" key=\{i\}[\s\S]*?aria-pressed=\{active === it\.key\}/)
  assert.doesNotMatch(charts, /svgActionProps|tabIndex:\s*0/,
    'large analytical charts must not create one tab stop per SVG point')
  assert.match(exportedFunction(charts, 'Gantt', 'ParallelCoords'),
    /render: value => onPick[\s\S]*?<button type="button" className="btn xs ghost"/)
  assert.match(charts, /const _RUN_DASHES = \[/)
  assert.match(charts, /strokeDasharray=\{_RUN_DASHES/)
  assert.match(charts, /className="metric-group-toggle" aria-expanded=\{open\}/)
})

test('shared table/chart contract is responsive and touch targets remain explicit', async () => {
  const [contract, css] = await Promise.all([source('accessibility.jsx'), source('styles.css')])
  assert.match(contract, /className="data-table-scroll" role="region" aria-labelledby=\{headingId\} tabIndex=\{0\}/)
  assert.match(contract, /<caption className="sr-only">/)
  assert.match(contract, /data-label=\{column\.label \|\| column\.key\}/)
  assert.match(contract, /aria-controls=\{`chart-data-\$\{generated\}`\}/)
  assert.match(css, /\.data-table-scroll \{[\s\S]*?overflow: auto;/)
  assert.match(css, /@media \(max-width: 600px\)[\s\S]*?\.data-table\.cardable tbody td::before/)
  assert.match(css, /\.run-menu \.mi, \.tabs \.tab,[\s\S]*?min-height: 44px;/)
  assert.match(css, /\.react-flow__controls button \{ width: 44px; height: 44px; \}/)
})

test('list and map use native links for the primary open-run action', async () => {
  const [list, map] = await Promise.all([source('RunList.jsx'), source('MapView.jsx')])
  assert.match(list, /<a className="run-card-main" href=\{`#\/run\/\$\{encodeURIComponent\(r\.run_id\)\}`\}/)
  assert.doesNotMatch(list, /role="link"/)
  assert.match(map, /<a className="run-node nodrag nopan" href=\{`#\/run\/\$\{encodeURIComponent\(run\.run_id\)\}`\}/)
  assert.match(map, /<button type="button" className="grp-tab nodrag nopan"/)
  assert.match(list, /followClientRoute\(event, \(\) => onOpen\(r\.run_id\)\)/)
  assert.match(map, /followClientRoute\(event, open\)/)
})

test('every analytical chart renders its non-empty data path', async t => {
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const charts = await vite.ssrLoadModule('/src/charts.jsx')
    const fixtures = [
      ['Trajectory', { nodes: [{ id: 1, metric: 0.7, operator: 'draft', feasible: true,
        idea: { theme: 'baseline' } }], direction: 'min', onPick() {} }],
      ['ImprovementWaterfall', { steps: [{ id: 1, operator: 'draft', from: 0.8, to: 0.7, delta: -0.1 }],
        direction: 'min' }],
      ['Bars', { data: [{ label: 'learning rate', value: 0.1 }] }],
      ['Gantt', { spans: { nodes: { 1: [{ name: 'evaluate', start: 0, duration_s: 1, status: 'OK' }] } },
        onPick() {} }],
      ['ParallelCoords', { nodes: [{ id: 1, metric: 0.7, operator: 'draft',
        idea: { params: { learning_rate: 0.1 } } }],
        direction: 'min', onPick() {} }],
      ['Scatter', { data: [{ id: 1, x: 0.1, y: 0.7, feasible: true }], xlab: 'Learning rate',
        ylab: 'Metric', onPick() {} }],
      ['Spark', { series: [0.8, 0.7] }],
      ['MultiTrajectory', { runs: [{ run_id: 'run-a', label: 'Baseline', series: [0.8, 0.7] }] }],
      ['MetricLines', { series: { loss: [{ step: 1, value: 0.8, wall_time: 1 },
        { step: 2, value: 0.7, wall_time: 2 }] } }],
      ['MiniLine', { label: 'loss', pts: [{ step: 1, value: 0.8, wall_time: 1 },
        { step: 2, value: 0.7, wall_time: 2 }] }],
    ]
    const expected = {
      Trajectory: 'Metric trajectory', ImprovementWaterfall: 'Improvement waterfall',
      Bars: 'Value comparison', Gantt: 'Execution span timeline',
      ParallelCoords: 'Parameter relationships', Scatter: 'Metric by Learning rate',
      Spark: 'Trend across 2 values', MultiTrajectory: 'Cross-run trajectories',
      MetricLines: '1 metric', MiniLine: 'Latest 0.7 · 2 points',
    }
    for (const [name, props] of fixtures) {
      await t.test(name, () => {
        const warnings = []
        const originalError = console.error
        console.error = (...args) => warnings.push(args.map(String).join(' '))
        let markup
        try { markup = renderToStaticMarkup(React.createElement(charts[name], props)) }
        finally { console.error = originalError }
        assert.match(markup, new RegExp(expected[name]), `${name} did not render its expected data path`)
        assert.deepEqual(warnings, [], `${name} emitted React render warnings`)
      })
    }
  } finally {
    await vite.close()
  }
})
