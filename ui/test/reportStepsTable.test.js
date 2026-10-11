import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { sharedVite } from './_mount.js'
import { readSource } from './_source.js'

// Measured 2026-10-11 in Chromium on the offline demo: beside the Assistant side panel the Report is
// 620px wide at a 1280px window, the steps table kept its 1040px desktop min-width inside a 574px
// region, and `what changed` — the column the table exists for — sat off-screen behind a `base`
// column that is empty on every run with no code base (the demo, every dataset task).

const digest = 'a'.repeat(64)
const baseRevision = { version: 1, complete: true, digest, node_id: 1, generation: 0, seed_event_seq: 2,
  file_count: 1, bytes: 10, scope: 'seeded_editables_before_mounts_and_overlay',
  archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } }

const run = withBase => ({
  run_id: 'steps', task_id: 'task', goal: 'Steps table', direction: 'min', phase: 'finished',
  best_node_id: 1, reward_hacks: [], drifts: [], research: [],
  nodes: {
    0: { id: 0, metric: 10, operator: 'draft', parent_ids: [], feasible: true, status: 'evaluated',
      idea: { params: { x: 1 } } },
    1: { id: 1, attempt: 0, metric: 7, operator: 'improve', parent_ids: [0], feasible: true, status: 'evaluated',
      idea: { params: { x: 2 } }, ...(withBase ? { metric_provenance: { base_revision: baseRevision } } : {}) },
  },
})

const renderSteps = async withBase => {
  const vite = await sharedVite()
  const { default: ReportView } = await vite.ssrLoadModule('/src/Report.jsx')
  const markup = renderToStaticMarkup(React.createElement(ReportView, {
    state: run(withBase), runId: 'steps', readOnly: true,
  }))
  const table = new JSDOM(markup).window.document.querySelector('.report-steps-table')
  return {
    headers: [...table.querySelectorAll('thead th')].map(th => th.textContent),
    widths: [...table.querySelectorAll('tbody tr')].map(row => row.children.length),
    lastCells: [...table.querySelectorAll('tbody tr')].map(row => row.lastElementChild.textContent),
    region: table.closest('section')?.className,
  }
}

test('a run that records no code base has no always-empty base column in the steps table', async () => {
  const steps = await renderSteps(false)
  assert.deepEqual(steps.headers,
    ['#', 'node', 'operator', 'recorded value', 'measurement', 'numeric change', 'what changed'])
  assert.deepEqual(steps.widths, [7, 7], 'every row has exactly the header cells')
  assert.deepEqual(steps.lastCells, ['—', 'x: 1 → 2'], 'the last column is still what changed')
  assert.equal(steps.region, 'data-table-region report-steps-region',
    'the region is the size container the narrow layout below queries')
})

test('a run whose experiments record a base keeps the base column before what changed', async () => {
  const steps = await renderSteps(true)
  assert.deepEqual(steps.headers,
    ['#', 'node', 'operator', 'recorded value', 'measurement', 'numeric change', 'base', 'what changed'])
  assert.deepEqual(steps.widths, [8, 8])
  assert.deepEqual(steps.lastCells, ['—', 'x: 1 → 2'])
})

test('below the desktop min-width the steps table shares its region instead of scrolling it away', async () => {
  // A container query is a layout rule jsdom does not compute; the Chromium measurement in the
  // header is the driven check, this keeps its load-bearing parts from being dropped separately.
  // Comments removed: the rule's own why-comment names the min-width it retired.
  const css = (await readSource(new URL('../src/report-trust-polish.css', import.meta.url), 'utf8'))
    .replace(/\/\*[\s\S]*?\*\//g, '')
  assert.match(css, /\.report-steps-region \{ container-type: inline-size; \}/)
  assert.doesNotMatch(css, /report-steps-table[^{]*\{[^}]*min-width/,
    'no desktop min-width may push the last column out of a narrower region')
  const wide = css.match(/@container \(min-width: 1040px\) \{([\s\S]*?)\n\}/)?.[1] || ''
  assert.match(wide, /\.report-steps-table \{ table-layout: fixed; \}/,
    'the fixed desktop columns apply only where all of them fit')
  assert.match(wide, /th\.report-steps-base \{ width: 130px; \}/)
  assert.doesNotMatch(css.replace(wide, ''), /table-layout: fixed/)
  assert.match(css, /\.report-view \.report-steps-table :is\(th, td\) \{ overflow-wrap: normal; \}/,
    'the report-wide `anywhere` wrap must not cut header words mid-word in auto layout')
})
