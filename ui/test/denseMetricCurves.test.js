import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { click, mountLive } from './_mount.js'

const dense = () => {
  const points = Array.from({ length: 250_001 }, (_, step) => ({ step, value: step % 23, wall_time: step + 100 }))
  points[125_001].value = 1000
  points[125_002].value = -1000
  return points
}

test('long logged series render a bounded plot while retaining range, latest value and point count', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { MiniLine } = await h.load('/src/MetricLines.jsx')
    const pts = dense()
    const markup = renderToStaticMarkup(React.createElement(MiniLine, { label: 'train/loss', pts }))
    const root = document.createElement('div'); root.innerHTML = markup
    const commands = root.querySelector('path').getAttribute('d').match(/[ML]/g)
    assert.ok(commands.length <= 1024, 'render cost must stay bounded as the log grows')
    assert.match(root.textContent, /250001 points/)
    assert.match(root.textContent, /plot.*of.*points/i, 'visual reduction must be disclosed')
    assert.match(root.textContent, /-1000|−1,000|-1,000/)
    assert.match(root.textContent, /step 0–250000/)
    assert.doesNotMatch(markup, /NaN|Infinity/)
    assert.equal(h.fetch.calls.length, 0)
  } finally { await h.close() }
})

test('exact curve data pages stay bounded and full CSV retains every recorded row', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { MiniLine } = await h.load('/src/MetricLines.jsx')
    const pts = Array.from({ length: 251 }, (_, step) => ({ step, value: step / 10, wall_time: step + 100 }))
    const view = await h.mount(MiniLine, { label: 'loss', pts })
    const button = name => [...view.container.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === name)
    try {
      await click(button('View loss data'))
      assert.equal(view.container.querySelectorAll('tbody tr').length, 100)
      assert.match(view.container.textContent, /1–100 of 251/)
      await click(button('Next loss data page'))
      assert.match(view.container.textContent, /101–200 of 251/)
      await click(button('Last loss data page'))
      assert.equal(view.container.querySelectorAll('tbody tr').length, 51)
      assert.equal(view.container.querySelector('tbody tr:last-child th').textContent, '250')
      assert.equal(button('Next loss data page').disabled, true)
      await click(button('First loss data page'))
      assert.equal(view.container.querySelector('tbody tr:first-child th').textContent, '0')
      await click(button('Last loss data page'))
      const previous = { create: URL.createObjectURL, revoke: URL.revokeObjectURL,
        click: window.HTMLAnchorElement.prototype.click }
      let exported, download
      URL.createObjectURL = blob => { exported = blob; return 'blob:curve-test' }
      URL.revokeObjectURL = () => {}
      window.HTMLAnchorElement.prototype.click = function () { download = this.download }
      try {
        await click(button('Export loss data as CSV'))
        const csv = await exported.text()
        assert.equal(download, 'loss.csv')
        assert.equal(csv.split('\r\n').length, 252)
        assert.match(csv, /"0","0","100"/)
        assert.match(csv, /"250","25","350"$/)
        await new Promise(resolve => setTimeout(resolve, 0))
      } finally {
        URL.createObjectURL = previous.create; URL.revokeObjectURL = previous.revoke
        window.HTMLAnchorElement.prototype.click = previous.click
      }
      await view.rerender({ label: 'loss', pts: pts.slice(0, 2) })
      assert.equal(view.container.querySelectorAll('tbody tr').length, 2,
        'a shorter refreshed series clamps the current page instead of hiding its data')
      assert.equal(button('Next loss data page'), undefined)
      assert.ok(button('Export loss data as CSV'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('visual projection preserves exact endpoints, extrema and recorded order without modifying data', async () => {
  const { metricCurveProjection } = await import('../src/metricCurveProjection.js')
  const pts = dense(), before = JSON.stringify(pts)
  const { points, bounds } = metricCurveProjection(pts)
  assert.ok(points.length <= 1024)
  assert.equal(points[0], pts[0])
  assert.equal(points.at(-1), pts.at(-1))
  assert.ok(points.includes(pts[125_001]))
  assert.ok(points.includes(pts[125_002]))
  assert.deepEqual(bounds, [0, 250000, -1000, 1000])
  const indices = points.map(p => pts.indexOf(p))
  assert.ok(indices.every((n, i) => i === 0 || n > indices[i - 1]))
  assert.equal(JSON.stringify(pts), before)
  const constant = pts.map(p => ({ step: p.step, value: 0 }))
  assert.ok(metricCurveProjection(constant).points.length <= 1024)
  const small = pts.slice(0, 1024)
  assert.equal(metricCurveProjection(small).points, small)
})

test('empty and single-point series render without invalid geometry and updates withdraw old hover', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { MiniLine } = await h.load('/src/MetricLines.jsx')
    const view = await h.mount(MiniLine, { label: 'loss', pts: [] })
    try {
      assert.match(view.container.textContent, /no metric points/)
      await view.rerender({ label: 'loss', pts: [{ step: 0, value: 0 }] })
      assert.doesNotMatch(view.container.innerHTML, /NaN|Infinity/)
      const svg = view.container.querySelector('svg')
      svg.getBoundingClientRect = () => ({ left: 0, width: 340 })
      const { act } = await import('react')
      await act(async () => svg.dispatchEvent(new window.MouseEvent('pointermove', { bubbles: true, clientX: 30 })))
      assert.match(view.container.textContent, /Step 0:/)
      await view.rerender({ label: 'loss', pts: [{ step: 50, value: -1 }] })
      assert.match(view.container.textContent, /Latest -1/)
      assert.doesNotMatch(view.container.textContent, /Step 50:/)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
