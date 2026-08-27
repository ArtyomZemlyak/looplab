// ONE trace surface, driven where the operator actually meets it.
//
// The report that produced this change, in the operator's own order:
//   1. the Developer trace had disappeared from the card — its section opened a two-span stub
//      (`Author node` → `materialize_node`) instead of the node's build and evaluation;
//   2. stop building a second, poorer trace element: reuse the node Trace tab's;
//   3. the research disclosure was a solid white slab;
//   4. its label sat BESIDE the trace, squeezing the trace into a narrow column;
//   5. the research trace could not be switched to the readable view with the main control.
//
// (1) is the one a source pin could never have caught: the card's rollup and the tree it opened
// disagreed about the same node — 61 spans against 2, measured on
// `vec-backups/rubertlite-dr-unified-v3.deleted-2026-08-12` node 0 — and both halves compiled.
// So every assertion below reads the REQUESTS the mounted UI issues and the rows that reach the
// screen. The two layout/styling points are driven against the real stylesheet, because "off-style"
// is a fact about the design system's rules, not about a class name.
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const src = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')

const NODE_TRACE_ID = 'authoring-trace-0001'   // where the node was AUTHORED: two spans, no build
const PROPOSE_TRACE_ID = 'propose-trace-0001'

// The card payload as the server projects it: the node row's `spans` counts the NODE's spans while
// its `trace_id` names only the authoring trace. That gap IS the defect.
const CARD_TRACE = {
  schema: 2,
  card_id: 'card-many',
  research: [{
    name: 'propose', trace_id: PROPOSE_TRACE_ID, span_id: 'propose-root', start: 0, duration_s: 9,
    status: 'OK', link: 'card_id', spans: 252, generations: 86, tools: 165,
    tokens: { total: 2286 },
  }],
  nodes: [{
    node_id: '4', trace_id: NODE_TRACE_ID, spans: 61, generations: 12, tools: 30,
    tokens: { total: 91000 }, errors: 0,
  }],
  projection: { schema: 2, truncated: false, omitted_spans: 0 },
}

// `run_generation` is not decoration: every trace read is fenced on it, so a payload without one is
// refused and the surface reports "unavailable" — which is how the server's own echo is checked.
const RUN_GENERATION = 'a'.repeat(64)

const conversationBody = (identity, label, turnText) => ({
  schema: 2, run_id: 'run-1', run_generation: RUN_GENERATION, ...identity,
  stages: [{
    trace_id: 'trace-x', label, start: 0,
    rollup: { generations: 1, tools: 0, tokens: {} },
    turns: [{ type: 'generation', output: turnText, usage: {} }],
  }],
  projection: {
    schema: 2, truncated: false, total_spans: 4, visible_spans: 4, omitted_spans: 0,
    total_stages: 1, visible_stages: 1, omitted_stages: 0,
    total_turns: 1, visible_turns: 1, omitted_turns: 0,
  },
})

const spanTreeBody = (identity, spans) => ({
  schema: 2, run_generation: RUN_GENERATION, ...identity, spans,
  nodes: spans,
  rollup: { generations: 1, tools: 0, tokens: {} },
  projection: {
    schema: 2, truncated: false, total_spans: spans.length, visible_spans: spans.length,
    omitted_spans: 0,
  },
})

const span = (name, id) => ({
  name, kind: 'operation', span_id: id, parent_id: null, trace_id: 'trace-x',
  attributes: {}, events: [], status: 'OK', start: 0, duration_s: 0.4, children: [],
})

const installDom = () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
    { url: 'http://localhost/', pretendToBeVisual: true })
  for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
    'MouseEvent', 'CustomEvent', 'FocusEvent', 'getComputedStyle', 'requestAnimationFrame',
    'cancelAnimationFrame']) {
    // `defineProperty`, not assignment — the house pattern: node ships `globalThis.navigator` as an
    // accessor with no setter, so a plain assignment takes down the whole file.
    Object.defineProperty(globalThis, name,
      { configurable: true, writable: true, value: dom.window[name] })
  }
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT',
    { configurable: true, writable: true, value: true })
  // jsdom has no IntersectionObserver and the reach sentinel constructs one on mount.
  class NoopObserver {
    observe() {} unobserve() {} disconnect() {}
  }
  Object.defineProperty(globalThis, 'IntersectionObserver',
    { configurable: true, writable: true, value: NoopObserver })
  return dom
}

const STATE = {
  // NO `generation` KEY, deliberately. The folded RunState has no run-level `generation` — it is
  // an envelope SIBLING of `state` in the /api/state payload — so stubbing one here produced a
  // shape production never produces, and it masked the board reading `state?.generation` (always
  // undefined live) instead of the `runGeneration` prop it already receives. The fence contract
  // this file proves passed here and could never pass live. Keep it absent so it cannot again.
  cards: {
    'card-many': {
      id: 'card-many', status: 'evaluated', verdict: 'supported',
      statement: 'Log-transform the target', evidence: [4], selection_ready: false,
    },
  },
  cards_projection: { source_valid: true, total: 1, returned: 1, omitted: 0, complete: true, items: {} },
  nodes: {
    4: { id: 4, status: 'evaluated', metric: 0.81, attempt: 0,
      idea: { operator: 'draft', card_id: 'card-many' } },
  },
}

// One fetch stub for every surface here: it records each path and answers by route, so a test can
// ask the only question that matters — WHICH trace did the UI go and read?
const installFetch = () => {
  const paths = []
  globalThis.fetch = async url => {
    const path = String(url)
    paths.push(path)
    const body = (() => {
      if (path.includes('/cards/')) return CARD_TRACE
      if (/\/nodes\/\d+\/conversation/.test(path)) {
        return conversationBody({ node_id: '4', attempt: 0 }, 'implement', 'the developer wrote code')
      }
      if (/\/nodes\/\d+\/trace/.test(path)) {
        return spanTreeBody({ node_id: 4, attempt: 0 },
          [span('create_node', 's1'), span('train', 's2')])
      }
      if (/\/by_trace\/[^/]+\/conversation/.test(path)) {
        return conversationBody({ trace_id: PROPOSE_TRACE_ID }, 'propose', 'the researcher argued')
      }
      if (path.includes('/by_trace/')) {
        return spanTreeBody({ trace_id: PROPOSE_TRACE_ID }, [span('propose', 'p1')])
      }
      return {}
    })()
    return { ok: true, status: 200, headers: { get: () => null }, json: async () => body }
  }
  return paths
}

const mount = async (element) => {
  const dom = installDom()
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  const { createRoot } = await import('react-dom/client')
  const { act } = await import('react-dom/test-utils')
  const container = dom.window.document.getElementById('root')
  const root = createRoot(container)
  const settle = async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  }
  const click = async node => {
    assert.ok(node, 'nothing to click')
    await act(async () => {
      node.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    })
    await settle()
  }
  const buttonWith = text => [...container.querySelectorAll('button')]
    .find(node => node.textContent.includes(text))
  // Scoped, because two surfaces are on screen at once on the card and both carry the same
  // controls — which is the point of them being the same surface.
  const buttonIn = (selector, text) => [...container.querySelectorAll(`${selector} button`)]
    .find(node => node.textContent.includes(text))
  await act(async () => { root.render(await element(vite)) })
  await settle()
  // The surface arrives through a real dynamic import (the card loads it lazily so the board's
  // chunk stays free of the Inspector), so nothing here can assume a fixed number of ticks.
  const waitFor = async (predicate, what) => {
    for (let tick = 0; tick < 200; tick += 1) {
      if (predicate()) return
      await settle()
    }
    assert.fail(`timed out waiting for ${what}`)
  }
  return {
    container,
    settle,
    click,
    buttonWith,
    buttonIn,
    waitFor,
    close: async () => {
      await act(async () => { root.unmount() })
      await vite.close()
      dom.window.close()
    },
  }
}

test('the card opens the NODE’s trace, not the trace the node was authored in', async () => {
  const paths = installFetch()
  const view = await mount(async vite => {
    const { CardWorkspace } = await vite.ssrLoadModule('/src/CardBoard.jsx')
    return React.createElement(CardWorkspace, {
      state: STATE, runId: 'run-1', runGeneration: STATE.generation, onToast: () => {},
      selectedCardId: 'card-many', onSelectCard: () => {}, selectedNodeId: null,
      onSelectNode: () => {}, renderInspector: null, pane: null, onSelect: () => {},
    })
  })
  try {
    await view.click(view.buttonWith('Developer · build and evaluation'))
    // The bands arrive collapsed, exactly as they do in the node's own tab.
    await view.waitFor(() => /Developer · implement/.test(view.container.textContent),
      'the Developer’s conversation to reach the screen')
    await view.click(view.buttonIn('.card-trace-body', 'expand all'))
    assert.match(view.container.textContent, /the developer wrote code/,
      'and its turns are readable, not just counted')

    assert.equal(paths.filter(path => path.includes(NODE_TRACE_ID)).length, 0,
      'the authoring trace is a two-span stub and must never be what the Developer section opens')
    const conversation = paths.filter(path => /\/nodes\/4\/conversation/.test(path))
    assert.equal(conversation.length, 1, 'the Developer section reads the node’s own trace')
    assert.match(conversation[0], /\/nodes\/4\/conversation\?attempt=0&limit=512/,
      '…for the attempt this snapshot knows about, at the shared default window')

    // (5) THE MAIN CONTROL. The same switcher the node tab has, doing the same thing.
    await view.click(view.buttonIn('.card-trace-body', 'span tree'))
    await view.waitFor(() => paths.some(path => /\/nodes\/4\/trace\?/.test(path)),
      'the span-tree read')
    assert.ok(paths.some(path => /\/nodes\/4\/trace\?attempt=0&limit=512/.test(path)),
      'switching the view must read the same node’s span tree')
    // The tree renders the operation's OPERATOR-FACING name (`stageMeta`), which is the whole
    // point of reusing the surface rather than dumping raw span rows.
    assert.match(view.container.textContent, /Author node/)
    // …and the span tree it renders is the searchable one, not a bare list.
    assert.ok(view.container.querySelector('.span-tree-search') != null,
      'the card must get the surface’s search, not a poorer copy of the tree')
  } finally {
    await view.close()
  }
})

test('research reads as a conversation, and the main control switches it', async () => {
  const paths = installFetch()
  const view = await mount(async vite => {
    const { CardWorkspace } = await vite.ssrLoadModule('/src/CardBoard.jsx')
    return React.createElement(CardWorkspace, {
      state: STATE, runId: 'run-1', runGeneration: STATE.generation, onToast: () => {},
      selectedCardId: 'card-many', onSelectCard: () => {}, selectedNodeId: null,
      onSelectNode: () => {}, renderInspector: null, pane: null, onSelect: () => {},
    })
  })
  try {
    await view.waitFor(() => /Researcher · propose/.test(view.container.textContent),
      'the proposal’s conversation to reach the screen')
    await view.click(view.buttonIn('.research-trace', 'expand all'))
    assert.match(view.container.textContent, /the researcher argued/)
    // A single proposal opens with the section: the operator came here to read it.
    const conversation = paths.filter(
      path => path.includes(`/by_trace/${PROPOSE_TRACE_ID}/conversation`))
    assert.equal(conversation.length, 1,
      'the proposal must be readable as the conversation, not only as a span tree')

    await view.click(view.buttonIn('.research-trace', 'span tree'))
    await view.waitFor(() => paths.some(path => /\/by_trace\/propose-trace-0001\?/.test(path)),
      'the proposal’s span-tree read')
    assert.ok(paths.some(path => /\/by_trace\/propose-trace-0001\?limit=512/.test(path)),
      'the switcher must reach the proposal’s span tree at the shared window')
  } finally {
    await view.close()
  }
})

test('the node’s research disclosure is the SAME surface, given the full width', async () => {
  const paths = installFetch()
  const view = await mount(async vite => {
    const { Trace } = await vite.ssrLoadModule('/src/Inspector.jsx')
    return React.createElement(Trace, {
      n: { id: 4, attempt: 0, status: 'done', trace: { nodes: [], projection: {} },
        idea: { card_id: 'card-many' } },
      runId: 'run-1', expectedGeneration: null, expectedTraceRevision: null, live: null,
      working: false, onReload: () => {}, detailStatus: 'ready', reloadPending: false,
      clearScope: 'run-1:4:0:trace-clear', clearRecoveryStore: { current: new Map() },
      recoverClearState: null, clearRecoverySignal: null, publishClearRecovery: () => {},
    })
  })
  try {
    await view.click(view.buttonWith('research'))
    await view.waitFor(() => /Researcher · propose/.test(view.container.textContent),
      'the proposal’s conversation to reach the screen')
    assert.ok(paths.some(path => path.includes('/cards/card-many/trace')),
      'the disclosure reads the card’s research link, never a guess from timing')
    assert.ok(paths.some(path => path.includes(`/by_trace/${PROPOSE_TRACE_ID}/conversation`)),
      'and renders it on the same surface as everything else — conversation first')
    await view.click(view.buttonIn('.research-trace', 'expand all'))
    assert.match(view.container.textContent, /the researcher argued/)

    // (4) LAYOUT. The trace is a BLOCK sibling of its heading, never a flex item beside the label —
    // that is what squeezed a span tree with its own search bar into a narrow column.
    const row = view.container.querySelector('.research-trace')
    assert.ok(row != null, 'the research row must render')
    assert.ok(row.querySelector('.research-trace-h .trace') == null,
      'the trace must not sit inside the heading row')
    assert.ok([...row.children].some(child => child.classList.contains('trace')),
      'the trace must be a direct child of the row, under the heading')

    // (3) STYLE. `.seg` paints a button only through `.seg button` (it is a CONTAINER rule) or the
    // scoped `.conv-toggle .seg`. A `<button className="seg">` anywhere else gets a border and no
    // background, so the browser's own buttonface shows through — the white slab. The surface's own
    // switcher lives inside `.conv-toggle` and is fine; the research chrome must not be bare.
    const bare = [...view.container.querySelectorAll('.trace-research button, .research-trace button')]
      .filter(node => node.className.split(/\s+/).includes('seg'))
      .filter(node => node.closest('.conv-toggle') == null && node.closest('.seg') !== node.parentElement)
    assert.equal(bare.length, 0,
      'a bare .seg button gets a border and no background — the UA buttonface is the white slab')
  } finally {
    await view.close()
  }
})

test('the node research disclosure recovers in place after its first card-trace read fails', async () => {
  let cardReads = 0
  const paths = []
  const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status,
    headers: { get: () => null }, json: async () => body })
  globalThis.fetch = async url => {
    const path = String(url)
    paths.push(path)
    if (path.includes('/cards/card-many/trace')) {
      cardReads += 1
      return cardReads === 1
        ? response({ detail: 'temporarily unavailable' }, 503)
        : response(CARD_TRACE)
    }
    if (/\/nodes\/4\/conversation/.test(path)) {
      return response(conversationBody(
        { node_id: '4', attempt: 0 }, 'implement', 'the developer wrote code'))
    }
    if (/\/by_trace\/[^/]+\/conversation/.test(path)) {
      return response(conversationBody(
        { trace_id: PROPOSE_TRACE_ID }, 'propose', 'the researcher argued'))
    }
    return response({})
  }
  const view = await mount(async vite => {
    const { Trace } = await vite.ssrLoadModule('/src/Inspector.jsx')
    return React.createElement(Trace, {
      n: { id: 4, attempt: 0, status: 'done', trace: { nodes: [], projection: {} },
        idea: { card_id: 'card-many' } },
      runId: 'run-1', expectedGeneration: null, expectedTraceRevision: null, live: null,
      working: false, onReload: () => {}, detailStatus: 'ready', reloadPending: false,
      clearScope: 'run-1:4:0:trace-clear', clearRecoveryStore: { current: new Map() },
      recoverClearState: null, clearRecoverySignal: null, publishClearRecovery: () => {},
    })
  })
  try {
    await view.click(view.buttonWith('research'))
    await view.waitFor(
      () => view.buttonIn('.trace-research-body', 'retry research trace') != null,
      'the research retry control')
    assert.equal(cardReads, 1)
    assert.match(view.container.querySelector('.trace-research-body')?.textContent || '',
      /Trace unavailable for this work item/i)

    await view.click(view.buttonIn('.trace-research-body', 'retry research trace'))
    await view.waitFor(() => /Researcher · propose/.test(view.container.textContent),
      'the retried research trace')
    assert.equal(cardReads, 2)
    assert.ok(paths.some(path => path.includes(`/by_trace/${PROPOSE_TRACE_ID}/conversation`)),
      'the successful retry must continue into the shared readable trace surface')
    assert.equal(view.buttonIn('.trace-research-body', 'retry research trace'), undefined)
  } finally {
    await view.close()
  }
})

test('the design system really does leave a bare .seg button unpainted', () => {
  // The mechanism behind (3), stated once so the assertion above is not folklore: `.seg` is a
  // CONTAINER rule — the background lives on `.seg button` and on the scoped `.conv-toggle .seg` —
  // so a `<button className="seg">` outside those contexts inherits nothing and the browser paints
  // its own button face. Every control on the research surface is a `.btn`, which is painted.
  const css = src('styles.css')
  const segRule = css.match(/\n\.seg \{([^}]*)\}/)
  assert.ok(segRule, '.seg must still be the container rule this reasoning is about')
  assert.doesNotMatch(segRule[1], /background/,
    'if .seg ever paints itself, revisit the research surface’s controls')
  assert.match(css, /\n\.btn \{[^}]*background:/, '.btn must stay fully specified')
  const inspector = src('Inspector.jsx')
  const surface = inspector.slice(inspector.indexOf('export function ResearchTraces'),
    inspector.indexOf('export function Trace('))
  assert.doesNotMatch(surface, /<button[^>]*className="seg"/,
    'the research surface must not go back to unpainted controls')
})

test('the reusable surface is ONE component, reached by every trace reader', () => {
  // (2) The through-line. Not a text pin for its own sake: what this catches is a second surface
  // being grown beside the first, which is exactly how the card ended up with a poorer one.
  const card = src('CardBoard.jsx')
  assert.match(card, /import\('\.\/Inspector\.jsx'\)[\s\S]*?module\.TraceSurface/,
    'the card must render the Inspector’s trace surface, not its own')
  assert.match(card, /import\('\.\/Inspector\.jsx'\)[\s\S]*?module\.ResearchTraces/)
  assert.doesNotMatch(card, /LazyOpTrace|module\.OpTrace/,
    'the card’s second trace element must not come back')
  const inspector = src('Inspector.jsx')
  assert.equal(inspector.match(/<Conversation /g)?.length, 1,
    'exactly one call site renders the conversation — the shared surface')
  assert.equal(inspector.match(/<VirtualSpanTree /g)?.length, 2,
    'the span tree renders from the shared surface and from NodeTrace (the Dock’s renderer)')
})
