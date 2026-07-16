import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import axe from 'axe-core'
import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const blockingViolations = violations => violations
  .filter(violation => violation.impact === 'critical' || violation.impact === 'serious')
  .map(violation => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map(node => ({
      target: node.target,
      failureSummary: node.failureSummary,
    })),
  }))

test('accessibility helpers satisfy their semantic and interaction contracts', async t => {
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true },
  })

  try {
    const { ChartFrame, DataTable, downloadBlob, followClientRoute, nextRovingIndex, tableCsv } =
      await vite.ssrLoadModule('/src/accessibility.jsx')
    const { ParallelCoords, Scatter, Trajectory } = await vite.ssrLoadModule('/src/charts.jsx')

    await t.test('DataTable and ChartFrame have no serious or critical WCAG 2 A/AA violations', async () => {
      const columns = [
        { key: 'candidate', label: 'Candidate', firstColumnHeader: true },
        { key: 'score', label: 'Score', numeric: true },
      ]
      const rows = [
        { candidate: 'baseline', score: 0.72 },
        { candidate: 'challenger', score: 0.81 },
      ]
      const fixture = React.createElement(React.Fragment, null,
        React.createElement(DataTable, {
          caption: 'Candidate evaluation scores',
          columns,
          rows,
          rowKey: row => row.candidate,
          csvName: 'candidate-scores.csv',
        }),
        React.createElement(ChartFrame, {
          title: 'Candidate score comparison',
          description: 'Bar lengths and exact values compare candidate scores.',
          columns,
          rows,
          csvName: 'candidate-score-chart.csv',
        }, ({ labelledBy }) => React.createElement('svg', {
          role: 'img',
          'aria-labelledby': labelledBy,
          viewBox: '0 0 100 20',
        }, React.createElement('rect', { x: 0, y: 2, width: 81, height: 6 }))),
        React.createElement(Trajectory, {
          nodes: [{ id: 1, metric: 0.7, operator: 'draft', feasible: true,
            idea: { theme: 'baseline' } }],
          direction: 'min', onPick() {},
        }),
        React.createElement(ParallelCoords, {
          nodes: [{ id: 1, metric: 0.7, operator: 'draft',
            idea: { params: { learning_rate: 0.1 } } }],
          direction: 'min', onPick() {},
        }),
        React.createElement(Scatter, {
          data: [{ id: 1, x: 0.1, y: 0.7, feasible: true }],
          xlab: 'Learning rate', ylab: 'Metric', onPick() {},
        }),
      )
      const markup = renderToStaticMarkup(fixture)
      const dom = new JSDOM(
        `<!doctype html><html lang="en"><head><title>Accessibility contract</title></head>` +
        `<body><main><h1>Run evidence</h1>${markup}</main></body></html>`,
        { runScripts: 'outside-only' },
      )

      try {
        dom.window.eval(axe.source)
        const results = await dom.window.axe.run(dom.window.document, {
          runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
          rules: {
            // jsdom has neither layout nor Canvas getContext(), so axe cannot compute rendered contrast.
            'color-contrast': { enabled: false },
          },
        })
        const blocking = blockingViolations(results.violations)
        assert.equal(blocking.length, 0, JSON.stringify(blocking, null, 2))
      } finally {
        dom.window.close()
      }
    })

    await t.test('tableCsv preserves embedded newlines and escapes quotes', () => {
      const columns = [
        { key: 'name', label: 'Name "quoted"' },
        { key: 'note', label: 'Line\nbreak' },
      ]
      const rows = [{ name: 'Ada "A"', note: 'one,\ntwo' }]

      assert.equal(
        tableCsv(columns, rows),
        '"Name ""quoted""","Line\nbreak"\r\n"Ada ""A""","one,\ntwo"',
      )
    })

    await t.test('tableCsv neutralizes formulas in untrusted text without changing numbers', () => {
      const columns = [{ key: 'value', label: '=dangerous label' }]
      const rows = [
        { value: '=HYPERLINK("https://invalid.example")' },
        { value: '  @SUM(1,2)' },
        { value: -12.5 },
      ]
      assert.equal(
        tableCsv(columns, rows),
        '"\'=dangerous label"\r\n"\'=HYPERLINK(""https://invalid.example"")"\r\n"\'  @SUM(1,2)"\r\n"-12.5"',
      )
    })

    await t.test('blob downloads attach before click and defer URL revocation', async () => {
      const dom = new JSDOM('<!doctype html><body></body>')
      const previous = {
        document: globalThis.document,
        create: globalThis.URL.createObjectURL,
        revoke: globalThis.URL.revokeObjectURL,
      }
      const clicks = []
      let revoked = false
      globalThis.document = dom.window.document
      globalThis.URL.createObjectURL = () => 'blob:download-test'
      globalThis.URL.revokeObjectURL = href => {
        assert.equal(href, 'blob:download-test')
        revoked = true
      }
      dom.window.HTMLAnchorElement.prototype.click = function click() {
        clicks.push({ attached: this.isConnected, download: this.download })
      }
      try {
        assert.equal(downloadBlob('report.md', ['safe report'], 'text/markdown'), true)
        assert.deepEqual(clicks, [{ attached: true, download: 'report.md' }])
        assert.equal(dom.window.document.querySelector('a'), null)
        assert.equal(revoked, false)
        await new Promise(resolve => setTimeout(resolve, 0))
        assert.equal(revoked, true)
      } finally {
        if (previous.document === undefined) delete globalThis.document
        else globalThis.document = previous.document
        if (previous.create === undefined) delete globalThis.URL.createObjectURL
        else globalThis.URL.createObjectURL = previous.create
        if (previous.revoke === undefined) delete globalThis.URL.revokeObjectURL
        else globalThis.URL.revokeObjectURL = previous.revoke
        dom.window.close()
      }
    })

    await t.test('client routing preserves every native modified-link gesture', () => {
      const navigated = []
      const event = overrides => ({
        button: 0, defaultPrevented: false, preventDefault() { this.prevented = true }, ...overrides,
      })
      const plain = event()
      assert.equal(followClientRoute(plain, () => navigated.push('plain')), true)
      assert.equal(plain.prevented, true)
      for (const modified of [event({ ctrlKey: true }), event({ metaKey: true }),
        event({ shiftKey: true }), event({ altKey: true }), event({ button: 1 })]) {
        assert.equal(followClientRoute(modified, () => navigated.push('modified')), false)
        assert.equal(modified.prevented, undefined)
      }
      assert.deepEqual(navigated, ['plain'])
    })

    await t.test('nextRovingIndex implements wrapping tabs with Home and End', () => {
      assert.equal(nextRovingIndex('ArrowRight', 2, 3), 0)
      assert.equal(nextRovingIndex('ArrowLeft', 0, 3), 2)
      assert.equal(nextRovingIndex('Home', 2, 3), 0)
      assert.equal(nextRovingIndex('End', 0, 3), 2)
      assert.equal(nextRovingIndex('Escape', 1, 3), null)
    })
  } finally {
    await vite.close()
  }
})
