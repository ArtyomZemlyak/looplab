import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import { directionProfit, optimizationLabel, toMarkdown } from '../src/report.js'
import { GROUP_MODES } from '../src/grouping.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const node = (id, metric, direction, parentIds = []) => ({
  id, metric, parent_ids: parentIds, feasible: true, status: 'evaluated', operator: 'improve',
  idea: { theme: direction, params: {} },
})

test('direction controls keep first-discovery order while live performance changes', () => {
  const state = {
    direction: 'min',
    // Deliberately use non-numeric object keys: node id, not insertion/key order, defines discovery.
    nodes: {
      late: node(2, 1, 'late winner', [1]),
      first: node(1, 10, 'first explored'),
      revisit: node(3, 9, 'first explored', [1]),
    },
  }
  const before = directionProfit(state)
  assert.deepEqual(before.map(row => row.direction), ['first explored', 'late winner'])
  assert.ok(before[1].gain > before[0].gain, 'the stronger result must not silently move its control')

  const after = directionProfit({
    ...state,
    nodes: { ...state.nodes, late: { ...state.nodes.late, metric: 20 } },
  })
  assert.deepEqual(after.map(row => row.direction), ['first explored', 'late winner'])
})

test('direction projection safely accepts agent-authored property-like names', () => {
  const rows = directionProfit({ direction: 'max', nodes: {
    a: node(1, 1, '__proto__'), b: node(2, 2, 'constructor', [1]),
  } })
  assert.deepEqual(rows.map(row => row.direction), ['__proto__', 'constructor'])
  assert.deepEqual(rows.map(row => row.count), [1, 1])
})

test('optimization and grouping labels use user-facing language without changing wire values', () => {
  assert.equal(optimizationLabel('min'), 'minimize')
  assert.equal(optimizationLabel('max'), 'maximize')
  assert.equal(optimizationLabel('legacy'), 'unknown')
  assert.deepEqual(GROUP_MODES[0], ['theme', 'direction'])
  assert.match(toMarkdown({ direction: 'max', nodes: {}, task_id: 't', run_id: 'r' }), /\*\*Optimization orientation:\*\* maximize/)
})

test('Directions renders a persistent, programmatically linked non-causal caveat', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: DirectionsOverview } = await vite.ssrLoadModule('/src/DirectionsOverview.jsx')
    const markup = renderToStaticMarkup(React.createElement(DirectionsOverview, {
      state: { direction: 'max', nodes: { 1: node(1, 1, 'Explore retrieval') } },
      active: null, onPick() {},
    }))
    const document = new JSDOM(markup).window.document
    assert.equal(document.querySelector('.do-head strong')?.textContent, 'Directions')
    const group = document.querySelector('[role="group"]')
    const caveat = document.getElementById(group?.getAttribute('aria-describedby'))
    assert.ok(caveat, 'aria-describedby must resolve to persistent visible copy')
    assert.match(caveat.textContent, /Optimization: maximize/)
    assert.match(caveat.textContent, /descriptive only; not a causal effect or winner claim/i)
    assert.equal(group.querySelector('.do-name')?.textContent, 'Explore retrieval')
  } finally {
    await vite.close()
  }
})
