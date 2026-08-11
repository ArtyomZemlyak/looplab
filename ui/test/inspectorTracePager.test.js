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
    attempt: 0,
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
      assert.match(conversationCalls()[0], /\/nodes\/7\/conversation\?attempt=0&limit=512$/)
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
      assert.match(conversationCalls()[1], /\/conversation\?attempt=0&limit=1024$/)
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
      assert.match(conversationCalls().at(-1), /\/conversation\?attempt=0&limit=2048$/)
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
      assert.match(conversationCalls().at(-1),
        new RegExp(`/conversation\\?attempt=0&limit=${expected}$`))
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

test('lazy span detail sends and validates the run generation before commit', async () => {
  const dom = installDom()
  const generation = 'a'.repeat(64)
  const foreignGeneration = 'b'.repeat(64)
  const requests = []
  const outcomes = [
    { run_generation: foreignGeneration, span_id: 'generation-one', kind: 'generation',
      attributes: { output: 'foreign detail must never render' }, projection: {} },
    { run_generation: generation, span_id: 'generation-one', kind: 'generation',
      attributes: { output: 'current detail rendered' }, projection: {} },
  ]
  globalThis.fetch = async url => {
    requests.push(String(url))
    const body = outcomes.shift()
    return { ok: true, status: 200, json: async () => body }
  }

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { NodeTrace } = await vite.ssrLoadModule('/src/Inspector.jsx')
    const { createRoot } = await import('react-dom/client')
    const { act } = await import('react-dom/test-utils')
    const container = dom.window.document.getElementById('root')
    const root = createRoot(container)
    const spans = [{
      span_id: 'generation-one', trace_id: 'trace-one', name: 'chat', kind: 'generation',
      start: 0, duration_s: 1, status: 'OK', attributes: { model: 'm' }, children: [],
    }]
    const settle = async () => {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    }
    await act(async () => {
      root.render(React.createElement(NodeTrace, {
        spans, runId: 'demo', expectedGeneration: generation, projection: {},
      }))
    })
    await settle()

    await act(async () => {
      container.querySelector('.span-row.gen').dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
    })
    await settle()
    assert.match(requests[0], new RegExp(`expected_generation=${generation}`))
    assert.doesNotMatch(container.textContent, /foreign detail must never render/)
    assert.match(container.textContent, /Trace detail unavailable/i)

    const retry = [...container.querySelectorAll('button')]
      .find(node => node.textContent.trim() === 'Retry trace')
    await act(async () => {
      retry.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    })
    await settle()
    assert.match(container.textContent, /current detail rendered/)
    assert.doesNotMatch(container.textContent, /Trace detail unavailable/i)

    await act(async () => { root.unmount() })
  } finally {
    await vite.close()
    dom.window.close()
  }
})

test('the earlier steps stay reachable without a mouse, and a failed widen keeps what is on screen',
  async () => {
    const dom = installDom()
    const observer = installObserver()
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
      assert.match(conversationCalls()[1], /\/conversation\?attempt=0&limit=1024$/)

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
      // …and it must be RETRYABLE. `settleTraceRead` deliberately does not record a window it could
      // not reach, so the settled window stays behind the requested one forever; a `pending` derived
      // from that comparison alone latches "Loading earlier steps…" permanently, and a surface stuck
      // in `loading` never re-arms — the operator is told to scroll again and scrolling does nothing.
      assert.ok(container.querySelector('.trace-reach-zone') != null,
        'the sentinel must survive a failed widen')
      assert.doesNotMatch(container.textContent, /Loading earlier steps/,
        'a failed widen must not leave a spinner running forever')
      failNext = false
      // Scroll + intersection, NOT another `.focus()`: the affordance already has focus from the
      // call above, and re-focusing an already-focused element fires no event at all.
      await act(async () => {
        dom.window.dispatchEvent(new dom.window.Event('scroll'))
        observer.reach()
      })
      await settle()
      assert.equal(conversationCalls().length, 3, 'scrolling again must retry')
      assert.equal(container.querySelectorAll('.trace-reach-failed').length, 0,
        'a read that succeeds clears the failure it replaces')
      assert.ok(container.querySelectorAll('.stage-dynamic').length > 64,
        'the retry must actually widen what is on screen')

      await act(async () => { root.unmount() })
    } finally {
      await vite.close()
      dom.window.close()
    }
  })

test('conversation fallback is lifecycle-scoped while a same-lifecycle widen keeps last-good evidence',
  async () => {
    const dom = installDom()
    let fail = false
    const response = body => ({ ok: true, status: 200, headers: { get: () => null },
      json: async () => body })
    globalThis.fetch = async url => {
      const path = String(url)
      if (path.includes('/logs')) return response({})
      if (fail) throw new Error('offline')
      return response(conversationPage(512, 1))
    }

    const vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    try {
      const { Conversation } = await vite.ssrLoadModule('/src/Inspector.jsx')
      const { createRoot } = await import('react-dom/client')
      const { act } = await import('react-dom/test-utils')
      const container = dom.window.document.getElementById('root')
      const root = createRoot(container)
      const props = { n: { id: 7, attempt: 0 }, runId: 'demo', working: false,
        spanLimit: 512, reloadNonce: 0 }
      const settle = async () => {
        await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      }

      await act(async () => { root.render(React.createElement(Conversation, props)) })
      await settle()
      assert.match(container.textContent, /turn 0/)

      fail = true
      await act(async () => { root.render(React.createElement(
        Conversation, { ...props, spanLimit: 1024 })) })
      await settle()
      assert.match(container.textContent, /turn 0/,
        'a failed wider representation may retain evidence from the same lifecycle')

      await act(async () => { root.render(React.createElement(
        Conversation, { ...props, spanLimit: 1024, reloadNonce: 1 })) })
      assert.doesNotMatch(container.textContent, /turn 0/,
        'a post-clear epoch must hide prior evidence during the render that changes scope')
      await settle()
      assert.doesNotMatch(container.textContent, /turn 0/)
      assert.ok(container.querySelector('.resource-error') != null,
        'a failed first observation in the new lifecycle is unavailable')

      await act(async () => { root.render(React.createElement(Conversation, {
        ...props, n: { id: 8, attempt: 1 }, spanLimit: 1024, reloadNonce: 1,
      })) })
      assert.doesNotMatch(container.textContent, /turn 0/,
        'node/attempt replacement may not borrow another lifecycle while its read fails')
      await settle()
      assert.doesNotMatch(container.textContent, /turn 0/)
      await act(async () => { root.unmount() })
    } finally {
      await vite.close()
      dom.window.close()
    }
  })

test('a failed live refresh marks a complete last-good conversation stale until recovery', async () => {
  const dom = installDom()
  installObserver()
  const callbacks = new Map()
  let nextTimer = 1
  const previousSetInterval = globalThis.setInterval
  const previousClearInterval = globalThis.clearInterval
  Object.defineProperty(globalThis, 'setInterval', {
    configurable: true, writable: true,
    value: callback => { const id = nextTimer++; callbacks.set(id, callback); return id },
  })
  Object.defineProperty(globalThis, 'clearInterval', {
    configurable: true, writable: true, value: id => callbacks.delete(id),
  })
  let conversationReads = 0
  globalThis.fetch = async url => {
    const path = String(url)
    if (!path.includes('/conversation')) {
      return { ok: true, status: 200, json: async () => ({}) }
    }
    conversationReads += 1
    if (conversationReads === 2) {
      return {
        ok: false, status: 409, headers: { get: () => null },
        json: async () => ({ detail: { code: 'run_reset_in_progress',
          message: 'run is being replayed' } }),
      }
    }
    return { ok: true, status: 200, json: async () => conversationPage(512, 1) }
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

    await act(async () => {
      root.render(React.createElement(Trace, traceProps({ working: true })))
    })
    await settle()
    assert.equal(container.querySelectorAll('.stage-dynamic').length, 1)
    assert.equal(container.querySelectorAll('.conversation-stale').length, 0)

    await act(async () => { for (const callback of [...callbacks.values()]) callback() })
    await settle()
    assert.equal(container.querySelectorAll('.stage-dynamic').length, 1,
      'a failed refresh retains the last confirmed evidence')
    assert.match(container.querySelector('.conversation-stale')?.textContent || '',
      /showing confirmed trace/i)
    assert.equal(container.querySelectorAll('.trace-reach-failed').length, 0,
      'a refresh failure must not masquerade as a failed request for earlier steps')

    await act(async () => { for (const callback of [...callbacks.values()]) callback() })
    await settle()
    assert.equal(container.querySelectorAll('.conversation-stale').length, 0,
      'a correctly scoped success clears the stale warning')

    await act(async () => { root.unmount() })
  } finally {
    await vite.close()
    Object.defineProperty(globalThis, 'setInterval', {
      configurable: true, writable: true, value: previousSetInterval,
    })
    Object.defineProperty(globalThis, 'clearInterval', {
      configurable: true, writable: true, value: previousClearInterval,
    })
    dom.window.close()
  }
})

test('live conversation reuses only an exact ETag and aborts an unsettled conditional poll', async () => {
  const dom = installDom()
  installObserver()
  const callbacks = new Map()
  let nextTimer = 1
  const previousSetInterval = globalThis.setInterval
  const previousClearInterval = globalThis.clearInterval
  Object.defineProperty(globalThis, 'setInterval', {
    configurable: true, writable: true,
    value: callback => { const id = nextTimer++; callbacks.set(id, callback); return id },
  })
  Object.defineProperty(globalThis, 'clearInterval', {
    configurable: true, writable: true, value: id => callbacks.delete(id),
  })
  const firstTag = 'W/"llconv1-' + 'a'.repeat(64) + '"'
  const nextTag = 'W/"llconv1-' + 'b'.repeat(64) + '"'
  const conversationCalls = []
  let conversationReads = 0
  let abortedSignal = null
  const response = (status, etag, body, json = async () => body) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: name => name.toLowerCase() === 'etag' ? etag : null },
    json,
  })
  globalThis.fetch = async (url, options = {}) => {
    const path = String(url)
    if (!path.includes('/conversation')) return response(200, null, {})
    conversationCalls.push({ path, options })
    conversationReads += 1
    if (conversationReads === 1) {
      return response(200, firstTag, { ...conversationPage(512, 1), cursor: firstTag })
    }
    if (conversationReads === 2) {
      return response(304, firstTag, null,
        async () => { throw new Error('the bodyless hit must not be parsed') })
    }
    if (conversationReads === 3) {
      return response(304, nextTag, null,
        async () => { throw new Error('the mismatched hit must not be parsed') })
    }
    if (conversationReads === 4) {
      const updated = conversationPage(512, 1)
      updated.stages[0].turns[0].output = 'updated after unconditional retry'
      return response(200, nextTag, { ...updated, cursor: nextTag })
    }
    return new Promise((resolve, reject) => {
      abortedSignal = options.signal
      const abort = () => reject(new DOMException('aborted', 'AbortError'))
      if (options.signal?.aborted) abort()
      else options.signal?.addEventListener('abort', abort, { once: true })
    })
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
    const tick = async () => {
      await act(async () => { for (const callback of [...callbacks.values()]) callback() })
      await settle()
    }

    await act(async () => {
      root.render(React.createElement(Trace, traceProps({ working: true })))
    })
    await settle()
    await act(async () => {
      container.querySelector('.trace-collapse')?.dispatchEvent(
        new dom.window.MouseEvent('click', { bubbles: true }))
    })
    assert.match(container.textContent, /turn 0/)

    await tick()
    assert.equal(conversationReads, 2)
    assert.equal(new Headers(conversationCalls[1].options.headers).get('If-None-Match'), firstTag)
    assert.match(container.textContent, /turn 0/,
      'a matching 304 must retain the exact same-scope last-good payload')
    assert.equal(container.querySelectorAll('.conversation-stale').length, 0)

    await tick()
    assert.equal(conversationReads, 4,
      'a mismatched 304 must trigger one unconditional recovery read in the same tick')
    assert.equal(new Headers(conversationCalls[2].options.headers).get('If-None-Match'), firstTag)
    assert.equal(new Headers(conversationCalls[3].options.headers).get('If-None-Match'), null)
    assert.match(container.textContent, /updated after unconditional retry/)

    // Start one more serialized live tick and unmount before it settles. usePoll owns the deadline
    // handle, so cleanup must abort the very signal conditionalGet passed through to fetch.
    await act(async () => {
      for (const callback of [...callbacks.values()]) callback()
      await Promise.resolve()
    })
    assert.ok(abortedSignal != null && !abortedSignal.aborted)
    await act(async () => { root.unmount() })
    assert.equal(abortedSignal.aborted, true)
  } finally {
    await vite.close()
    Object.defineProperty(globalThis, 'setInterval', {
      configurable: true, writable: true, value: previousSetInterval,
    })
    Object.defineProperty(globalThis, 'clearInterval', {
      configurable: true, writable: true, value: previousClearInterval,
    })
    dom.window.close()
  }
})

test('a conversation response for another attempt is rejected before it reaches the trace UI',
  async () => {
    const dom = installDom()
    installObserver()
    const requests = []
    globalThis.fetch = async (url) => {
      const path = String(url)
      requests.push(path)
      const limit = Number(new URL(path, 'http://localhost').searchParams.get('limit') || 0)
      const body = path.includes('/conversation')
        ? { ...conversationPage(limit), attempt: 1 }
        : {}
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

      await act(async () => { root.render(React.createElement(Trace, traceProps())) })
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

      const conversationCalls = requests.filter(path => path.includes('/conversation'))
      assert.equal(conversationCalls.length, 1)
      assert.match(conversationCalls[0], /\/conversation\?attempt=0&limit=512$/,
        'the request must carry the lifecycle rendered by the Inspector')
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 0,
        'another attempt\'s fulfilled payload must not settle as this conversation')
      assert.ok(container.querySelector('.resource-error') != null,
        'a first observation with the wrong identity is unavailable, not a successful empty trace')
      assert.doesNotMatch(container.textContent, /turn 0/)

      await act(async () => { root.unmount() })
    } finally {
      await vite.close()
      dom.window.close()
    }
  })

test('a conversation response for another run generation is rejected before commit', async () => {
  const dom = installDom()
  installObserver()
  const expectedGeneration = 'a'.repeat(64)
  const requests = []
  globalThis.fetch = async (url) => {
    const path = String(url)
    requests.push(path)
    const limit = Number(new URL(path, 'http://localhost').searchParams.get('limit') || 0)
    const body = path.includes('/conversation')
      ? { ...conversationPage(limit), run_generation: 'b'.repeat(64) }
      : {}
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

    await act(async () => {
      root.render(React.createElement(Trace, traceProps({ expectedGeneration })))
    })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

    const call = requests.find(path => path.includes('/conversation'))
    assert.match(call, new RegExp(
      `/conversation\\?attempt=0&limit=512&expected_generation=${expectedGeneration}$`))
    assert.equal(container.querySelectorAll('.stage-dynamic').length, 0)
    assert.ok(container.querySelector('.resource-error') != null,
      'a foreign-generation 200 is unavailable, not a successful empty conversation')
    assert.doesNotMatch(container.textContent, /turn 0/)

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
  let traceReads = 0
  globalThis.fetch = async (url) => {
    const path = String(url)
    requests.push(path)
    if (/\/nodes\/7\/trace\?/.test(path) && ++traceReads === 2) {
      return { ok: false, status: 503, json: async () => ({ detail: 'offline' }) }
    }
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
    const props = traceProps({
      n: { id: 7, attempt: 0, status: 'done', trace: detailTrace },
    })
    const settle = async () => {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    }
    await act(async () => {
      root.render(React.createElement(Trace, props))
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

    // A live refresh of the SAME 1024 window fails. The proven wider rows stay on screen and the
    // operator is told they are stale; falling back to detailTrace would leave only one row.
    await act(async () => {
      root.render(React.createElement(Trace, { ...props, working: true }))
    })
    await settle()
    assert.equal(traceCalls().length, 2)
    assert.equal(container.querySelectorAll('.span-row').length, 2)
    assert.match(container.querySelector('[role="alert"]')?.textContent || '',
      /Span-tree refresh failed; showing confirmed spans/i)

    await act(async () => { root.unmount() })
  } finally {
    await vite.close()
    dom.window.close()
  }
})
