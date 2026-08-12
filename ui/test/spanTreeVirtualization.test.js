import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import axe from 'axe-core'
import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

import {
  flattenSpanTree, spanTreeMatches,
} from '../src/spanTreeModel.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const operation = (id, children = [], extra = {}) => ({
  span_id: id, trace_id: 'trace', name: 'implement', kind: 'operation', status: 'OK',
  start: Number(id.replace(/\D/g, '')) || 0, duration_s: 0.01, attributes: {}, children, ...extra,
})

test('the logical span tree is iterative, topology-complete, and indexes bounded root evidence', () => {
  let deep = null
  for (let index = 4_999; index >= 0; index -= 1) deep = operation(`deep-${index}`, deep ? [deep] : [],
    index === 4_999 ? { attributes: { reason: 'leaf needle' } } : {})
  deep.events = [{ name: 'llm_call', status: 'legacy marker' },
    { name: 'captured', Messages: [{ Content: 'SECRET_NEEDLE' }] }]
  const rows = flattenSpanTree([deep])

  assert.equal(rows.length, 5_000, 'no local cap may drop a server-returned span')
  assert.equal(rows[0].level, 1)
  assert.equal(rows.at(-1).level, 5_000)
  assert.equal(rows.at(-1).parent, rows.length - 2)
  assert.deepEqual(spanTreeMatches(rows, 'legacy marker'), [0],
    'root events remain searchable even when the root has children')
  assert.deepEqual(spanTreeMatches(rows, 'leaf needle'), [4_999])
  assert.deepEqual(spanTreeMatches(rows, 'SECRET_NEEDLE'), [],
    'payload aliases are classified case-insensitively and never enter the search index')
  const hostile = operation('bounded-search', [], { attributes: {
    metric: 'known metadata needle',
    metadata: { legacy: 'SECRET_NESTED', output: 'x'.repeat(1_000_000) },
  } })
  const hostileRows = flattenSpanTree([hostile])
  assert.deepEqual(spanTreeMatches(hostileRows, 'known metadata needle'), [0])
  assert.deepEqual(spanTreeMatches(hostileRows, 'SECRET_NESTED'), [],
    'unknown nested metadata stays outside the privacy-bounded index')
  assert.deepEqual(spanTreeMatches(hostileRows, 'xxxxxxxxxxxxxxxx'), [],
    'search does not inspect captured I/O even when it is nested in otherwise useful metadata')
})

test('logical row keys stay unique and ARIA sibling metadata ignores malformed children', () => {
  const root = operation('x', [null, operation('child-a'), 'invalid', operation('child-b')])
  const rows = flattenSpanTree([root, operation('x'), operation('x:2'), null])

  assert.equal(new Set(rows.map(row => row.key)).size, rows.length,
    'source ids and synthesized duplicate suffixes may never collide')
  assert.deepEqual(rows.slice(0, 3).map(row => [row.span.span_id, row.pos, row.size]), [
    ['x', 1, 3], ['child-a', 1, 2], ['child-b', 2, 2],
  ])
  assert.deepEqual(rows.filter(row => row.level === 1).map(row => [row.pos, row.size]), [
    [1, 3], [2, 3], [3, 3],
  ])
})

const installDom = () => {
  const dom = new JSDOM('<!doctype html><html lang="en"><head><title>Span tree</title></head>'
    + '<body><main><h1>Trace</h1><div id="root"></div></main></body></html>',
  { url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only' })
  for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
    'MouseEvent', 'KeyboardEvent', 'CustomEvent', 'FocusEvent', 'getComputedStyle',
    'requestAnimationFrame', 'cancelAnimationFrame']) {
    Object.defineProperty(globalThis, name,
      { configurable: true, writable: true, value: dom.window[name] })
  }
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT',
    { configurable: true, writable: true, value: true })
  Object.defineProperty(dom.window.HTMLElement.prototype, 'clientHeight', {
    configurable: true, get() { return this.classList?.contains('span-tree-virtual') ? 320 : 30 },
  })
  dom.window.HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
    return { x: 0, y: 0, top: 0, left: 0, right: 600, bottom: 30, width: 600, height: 30,
      toJSON() { return this } }
  }
  return dom
}

const stressForest = () => {
  let deep = null
  for (let index = 2_047; index >= 0; index -= 1) deep = operation(`deep-${index}`, deep ? [deep] : [],
    index === 2_047 ? { attributes: { reason: 'needle-final' } } : {})
  const generation = {
    span_id: 'generation-one', trace_id: 'trace', name: 'chat', kind: 'generation', status: 'OK',
    start: 1, duration_s: 0.2, attributes: { model: 'test-model', op: 'chat' }, children: [],
  }
  const wide = Array.from({ length: 2_045 }, (_, index) => operation(`wide-${index}`))
  return operation('root-0', [generation, ...wide, deep], {
    attributes: { stage: 'author', root_marker: 'root evidence' },
    events: [{ name: 'llm_call', status: 'legacy root event' }],
  })
}

test('extreme span trees keep a bounded DOM and retain selection, disclosure and detail across windows',
  async () => {
    const dom = installDom()
    let detailReads = 0
    globalThis.fetch = async url => {
      assert.match(String(url), /\/spans\/generation-one/)
      detailReads += 1
      return { ok: true, status: 200, json: async () => ({
        span_id: 'generation-one', attributes: {
          model: 'test-model', op: 'chat', output: 'cached detail', thinking: 'retained reasoning',
        }, projection: {},
      }) }
    }
    const vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    let root
    try {
      const { NodeTrace } = await vite.ssrLoadModule('/src/Inspector.jsx')
      const { createRoot } = await import('react-dom/client')
      const { act } = React
      const container = dom.window.document.getElementById('root')
      root = createRoot(container)
      const forest = stressForest()
      const render = (span = forest, treeKey = 'scope-a') => React.createElement(NodeTrace, {
        spans: [span], runId: 'demo', treeKey,
        projection: { total_spans: 4_096, visible_spans: 4_096, omitted_spans: 0 },
      })
      const settle = async () => {
        await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
      }
      await act(async () => { root.render(render()) })
      await settle()

      const tree = container.querySelector('[role="tree"]')
      assert.ok(tree)
      assert.match(tree.getAttribute('aria-label'), /4095 observations/)
      assert.ok(container.querySelectorAll('[role="treeitem"]').length <= 24,
        'one global virtual window, not a per-depth cap, owns mounted rows')
      assert.ok(dom.window.document.getElementById(tree.getAttribute('aria-activedescendant')),
        'the active descendant is mounted')
      dom.window.eval(axe.source)
      const accessibility = await dom.window.axe.run(dom.window.document, {
        runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
        rules: { 'color-contrast': { enabled: false } },
      })
      const blocking = accessibility.violations.filter(violation =>
        violation.impact === 'serious' || violation.impact === 'critical')
      assert.equal(blocking.length, 0, JSON.stringify(blocking.map(violation => ({
        id: violation.id, impact: violation.impact,
        nodes: violation.nodes.map(node => ({ target: node.target,
          failureSummary: node.failureSummary })),
      })), null, 2))

      // The tree owns one keyboard stop. Its row disclosures are not additional tab stops, and
      // Space/Enter toggle detail without moving focus away from the active-descendant owner.
      tree.focus()
      assert.equal(dom.window.document.activeElement, tree)
      await act(async () => tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
        key: ' ', bubbles: true,
      })))
      assert.match(container.textContent, /root evidence/)
      assert.match(container.textContent, /llm_call/)

      // Load and expand generation I/O, including the nested debug disclosure.
      await act(async () => tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
        key: 'ArrowRight', bubbles: true,
      })))
      assert.equal(container.querySelector('.span-row.gen').tabIndex, -1)
      assert.equal(dom.window.document.activeElement, tree)
      await act(async () => tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
        key: 'Enter', bubbles: true,
      })))
      await settle()
      assert.match(container.textContent, /cached detail/)
      const generationActive = tree.getAttribute('aria-activedescendant')
      await act(async () => tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
        key: 'ArrowLeft', bubbles: true,
      })))
      assert.ok(dom.window.document.getElementById(tree.getAttribute('aria-activedescendant'))
        .querySelector('.span-stage-row'), 'ArrowLeft selects the exact logical parent')
      await act(async () => tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
        key: 'ArrowRight', bubbles: true,
      })))
      assert.equal(tree.getAttribute('aria-activedescendant'), generationActive,
        'ArrowRight returns to the first logical child')
      await act(async () => container.querySelector('.think-debug button').click())
      assert.match(container.textContent, /retained reasoning/)
      assert.equal(detailReads, 1)

      // Jump thousands of logical rows. The old row unmounts, while the new active row is mounted
      // immediately (not after mounting the gap) and the DOM remains viewport-bounded.
      await act(async () => {
        tree.focus()
        tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'End', bubbles: true }))
      })
      await settle()
      assert.equal(container.querySelector('.span-row.gen'), null)
      assert.ok(dom.window.document.getElementById(tree.getAttribute('aria-activedescendant')))
      assert.ok(container.querySelectorAll('[role="treeitem"]').length <= 24)

      await act(async () => {
        tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Home', bubbles: true }))
      })
      await settle()
      assert.match(container.textContent, /cached detail/,
        'hoisted detail survives a virtual unmount/remount')
      assert.equal(container.querySelector('.think-debug button').getAttribute('aria-expanded'), 'true')
      assert.equal(detailReads, 1, 'remounting a confirmed detail does not fetch it again')

      // Search indexes the whole logical tree, not just mounted rows, and a far match remains an
      // honest active descendant throughout the jump.
      const search = container.querySelector('.span-tree-search')
      await act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value')
          .set.call(search, 'needle-final')
        search.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
      })
      await settle()
      assert.match(container.querySelector('.span-tree-count').textContent, /1 of 1/)
      assert.ok(dom.window.document.getElementById(tree.getAttribute('aria-activedescendant')))

      // Same lifecycle polling can replace/append topology without losing retained disclosure state.
      const updated = { ...forest, children: [...forest.children, operation('wide-new')] }
      await act(async () => { root.render(render(updated)) })
      await settle()
      await act(async () => {
        tree.focus()
        tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Home', bubbles: true }))
      })
      await settle()
      assert.match(container.textContent, /cached detail/)
      assert.equal(detailReads, 1)

      // A fixed live window can evict an expanded row. Its cached I/O must be released rather than
      // growing an unbounded off-window Map for the lifetime of the Inspector.
      const withoutGeneration = { ...updated, children: updated.children.slice(1) }
      await act(async () => { root.render(render(withoutGeneration)) })
      await settle()
      assert.equal(container.querySelector('.span-row.gen'), null)
      await act(async () => { root.render(render(updated)) })
      await settle()
      await act(async () => {
        tree.focus()
        tree.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Home', bubbles: true }))
      })
      await settle()
      assert.equal(container.querySelector('.span-row.gen').getAttribute('aria-expanded'), 'false',
        'an evicted id does not recover stale disclosure state if it later reappears')
      await act(async () => container.querySelector('.span-row.gen').click())
      await settle()
      assert.equal(detailReads, 2, 'evicted detail is fetched again if the span reappears')

      // A run/node lifecycle identity change remounts the state owner: no stale expansion or search
      // can leak across Replay/reset even when a server happens to reuse a span id.
      await act(async () => { root.render(render(updated, 'scope-b')) })
      await settle()
      assert.equal(container.querySelector('.span-detail'), null)
      assert.equal(container.querySelector('.span-tree-search').value, '')
      assert.ok(dom.window.document.getElementById(
        container.querySelector('[role="tree"]').getAttribute('aria-activedescendant')))
    } finally {
      if (root) {
        await React.act(async () => root.unmount())
      }
      await vite.close()
      dom.window.close()
    }
  })
