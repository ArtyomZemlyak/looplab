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
  // Match BOTH the object-property form (tabIndex: 0) and the JSX attribute form (tabIndex={0}) —
  // an SVG point tab stop would use the latter, so the guard must cover it.
  assert.doesNotMatch(charts, /svgActionProps|tabIndex:\s*0|tabIndex=\{0\}/,
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
  // aria-controls only points at the data panel while it is expanded (it is unmounted when collapsed,
  // so an always-on aria-controls would dangle at a non-existent id).
  assert.match(contract, /aria-controls=\{showData \? `chart-data-\$\{generated\}` : undefined\}/)
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

test('dense trajectory and waterfall visuals stay legible while exact data remains available', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { Trajectory, ImprovementWaterfall } = await vite.ssrLoadModule('/src/charts.jsx')
    const nodes = Array.from({ length: 100 }, (_, index) => ({
      id: index + 1, metric: 100 - index, operator: 'improve', feasible: true,
    }))
    const steps = nodes.map((node, index) => ({
      id: node.id, operator: node.operator, from: index ? 102 - index : null,
      to: node.metric, delta: index ? -1 : null,
    }))
    const trajectory = renderToStaticMarkup(React.createElement(Trajectory, {
      nodes, steps, direction: 'min', onPick() {},
    }))
    assert.doesNotMatch(trajectory, /chart-hit-area/,
      'nearest-x picking must not leave overlapping per-point hit circles')
    const trajectoryLabels = [...trajectory.matchAll(/class="trajectory-step-label"[^>]*>([^<]+)<\/text>/g)]
      .map(match => match[1])
    assert.ok(trajectoryLabels.length <= 20, 'dense frontier labels must be spatially sampled')
    assert.equal(trajectoryLabels[0], '#1')
    assert.equal(trajectoryLabels.at(-1), '#100')

    const waterfall = renderToStaticMarkup(React.createElement(ImprovementWaterfall, {
      steps, direction: 'min',
    }))
    const bars = [...waterfall.matchAll(/<rect class="waterfall-bar" x="([^"]+)"[^>]*width="([^"]+)"/g)]
      .map(match => ({ x: Number(match[1]), width: Number(match[2]) }))
    assert.equal(bars.length, 100, 'a 100-step run must keep every visual bar')
    bars.slice(1).forEach((bar, index) => assert.ok(bar.x >= bars[index].x + bars[index].width,
      `waterfall bars ${index + 1} and ${index + 2} overlap`))
    const waterfallLabels = [...waterfall.matchAll(/class="waterfall-step-label"[^>]*>([^<]+)<\/text>/g)]
      .map(match => match[1])
    assert.ok(waterfallLabels.length <= 52, 'dense waterfall labels must be spatially sampled')
    assert.match(waterfallLabels[0], /#1$/)
    assert.match(waterfallLabels.at(-1), /#100$/)

    const longSteps = [...steps, ...Array.from({ length: 50 }, (_, index) => ({
      id: index + 101, operator: 'improve', from: -index, to: -index - 1, delta: -1,
    }))]
    const bounded = renderToStaticMarkup(React.createElement(ImprovementWaterfall, {
      steps: longSteps, direction: 'min',
    }))
    assert.equal((bounded.match(/class="waterfall-bar"/g) || []).length, 100)
    assert.match(bounded, /latest 99 of 150 steps; View data and CSV include all 150/)

    const baseline = renderToStaticMarkup(React.createElement(ImprovementWaterfall, {
      steps: [{ id: 1, operator: 'draft', from: null, to: 7, delta: null }], direction: 'min',
    }))
    const baselineHeight = Number(/class="waterfall-bar"[^>]*height="([^"]+)"/.exec(baseline)?.[1])
    assert.ok(baselineHeight >= 3, 'a constant-range baseline must remain visible')
  } finally {
    await vite.close()
  }
})
