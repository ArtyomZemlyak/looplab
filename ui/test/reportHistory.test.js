// Review 2026-10-14 §6 / §6.3 and UI review §3.2: the report's figures, its version history, the
// field-by-field comparison of two recorded versions, and where the optional paid action sits.
import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  REPORT_DIFF_MAX_LINES, lineDiff, normalizeReportFigures, normalizeReportVersions, reportDiff,
} from '../src/reportHistoryModel.js'
import { sharedVite } from './_mount.js'

const FIG = 'a'.repeat(64)

test('figures are read defensively: ids only, known image types, bounded, de-duplicated', () => {
  const rows = normalizeReportFigures([
    { figure_id: FIG, caption: 'Recall by week', mime: 'image/png', source: { root: 'run', path: 'plots/r.png', node_id: 3 } },
    { figure_id: FIG, caption: 'again' },
    { figure_id: '../etc/passwd' }, { figure_id: 'b'.repeat(64), mime: 'text/html' }, null, 7,
  ])
  assert.deepEqual(rows.map(row => [row.figureId.slice(0, 1), row.caption, row.mime]),
    [['a', 'Recall by week', 'image/png'], ['b', '', '']])
  assert.deepEqual(rows[0].source, { root: 'run', path: 'plots/r.png', nodeId: 3 })
  assert.deepEqual(normalizeReportFigures('x'), [])
})

test('the version list keeps the server marks and survives junk', () => {
  const list = normalizeReportVersions({
    total: 3, distinct: 2, versions: [
      { seq: 9, ts: 1.7e9, at_node: 4, headline: 'new', publisher: 'external_agent', current: true },
      { seq: 7, headline: 'same', duplicate_of: 5 },
      { seq: 5, headline: 'same' }, { seq: -1 }, 'x'],
  })
  assert.deepEqual(list.versions.map(v => [v.seq, v.current, v.duplicateOf]),
    [[9, true, null], [7, false, 5], [5, false, null]])
  assert.equal(list.distinct, 2)
  assert.deepEqual(normalizeReportVersions(null).versions, [])
})

test('the diff states what changed, field by field, and says when nothing did', () => {
  const older = { headline: 'D3 wins', verdict: 'D3 beats the base.', learnings: ['a', 'b'],
    body_markdown: 'line 1\nline 2\nline 3' }
  const newer = { headline: 'D3 wins', verdict: 'D3 beats the base by 3 %.', learnings: ['b', 'c'],
    body_markdown: 'line 1\nline 2 changed\nline 3', figures: [{ figure_id: FIG }] }
  const diff = reportDiff(older, newer)
  const by = Object.fromEntries(diff.fields.map(row => [row.field, row]))
  assert.equal(diff.changed, true)
  assert.equal(by.headline.status, 'same')
  assert.equal(by.verdict.status, 'changed')
  assert.deepEqual([by.learnings.added, by.learnings.removed], [['c'], ['a']])
  assert.deepEqual(by.body_markdown.ops.filter(op => op.op !== 'same'),
    [{ op: 'del', text: 'line 2' }, { op: 'add', text: 'line 2 changed' }])
  assert.deepEqual([by.figures.added, by.figures.removed], [1, 0])
  assert.equal(reportDiff(older, { ...older }).changed, false)
  const long = Array.from({ length: REPORT_DIFF_MAX_LINES + 1 }, (_, i) => `l${i}`).join('\n')
  assert.equal(lineDiff(long, `${long}\nmore`), null, 'over the bound the page says so, never a quadratic table')
})

test('Report draws figures, the version history, and the optional paid action behind More', async () => {
  const vite = await sharedVite()
  try {
    const { default: ReportView } = await vite.ssrLoadModule('/src/Report.jsx')
    const state = {
      run_id: 'r', task_id: 't', goal: 'g', direction: 'max', phase: 'finished', research: [],
      reward_hacks: [], drifts: [], best_node_id: 0,
      nodes: { 0: { id: 0, metric: 1, operator: 'draft', parent_ids: [], feasible: true, status: 'evaluated', idea: { params: {} } } },
      report: { headline: 'With a plot', at_node: 1, trigger: 'external', published_seq: 5,
        publisher: { kind: 'external_agent' },
        figures: [{ figure_id: FIG, caption: 'Recall by week', mime: 'image/png' }] },
    }
    const external = new JSDOM(renderToStaticMarkup(React.createElement(ReportView, {
      state, runId: 'r', runBackend: 'toy', externalHarness: true, expectedGeneration: 'a'.repeat(64),
    })))
    const internal = new JSDOM(renderToStaticMarkup(React.createElement(ReportView, {
      state, runId: 'r', runBackend: 'toy', expectedGeneration: 'a'.repeat(64),
    })))
    const review = renderToStaticMarkup(React.createElement(ReportView, {
      state, runId: 'r', readOnly: true, readOnlyReason: 'review', expectedGeneration: 'a'.repeat(64),
    }))
    try {
      const doc = external.window.document
      assert.ok(doc.querySelector('figure.report-figure'), 'the attached figure is drawn')
      assert.match(doc.querySelector('figure.report-figure figcaption').textContent, /Recall by week/)
      assert.ok(doc.querySelector('details.report-versions'), 'version history is offered')
      const more = doc.querySelector('details.report-more')
      assert.ok(more, 'the optional model generation sits behind More actions on an external run')
      assert.match(more.querySelector('button').textContent, /Generate with model · optional · paid/)
      // A toy internal run has no model: its paid refresh stays visible but cannot start.
      const paid = [...internal.window.document.querySelectorAll('button')]
        .find(button => /paid/.test(button.textContent))
      assert.ok(paid && paid.disabled, 'no model, no paid action')
      assert.equal(internal.window.document.querySelector('details.report-more'), null)
      assert.doesNotMatch(review, /report-versions/, 'a review link reads no history route')
      assert.match(review, /1 attached figure is not shown in this view/)
    } finally {
      external.window.close(); internal.window.close()
    }
  } finally { await vite.close() }
})
