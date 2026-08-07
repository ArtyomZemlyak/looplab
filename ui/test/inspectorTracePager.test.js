// The operator's report, driven rather than pinned: the Inspector's Trace tab said "N steps hidden",
// offered one click at a time, and then refused ("the window is at its maximum"). Earlier steps now
// arrive by SCROLLING and there is no pager button at all. A source pin cannot tell a rendered
// sentinel from a working one, so this mounts the real component, drives a real IntersectionObserver
// callback, and reads the real requests it issues.
//
// NOTE the assertion style: never `assert.equal(<DOM element>, null)`. Building that diff serializes
// a JSDOM element and walks the whole window graph — the process grows to ~13 GB and is OOM-killed,
// which surfaces as a bare SIGKILL with no message.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// A node whose conversation is bounded by the STAGE cap, not the span read — the shape measured on
// runs/rubert-dr-0804 node 1, where every withheld band was already derivable from the spans in hand.
const conversationPage = (limit, totalStages = 200) => {
  const visibleStages = Math.min(totalStages, 64 * Math.max(1, Math.floor(limit / 512)))
  return {
    schema: 2,
    run_id: 'demo',
    node_id: '7',
    stages: Array.from({ length: visibleStages }, (_, index) => ({
      trace_id: `trace-${index}`,
      label: 'inline_repair',
      start: index,
      rollup: { generations: 1, tools: 0, tokens: {} },
      turns: [{ type: 'generation', output: `turn ${index}`, usage: {} }],
    })),
    projection: {
      schema: 2,
      truncated: visibleStages < totalStages,
      total_spans: 400,
      visible_spans: 400,
      omitted_spans: 0,
      total_stages: totalStages,
      visible_stages: visibleStages,
      omitted_stages: totalStages - visibleStages,
      total_turns: totalStages,
      visible_turns: visibleStages,
      omitted_turns: totalStages - visibleStages,
    },
  }
}

// The observer stub is the whole point of this harness: jsdom has no IntersectionObserver, and a
// real one would need real geometry. Driving the callback directly is also CLOSER to the property —
// what matters is what happens when the sentinel comes into view, not how the browser decided it did.
const installObserver = () => {
  const live = []
  class FakeIntersectionObserver {
    constructor(callback) { this.callback = callback; this.node = null; live.push(this) }
    observe(node) { this.node = node }
    unobserve() { this.node = null }
    disconnect() {
      this.node = null
      const at = live.indexOf(this)
      if (at >= 0) live.splice(at, 1)
    }
  }
  Object.defineProperty(globalThis, 'IntersectionObserver',
    { configurable: true, writable: true, value: FakeIntersectionObserver })
  return {
    // Scroll the sentinel into view.
    reach: () => {
      for (const observer of [...live]) {
        if (observer.node) observer.callback([{ isIntersecting: true, target: observer.node }], observer)
      }
    },
    attached: () => live.filter(observer => observer.node != null).length,
  }
}

const installDom = () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
    { url: 'http://localhost/', pretendToBeVisual: true })
  for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
    'MouseEvent', 'CustomEvent', 'FocusEvent', 'getComputedStyle', 'requestAnimationFrame',
    'cancelAnimationFrame']) {
    // `defineProperty`, not assignment — the house pattern (conceptLensServerRecovery.test.js:186).
    // Node ships `globalThis.navigator` as an accessor with no setter, so `globalThis.navigator = …`
    // throws `Cannot set property navigator of #<Object> which has only a getter` and takes down the
    // whole file before a single assertion runs.
    Object.defineProperty(globalThis, name,
      { configurable: true, writable: true, value: dom.window[name] })
  }
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT',
    { configurable: true, writable: true, value: true })
  return dom
}

const traceProps = overrides => ({
  n: { id: 7, attempt: 0, status: 'done', trace: { nodes: [], projection: {} } },
  runId: 'demo',
  expectedGeneration: null,
  expectedTraceRevision: null,
  live: null,
  working: false,
  onReload: () => {},
  detailStatus: 'ready',
  reloadPending: false,
  clearScope: 'demo:7:0:trace-clear',
  clearRecoveryStore: { current: new Map() },
  recoverClearState: null,
  clearRecoverySignal: null,
  publishClearRecovery: () => {},
  ...overrides,
})

test('scrolling the conversation sentinel into view fetches a bigger window and renders it',
  async () => {
    const dom = installDom()
    const observer = installObserver()
    const requests = []
    globalThis.fetch = async (url) => {
      const path = String(url)
      requests.push(path)
      const limit = Number(new URL(path, 'http://localhost').searchParams.get('limit') || 0)
      const body = path.includes('/conversation') ? conversationPage(limit) : {}
      return { ok: true, status: 200, json: async () => body }
    }

    const vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    try {
      const { Trace } = await vite.ssrLoadModule('/src/Inspector.jsx')
      const { createRoot } = await import('react-dom/client')
      const { act } = await import('react-dom/test-utils')

      const container = dom.window.document.getElementById('root')
      const root = createRoot(container)
      const settle = async () => {
        await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      }
      // The gesture budget is one automatic widen per operator scroll, so every reach after the
      // first has to be preceded by a real scroll — exactly as it would be in the browser.
      const scrollThenReach = async () => {
        await act(async () => {
          dom.window.dispatchEvent(new dom.window.Event('scroll'))
          observer.reach()
        })
        await settle()
      }

      await act(async () => { root.render(React.createElement(Trace, traceProps())) })
      await settle()

      // FIRST read: the shared default window, sent explicitly.
      const conversationCalls = () => requests.filter(path => path.includes('/conversation'))
      assert.equal(conversationCalls().length, 1)
      assert.match(conversationCalls()[0], /\/nodes\/7\/conversation\?limit=512$/)
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 64,
        'the default window renders the capped bands')

      // The button is GONE, and so is the announcement of a limit. What is here instead is a
      // sentinel with an observer attached to it.
      assert.equal(container.querySelectorAll('button.trace-loadmore').length, 0,
        'the pager button must not come back')
      assert.equal(container.querySelectorAll('.notice.compact').length, 0,
        'a reachable window must not announce a limit it is about to lift')
      assert.ok(container.querySelector('.trace-reach-zone') != null, 'a sentinel must be rendered')
      assert.equal(observer.attached(), 1, 'the sentinel must actually be observed')

      // THE WHOLE PROPERTY: bring it into view. The window doubles, a NEW request goes out carrying
      // it, and the wider response reaches the screen — with nothing clicked.
      await act(async () => { observer.reach() })
      await settle()
      assert.equal(conversationCalls().length, 2, 'reaching the sentinel must issue a real request')
      assert.match(conversationCalls()[1], /\/conversation\?limit=1024$/)
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 128,
        'the wider response must actually reach the screen')

      // One widen per gesture: reaching again WITHOUT scrolling must not chain. Without this a
      // thread shorter than the viewport walks the whole ladder the moment the node is opened.
      await act(async () => { observer.reach() })
      await settle()
      assert.equal(conversationCalls().length, 2, 'the budget must not refill by itself')

      // Scroll, reach: the next rung. This fixture is 200 stages, so the 2048 window returns all of
      // them and the sentinel goes away rather than lingering as an affordance that cannot help.
      await scrollThenReach()
      assert.match(conversationCalls().at(-1), /\/conversation\?limit=2048$/)
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 200)
      assert.equal(container.querySelectorAll('.trace-reach-zone').length, 0,
        'a complete conversation must stop offering to load more')
      assert.equal(container.querySelectorAll('.notice.compact').length, 0)

      await act(async () => { root.unmount() })
    } finally {
      await vite.close()
      dom.window.close()
    }
  })

test('at the ceiling the operator gets the COUNT, not another sentinel', async () => {
  const dom = installDom()
  const observer = installObserver()
  const requests = []
  globalThis.fetch = async (url) => {
    const path = String(url)
    requests.push(path)
    const limit = Number(new URL(path, 'http://localhost').searchParams.get('limit') || 0)
    // 5000 stages: no window this surface can ask for ever completes it, which is the one shape
    // where a bound really does bind — measured, exactly one node in the 43-run corpus.
    const body = path.includes('/conversation') ? conversationPage(limit, 5000) : {}
    return { ok: true, status: 200, json: async () => body }
  }

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { Trace } = await vite.ssrLoadModule('/src/Inspector.jsx')
    const { createRoot } = await import('react-dom/client')
    const { act } = await import('react-dom/test-utils')

    const container = dom.window.document.getElementById('root')
    const root = createRoot(container)
    const settle = async () => {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    }
    await act(async () => { root.render(React.createElement(Trace, traceProps())) })
    await settle()

    const conversationCalls = () => requests.filter(path => path.includes('/conversation'))
    for (const expected of [1024, 2048, 4096]) {
      await act(async () => {
        dom.window.dispatchEvent(new dom.window.Event('scroll'))
        observer.reach()
      })
      await settle()
      assert.match(conversationCalls().at(-1), new RegExp(`/conversation\\?limit=${expected}$`))
    }

    // At the ceiling the sentinel is withdrawn — an affordance that cannot do anything is exactly
    // the dead control this change removed — and the notice states the remainder in STEPS.
    assert.equal(container.querySelectorAll('.trace-reach-zone').length, 0,
      'a sentinel at the ceiling would invite a scroll that can never help')
    const notice = container.querySelector('.notice.compact')
    assert.ok(notice != null, 'a bound that really binds owes the operator the count')
    assert.match(notice.textContent, /Showing the most recent 512 of 5000 steps\./)
    assert.match(notice.textContent, /No more of it can be loaded here\./)
    assert.doesNotMatch(notice.textContent, /span/i, 'never a count of a quantity they cannot see')

    // Further scrolling must not keep asking: the window cannot grow, and each of these reads costs
    // the server seconds.
    const before = conversationCalls().length
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.Event('scroll'))
      observer.reach()
    })
    await settle()
    assert.equal(conversationCalls().length, before)

    await act(async () => { root.unmount() })
  } finally {
    await vite.close()
    dom.window.close()
  }
})

test('the earlier steps stay reachable without a mouse, and a failed widen keeps what is on screen',
  async () => {
    const dom = installDom()
    installObserver()
    const requests = []
    let failNext = false
    globalThis.fetch = async (url) => {
      const path = String(url)
      requests.push(path)
      if (path.includes('/conversation') && failNext) throw new Error('network')
      const limit = Number(new URL(path, 'http://localhost').searchParams.get('limit') || 0)
      const body = path.includes('/conversation') ? conversationPage(limit) : {}
      return { ok: true, status: 200, json: async () => body }
    }

    const vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    try {
      const { Trace } = await vite.ssrLoadModule('/src/Inspector.jsx')
      const { createRoot } = await import('react-dom/client')
      const { act } = await import('react-dom/test-utils')

      const container = dom.window.document.getElementById('root')
      const root = createRoot(container)
      const settle = async () => {
        await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      }
      await act(async () => { root.render(React.createElement(Trace, traceProps())) })
      await settle()
      const conversationCalls = () => requests.filter(path => path.includes('/conversation'))
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 64)

      // KEYBOARD / SCREEN READER. Infinite scroll's standard failure is that a virtual-cursor user
      // never scrolls the container and so has no path to the earlier steps at all. The affordance
      // is sr-only until focused, and FOCUSING it does what scrolling to it does.
      const reachButton = container.querySelector('button.trace-reach')
      assert.ok(reachButton != null, 'a focusable path to the earlier steps must exist')
      assert.match(reachButton.textContent, /Load earlier steps/)

      failNext = true
      // `.focus()`, not a synthetic FocusEvent: React delegates `onFocus` through `focusin`, so a
      // hand-dispatched non-bubbling `focus` never reaches it. This is also the real gesture — what
      // a keyboard user does is Tab, and Tab is what `.focus()` models.
      await act(async () => { reachButton.focus() })
      await settle()
      assert.equal(conversationCalls().length, 2, 'focus must issue the same widen a scroll does')
      assert.match(conversationCalls()[1], /\/conversation\?limit=1024$/)

      // …and that widen FAILED. Asking for more must never cost the operator what they already had:
      // the 64 bands stay, the surface does not become "unavailable", and the failure is reported
      // as its own alert.
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 64,
        'a failed widen must not blank the conversation the operator was reading')
      assert.equal(container.querySelectorAll('.resource-error').length, 0,
        'a failed widen is not a failed observation — the trace is not unavailable')
      const failure = container.querySelector('.trace-reach-failed')
      assert.ok(failure != null, 'the failed widen must be reported, not swallowed')
      assert.match(failure.textContent, /Could not load earlier steps/)

      await act(async () => { root.unmount() })
    } finally {
      await vite.close()
      dom.window.close()
    }
  })

test('the span-tree view pages through /trace, and only once the operator asks', async () => {
  const dom = installDom()
  const observer = installObserver()
  const requests = []
  globalThis.fetch = async (url) => {
    const path = String(url)
    requests.push(path)
    const limit = Number(new URL(path, 'http://localhost').searchParams.get('limit') || 512)
    const visible = Math.min(2000, limit)
    const body = path.includes('/trace')
      ? {
        node_id: 7,
        attempt: 0,
        nodes: Array.from({ length: 2 }, (_, index) => ({
          span_id: `s${limit}-${index}`, name: 'implement', kind: 'operation',
          start: index, duration_s: 1, children: [],
        })),
        rollup: { generations: visible, tools: 0, tokens: {} },
        projection: {
          schema: 2, truncated: visible < 2000, total_spans: 2000,
          visible_spans: visible, omitted_spans: 2000 - visible,
        },
      }
      : { stages: [], projection: { schema: 2, truncated: false } }
    return { ok: true, status: 200, json: async () => body }
  }

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { Trace } = await vite.ssrLoadModule('/src/Inspector.jsx')
    const { createRoot } = await import('react-dom/client')
    const { act } = await import('react-dom/test-utils')

    // The detail payload's default window is what the span tree renders first — no fetch of its own.
    const detailTrace = {
      nodes: [{ span_id: 'detail-root', name: 'implement', kind: 'operation', start: 0,
        duration_s: 1, children: [] }],
      rollup: { generations: 512, tools: 0, tokens: {} },
      projection: { schema: 2, truncated: true, total_spans: 2000, visible_spans: 512,
        omitted_spans: 1488 },
    }
    const container = dom.window.document.getElementById('root')
    const root = createRoot(container)
    const settle = async () => {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    }
    await act(async () => {
      root.render(React.createElement(Trace, traceProps({
        n: { id: 7, attempt: 0, status: 'done', trace: detailTrace },
      })))
    })
    await settle()

    // Switch to the span tree. It must render from the detail payload it already has.
    const spanTreeButton = [...container.querySelectorAll('button')]
      .find(node => node.textContent.trim() === 'span tree')
    await act(async () => {
      spanTreeButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    })
    await settle()

    const traceCalls = () => requests.filter(path => /\/nodes\/7\/trace\?/.test(path))
    assert.equal(traceCalls().length, 0,
      'an unpaged span tree must cost no extra request — the detail payload already carries it')
    assert.equal(container.querySelectorAll('button.trace-loadmore').length, 0)
    assert.ok(container.querySelector('.trace-reach-zone') != null,
      'a bounded span tree must offer the same scroll affordance as the conversation')

    await act(async () => { observer.reach() })
    await settle()

    assert.equal(traceCalls().length, 1, 'reaching the sentinel must hit the O(node) /trace endpoint')
    assert.match(traceCalls()[0], /\/nodes\/7\/trace\?attempt=0&limit=1024$/)
    // The widened projection, not the detail payload, now drives the receipt: 1024 of 2000 visible,
    // so the sentinel is still offered rather than the surface declaring itself finished.
    assert.ok(container.querySelector('.trace-reach-zone') != null)
    assert.equal(container.querySelectorAll('.notice.compact').length, 0)

    await act(async () => { root.unmount() })
  } finally {
    await vite.close()
    dom.window.close()
  }
})
