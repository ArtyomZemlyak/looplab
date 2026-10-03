import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { mountLive } from './_mount.js'

test('finite metric ranges remain visible without fake floors or coordinate overflow', async t => {
  const h = await mountLive({ visible: true })
  try {
    const { MiniLine } = await h.load('/src/MetricLines.jsx')
    for (const [name, low, high] of [
      ['tiny learning-rate or loss range', 0, 1e-12],
      ['small nonzero range', 1e-12, 1.1e-12],
      ['tiny negative range', -1e-12, 0],
      ['subnormal range', 0, Number.MIN_VALUE],
      ['opposite finite extremes', -Number.MAX_VALUE, Number.MAX_VALUE],
      ['ordinary negative range', -2, -1],
    ]) {
      await t.test(name, () => {
        const pts = [{ step: low, value: low }, { step: high, value: high }]
        const root = document.createElement('div')
        root.innerHTML = renderToStaticMarkup(React.createElement(MiniLine, { label: name, pts }))
        const path = root.querySelector('path').getAttribute('d')
        assert.doesNotMatch(path, /NaN|Infinity/)
        const coordinates = [...path.matchAll(/[ML]([-\d.]+) ([-\d.]+)/g)]
          .map(match => [Number(match[1]), Number(match[2])])
        assert.deepEqual(coordinates, [[30, 100], [332, 16]],
          'real nonconstant finite bounds must span the plot on both axes')
        assert.match(root.textContent, /2 points/)
      })
    }
    assert.equal(h.fetch.calls.length, 0)
  } finally { await h.close() }
})

test('a single recorded measurement has a visible marker without needing hover', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { MiniLine } = await h.load('/src/MetricLines.jsx')
    const view = await h.mount(MiniLine, { label: 'val/loss', pts: [{ step: 5, value: -2 }] })
    try {
      const marker = view.container.querySelector('svg circle')
      assert.ok(marker, 'a move-only SVG path does not draw a measurement')
      assert.equal(marker.getAttribute('cx'), '181')
      assert.equal(marker.getAttribute('cy'), '58')
      assert.match(view.container.textContent, /Latest -2.*1 points/)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('constant curves are centered and an interior extreme-range value stays halfway', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { MiniLine } = await h.load('/src/MetricLines.jsx')
    const root = document.createElement('div')
    for (const [pts, expected] of [
      [[{ step: 0, value: 0 }, { step: 1, value: 0 }], 'M30.0 58.0 L332.0 58.0'],
      [[{ step: -Number.MAX_VALUE, value: -Number.MAX_VALUE }, { step: 0, value: 0 },
        { step: Number.MAX_VALUE, value: Number.MAX_VALUE }], 'M30.0 100.0 L181.0 58.0 L332.0 16.0'],
    ]) {
      root.innerHTML = renderToStaticMarkup(React.createElement(MiniLine, { label: 'loss', pts }))
      assert.equal(root.querySelector('path').getAttribute('d'), expected)
    }
  } finally { await h.close() }
})
