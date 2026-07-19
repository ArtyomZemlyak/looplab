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
  nodes: Object.fromEntries(Array.from({ length: 5 }, (_, id) => [id, { id }])),
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

test('ConceptChipBar distinguishes partial and unavailable membership from an empty run', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ConceptChipBar } = await vite.ssrLoadModule('/src/ConceptChipBar.jsx')
    const render = state => new JSDOM(renderToStaticMarkup(React.createElement(ConceptChipBar, {
      state, onHighlight() {},
    }))).window.document
    const partial = render({ ...STATE, node_concept_materialization_receipts: {
      0: { status: 'partial', reasons: ['concepts_per_node_cap'] },
    } })
    assert.equal(partial.querySelector('.concept-bar')?.getAttribute('role'), 'status')
    assert.match(partial.body.textContent, /PARTIAL.*display-only.*filters off/s)
    assert.equal(partial.querySelector('.cb-chip'), null)
    assert.equal(partial.querySelector('[aria-label="Search concepts"]'), null)

    const unavailable = render({ nodes: {}, node_concepts: {}, run_base_concept_receipt: {
      status: 'unavailable', reasons: ['delta_dependency_cycle'],
    } })
    assert.equal(unavailable.querySelector('.concept-bar')?.getAttribute('role'), 'alert')
    assert.match(unavailable.body.textContent, /UNAVAILABLE.*not empty/s)
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
    // Counts remain live, but canonical identity order keeps controls from jumping as evidence changes.
    const first = document.querySelector('.cb-chip')
    assert.equal(first.querySelector('.cb-name').textContent, 'architecture')
    assert.equal(first.querySelector('.cb-count').textContent, '1')
  } finally {
    await vite.close()
  }
})

test('ConceptChipBar excludes tombstoned and run-level aborted memberships', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ConceptChipBar } = await vite.ssrLoadModule('/src/ConceptChipBar.jsx')
    const markup = renderToStaticMarkup(React.createElement(ConceptChipBar, {
      state: {
        nodes: { 0: { id: 0 }, 1: { id: 1, tombstoned: true }, 2: { id: 2 } },
        aborted_nodes: [2],
        node_concepts: { 0: ['active/one'], 1: ['deleted/one'], 2: ['aborted/one'] },
      },
      onHighlight() {},
    }))
    const names = [...new JSDOM(markup).window.document.querySelectorAll('.cb-name')]
      .map(node => node.textContent)
    assert.deepEqual(names, ['active'])
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

    // click the "loss" chip -> highlight its whole subtree (0,1,2,4)
    const loss = [...document.querySelectorAll('.cb-chip')].find(
      c => c.querySelector('.cb-name')?.textContent === 'loss')
    await act(async () => {
      loss.querySelector('.cb-chip-main').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    const hi = calls.at(-1)
    assert.ok(hi instanceof Set)
    assert.deepEqual([...hi].sort((a, b) => a - b), [0, 1, 2, 4])
    assert.equal(document.querySelector('.cb-pill-label')?.textContent, 'loss')

    // A nested selection must retain its full path, but replace its plain ancestor: OR-ing both would
    // leave the ancestor authoritative and make the apparent child refinement ineffective.
    await act(async () => {
      loss.querySelector('.cb-drill').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    const contrastive = [...document.querySelectorAll('.cb-chip')].find(
      c => c.querySelector('.cb-name')?.textContent === 'contrastive')
    await act(async () => {
      contrastive.querySelector('.cb-chip-main').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.deepEqual([...calls.at(-1)].sort((a, b) => a - b), [0, 1],
      'the child replaces its OR ancestor and genuinely narrows the graph highlight')
    assert.deepEqual([...document.querySelectorAll('.cb-pill-label')].map(node => node.textContent),
      ['loss/contrastive'])

    // A live consolidation rename also updates an already-selected legacy key's visible identity.
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: { ...STATE, concept_consolidation: { 'loss/contrastive': 'objective/contrastive' } },
      onHighlight: v => calls.push(v),
    })))
    assert.deepEqual([...document.querySelectorAll('.cb-pill-label')].map(node => node.textContent),
      ['objective/contrastive'])

    // A partial live receipt keeps its warning visible but atomically removes authoritative filters.
    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: { ...STATE, node_concept_materialization_receipts: {
        0: { status: 'partial', reasons: ['concepts_per_node_cap'] },
      } },
      onHighlight: v => calls.push(v),
    })))
    assert.match(document.querySelector('[role="status"]').textContent, /PARTIAL/)
    assert.equal(document.querySelector('.cb-chip'), null)
    assert.equal(calls.at(-1), null, 'partial membership cannot strand an authoritative DAG filter')

    await act(async () => root.render(React.createElement(ConceptChipBar, {
      state: STATE, onHighlight: v => calls.push(v),
    })))
    assert.equal(document.querySelector('.cb-pill'), null,
      'restoring complete membership cannot resurrect a stale selection')

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

test('search previews a graph highlight and commits a concept on result click', async () => {
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
  const setValue = (input, value) => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value)
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  }
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

    // open the search box, type "loss" -> live preview highlight over the whole loss subtree (0,1,2,4)
    await act(async () => {
      document.querySelector('.cs-icon').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    // A live SSE update can add results without changing the query. The keyboard cursor must move
    // from -1 into the new list so Enter works and aria-selected names the active option.
    await act(async () => { setValue(document.querySelector('.cs-input'), 'fresh'); await Promise.resolve() })
    assert.equal(document.querySelector('.cs-res'), null)
    await act(async () => {
      root.render(React.createElement(ConceptChipBar, {
        state: { nodes: { ...STATE.nodes, 5: { id: 5 } },
          node_concepts: { ...STATE.node_concepts, 5: ['fresh/axis'] } },
        onHighlight: v => calls.push(v),
      }))
      await Promise.resolve()
    })
    const freshInput = document.querySelector('.cs-input')
    const freshResult = document.querySelector('.cs-res')
    assert.equal(freshResult?.getAttribute('aria-selected'), 'true')
    assert.equal(freshInput.getAttribute('aria-autocomplete'), 'list')
    assert.ok(freshResult.id, 'each option has a stable DOM id')
    assert.equal(freshInput.getAttribute('aria-activedescendant'), freshResult.id)
    assert.equal(document.getElementById(freshInput.getAttribute('aria-controls')), freshResult.parentElement)
    assert.equal(freshResult.tabIndex, -1, 'DOM focus remains on the combobox input')
    await act(async () => {
      document.querySelector('.cs-input').dispatchEvent(
        new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(document.querySelector('.cb-pill-label')?.textContent, 'fresh')
    await act(async () => {
      document.querySelector('.cb-pill').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    await act(async () => { setValue(document.querySelector('.cs-input'), 'loss'); await Promise.resolve() })
    const previewHi = calls.at(-1)
    assert.ok(previewHi instanceof Set)
    assert.deepEqual([...previewHi].sort((a, b) => a - b), [0, 1, 2, 4])
    // ranked results are listed; the exact-leaf `loss` ranks first
    const resultNames = [...document.querySelectorAll('.cs-res')].map(r => r.textContent)
    assert.ok(resultNames.some(t => t.includes('loss')))
    const input = document.querySelector('.cs-input')
    const options = [...document.querySelectorAll('.cs-res')]
    const stableOptionIds = options.map(option => option.id)
    assert.equal(input.getAttribute('aria-activedescendant'), options[0].id)
    await act(async () => {
      input.focus()
      input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(document.activeElement, input)
    assert.equal(input.getAttribute('aria-activedescendant'), options[1].id)
    assert.equal(options[1].getAttribute('aria-selected'), 'true')
    assert.deepEqual([...document.querySelectorAll('.cs-res')].map(option => option.id), stableOptionIds,
      'moving the keyboard cursor must not rewrite option identity')
    // no pinned selection yet
    assert.equal(document.querySelector('.cb-pill'), null)

    // click the first result -> pins `loss`, clears the query, keeps the committed highlight
    await act(async () => {
      document.querySelector('.cs-res').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(document.querySelector('.cs-input').value, '')            // query cleared
    assert.equal(document.querySelector('.cs-pop'), null)                  // dropdown gone
    assert.equal(document.querySelector('.cb-pill-label')?.textContent, 'loss')
    assert.deepEqual([...calls.at(-1)].sort((a, b) => a - b), [0, 1, 2, 4]) // committed = same set
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

test('a non-matching query shows an empty state and dims nothing', async () => {
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
  const setValue = (input, value) => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value)
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  }
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
    await act(async () => {
      document.querySelector('.cs-icon').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    await act(async () => { setValue(document.querySelector('.cs-input'), 'zzznope'); await Promise.resolve() })
    assert.ok(document.querySelector('.cs-empty'))                         // empty state shown
    assert.equal(calls.at(-1), null)                                      // no dimming (null, not empty Set)
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
