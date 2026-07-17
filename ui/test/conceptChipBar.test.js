// View 2 ConceptChipBar wiring test. The pure model (chip counts, breadcrumb, matching set) is covered
// by conceptChips.test.js; here we verify the component wires the model to render + the onHighlight
// callback. Static markup via react-dom/server; interaction via react-dom/client + act. `node --test`.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React, { act } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const STATE = {
  node_concepts: {
    0: ['loss/contrastive/dcl', 'architecture/moe'],
    1: ['loss/contrastive/mnr'],
    2: ['loss/mnr'],
    3: ['data/synth'],
    4: ['loss'],
  },
}

test('ConceptChipBar renders nothing when the run carries no concepts', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ConceptChipBar } = await vite.ssrLoadModule('/src/ConceptChipBar.jsx')
    const markup = renderToStaticMarkup(React.createElement(ConceptChipBar, {
      state: { node_concepts: {} }, onHighlight() {},
    }))
    assert.equal(markup, '')
  } finally {
    await vite.close()
  }
})

test('ConceptChipBar renders top-level concept chips with subtree counts', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ConceptChipBar } = await vite.ssrLoadModule('/src/ConceptChipBar.jsx')
    const markup = renderToStaticMarkup(React.createElement(ConceptChipBar, {
      state: STATE, onHighlight() {},
    }))
    const document = new JSDOM(markup).window.document
    assert.equal(document.querySelector('.cb-head strong')?.textContent, 'Concepts')
    const names = [...document.querySelectorAll('.cb-chip .cb-name')].map(n => n.textContent)
    assert.ok(names.includes('loss') && names.includes('architecture') && names.includes('data'))
    // loss subtree touches nodes 0,1,2,4 -> count 4, and sorts first (count desc)
    const first = document.querySelector('.cb-chip')
    assert.equal(first.querySelector('.cb-name').textContent, 'loss')
    assert.equal(first.querySelector('.cb-count').textContent, '4')
  } finally {
    await vite.close()
  }
})

test('selecting a chip pushes the matching node set to onHighlight; clear resets it', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: cb => setTimeout(cb, 0), cancelAnimationFrame: h => clearTimeout(h),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const calls = []
  let root, vite
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: ConceptChipBar }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ConceptChipBar.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: STATE, onHighlight: v => calls.push(v),
    })))
    // mount effect pushes the initial (empty selection -> null) highlight
    assert.equal(calls.at(-1), null)

    // click the "architecture" chip -> highlight node 0 only (architecture/moe)
    const arch = [...document.querySelectorAll('.cb-chip')].find(
      c => c.querySelector('.cb-name')?.textContent === 'architecture')
    await act(async () => {
      arch.querySelector('.cb-chip-main').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    const hi = calls.at(-1)
    assert.ok(hi instanceof Set)
    assert.deepEqual([...hi].sort((a, b) => a - b), [0])
    assert.equal(document.querySelector('.cb-pill-label')?.textContent, 'architecture')

    // A nested selection must retain its full path: duplicate leaves are otherwise indistinguishable.
    await act(async () => {
      arch.querySelector('.cb-drill').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    const moe = [...document.querySelectorAll('.cb-chip')].find(
      c => c.querySelector('.cb-name')?.textContent === 'moe')
    await act(async () => {
      moe.querySelector('.cb-chip-main').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.deepEqual([...document.querySelectorAll('.cb-pill-label')].map(node => node.textContent),
      ['architecture', 'architecture/moe'])

    // A live consolidation rename also updates an already-selected legacy key's visible identity.
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: { ...STATE, concept_consolidation: { 'architecture/moe': 'model/moe' } },
      onHighlight: v => calls.push(v),
    })))
    assert.deepEqual([...document.querySelectorAll('.cb-pill-label')].map(node => node.textContent),
      ['architecture', 'model/moe'])

    // A live reset that removes the last concept must clear graph dimming before hiding the bar.
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: { node_concepts: {} }, onHighlight: v => calls.push(v),
    })))
    assert.equal(document.querySelector('.concept-bar'), null)
    assert.equal(calls.at(-1), null,
      'an empty live concept projection cannot strand the DAG fully dimmed without controls')

    // If concepts return, the vanished projection must not restore an invisible stale selection.
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: STATE, onHighlight: v => calls.push(v),
    })))
    assert.equal(document.querySelector('.cb-pill'), null)
    assert.equal(calls.at(-1), null)

    const restoredArch = [...document.querySelectorAll('.cb-chip')].find(
      c => c.querySelector('.cb-name')?.textContent === 'architecture')
    await act(async () => {
      restoredArch.querySelector('.cb-chip-main').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    // a removable pill appears; clicking it clears the selection -> null highlight
    const pill = document.querySelector('.cb-pill')
    assert.ok(pill)
    await act(async () => {
      pill.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(calls.at(-1), null)
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
})

test('concepts vanishing while selected clears the highlight instead of stranding the graph dimmed', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: cb => setTimeout(cb, 0), cancelAnimationFrame: h => clearTimeout(h),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const calls = []
  let root, vite
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: ConceptChipBar }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ConceptChipBar.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: STATE, onHighlight: v => calls.push(v),
    })))
    // select a concept -> non-null highlight, bar visible
    const arch = [...document.querySelectorAll('.cb-chip')].find(
      c => c.querySelector('.cb-name')?.textContent === 'architecture')
    await act(async () => {
      arch.querySelector('.cb-chip-main').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.ok(calls.at(-1) instanceof Set)

    // a live tick empties node_concepts (all tagged nodes reset): the bar hides, but the highlight must
    // be cleared to null so the graph is not stranded fully dimmed with no controls.
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: { node_concepts: {} }, onHighlight: v => calls.push(v),
    })))
    await act(async () => { await Promise.resolve() })
    assert.equal(document.querySelector('.concept-bar'), null)   // bar hidden
    assert.equal(calls.at(-1), null)                             // highlight cleared, not a stuck empty Set
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
})
