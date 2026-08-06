// The operator's report, driven rather than pinned: the Inspector's Trace tab said "N steps hidden"
// and offered no way to see them. A source pin cannot tell a rendered button from a working one, so
// this mounts the real component, clicks the real control, and reads the real requests it issues.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

// A node whose conversation is bounded by the STAGE cap, not the span read — the shape measured on
// runs/rubert-dr-0804 node 1, where every withheld band was already derivable from the spans in hand.
const conversationPage = limit => {
  const visibleStages = Math.min(200, 64 * Math.max(1, Math.floor(limit / 512)))
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
      truncated: visibleStages < 200,
      total_spans: 400,
      visible_spans: 400,
      omitted_spans: 0,
      total_stages: 200,
      visible_stages: visibleStages,
      omitted_stages: 200 - visibleStages,
      total_turns: 200,
      visible_turns: visibleStages,
      omitted_turns: 200 - visibleStages,
    },
  }
}

const installDom = () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
    { url: 'http://localhost/', pretendToBeVisual: true })
  for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
    'MouseEvent', 'CustomEvent', 'getComputedStyle', 'requestAnimationFrame',
    'cancelAnimationFrame']) {
    globalThis[name] = dom.window[name]
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true
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

test('the Trace tab conversation pager fetches a bigger window and renders what comes back',
  async () => {
    const dom = installDom()
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
      await act(async () => { root.render(React.createElement(Trace, traceProps())) })
      // Let the poll's immediate read settle.
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

      // FIRST read: the shared default window, sent explicitly.
      const conversationCalls = () => requests.filter(path => path.includes('/conversation'))
      assert.equal(conversationCalls().length, 1)
      assert.match(conversationCalls()[0], /\/nodes\/7\/conversation\?limit=512$/)
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 64,
        'the default window renders the capped bands')

      // The control exists, and it names what is missing in STEPS — never in spans.
      const pager = container.querySelector('button.trace-loadmore')
      assert.ok(pager, 'a truncated conversation must offer a control, not a dead notice')
      assert.match(pager.textContent, /load more of this conversation \(136 earlier stages not shown\)/)
      const notice = container.querySelector('.notice.compact')
      assert.match(notice.textContent, /Showing the most recent 64 of 200 steps\./)
      assert.doesNotMatch(notice.textContent, /span/i)

      // CLICK it. This is the whole property: the window doubles, a NEW request goes out carrying
      // it, and the wider response reaches the screen.
      await act(async () => {
        pager.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      })
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

      assert.equal(conversationCalls().length, 2, 'the click must issue a real request')
      assert.match(conversationCalls()[1], /\/conversation\?limit=1024$/)
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 128,
        'the wider response must actually reach the screen')

      // Keep paging: the window climbs to the ceiling, and once nothing is hidden the control goes
      // away instead of lingering as a button that cannot change anything.
      for (const expected of [2048, 4096]) {
        await act(async () => {
          container.querySelector('button.trace-loadmore')
            .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
        })
        await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
        assert.match(conversationCalls().at(-1), new RegExp(`/conversation\\?limit=${expected}$`))
      }
      assert.equal(container.querySelectorAll('.stage-dynamic').length, 200)
      assert.equal(container.querySelector('button.trace-loadmore'), null,
        'a complete conversation must not keep offering "load more"')
      assert.equal(container.querySelector('.notice.compact'), null)

      await act(async () => { root.unmount() })
    } finally {
      await vite.close()
    }
  })

test('the span-tree view pages through /trace, and only once the operator asks', async () => {
  const dom = installDom()
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
    await act(async () => {
      root.render(React.createElement(Trace, traceProps({
        n: { id: 7, attempt: 0, status: 'done', trace: detailTrace },
      })))
    })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

    // Switch to the span tree. It must render from the detail payload it already has.
    const spanTreeButton = [...container.querySelectorAll('button')]
      .find(node => node.textContent.trim() === 'span tree')
    await act(async () => {
      spanTreeButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

    const traceCalls = () => requests.filter(path => /\/nodes\/7\/trace\?/.test(path))
    assert.equal(traceCalls().length, 0,
      'an unpaged span tree must cost no extra request — the detail payload already carries it')
    const pager = container.querySelector('button.trace-loadmore')
    assert.match(pager.textContent, /load more spans \(1488 not shown\)/)

    await act(async () => {
      pager.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

    assert.equal(traceCalls().length, 1, 'paging must reach the O(node) /trace endpoint')
    assert.match(traceCalls()[0], /\/nodes\/7\/trace\?attempt=0&limit=1024$/)
    assert.match(container.querySelector('.notice.compact, button.trace-loadmore').textContent,
      /load more spans \(976 not shown\)/,
      'the widened projection, not the detail payload, must now drive the receipt')

    await act(async () => { root.unmount() })
  } finally {
    await vite.close()
  }
})
