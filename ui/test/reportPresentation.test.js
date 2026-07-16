import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import { analyze, toMarkdown } from '../src/report.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const node = (id, metric, operator, parentIds = [], theme = '') => ({
  id, metric, operator, parent_ids: parentIds, feasible: true, status: 'evaluated',
  idea: { params: {}, theme },
})

const state = (direction, first, final = null) => {
  const nodes = { 0: node(0, first, 'draft', [], 'starter') }
  if (final != null) nodes[1] = node(1, final, 'manual', [0], 'manual-probe')
  return {
    run_id: `report-${direction}`, task_id: 'task', goal: 'Report semantics', direction,
    phase: 'finished', nodes, best_node_id: final == null ? 0 : 1,
    reward_hacks: [], drifts: [], research: [],
  }
}

test('total improvement is positive for both objective directions and Markdown names a lone baseline truthfully', () => {
  assert.equal(analyze(state('min', 10, 7)).totalGain, 3)
  assert.equal(analyze(state('max', 10, 13)).totalGain, 3)
  assert.match(toMarkdown(state('min', 10, 7)), /Total improvement: \*\*3\*\*/)

  const baseline = toMarkdown(state('min', 10))
  assert.match(baseline, /^## Metric baseline$/m)
  assert.doesNotMatch(baseline, /What worked — key improvements/)
})

test('Report uses semantic section headings and exposes an unambiguous operator/theme identity', async () => {
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ReportView } = await vite.ssrLoadModule('/src/Report.jsx')
    const markup = renderToStaticMarkup(React.createElement(ReportView, {
      state: state('min', 10, 7), runId: 'report-min', readOnly: true,
    }))
    const dom = new JSDOM(markup)
    try {
      const sections = [...dom.window.document.querySelectorAll('.report-view .section-h')]
      assert.ok(sections.length >= 4)
      assert.ok(sections.every(heading => heading.tagName === 'H2'))
      assert.equal(sections[1].textContent, 'How the metric got better')

      const identities = [...dom.window.document.querySelectorAll('.report-step-kind')]
      assert.ok(identities.every(identity => identity.getAttribute('aria-hidden') === 'true'))
      assert.equal(identities[1].nextElementSibling?.textContent, 'manual · manual-probe')
      assert.equal(identities[1].nextElementSibling?.className, 'sr-only')
      assert.equal(identities[1].hasAttribute('aria-label'), false)
    } finally {
      dom.window.close()
    }

    const baselineMarkup = renderToStaticMarkup(React.createElement(ReportView, {
      state: state('min', 10), runId: 'report-min', readOnly: true,
    }))
    assert.match(baselineMarkup, /<h2 class="section-h">Metric baseline<\/h2>/)
    assert.doesNotMatch(baselineMarkup, /How the metric got better/)
    assert.match(baselineMarkup, /First feasible metric; no improvement is recorded yet/)
  } finally {
    await vite.close()
  }
})

test('print CSS removes the screen-only code viewport limit', async () => {
  const css = await readFile(new URL('../src/report-trust-polish.css', import.meta.url), 'utf8')
  assert.match(css, /@media print[\s\S]*?\.report-view pre\.code\s*\{[^}]*max-height:\s*none\s*!important;[^}]*overflow:\s*visible\s*!important;/)
})
