import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

import { parseRunRouteState, sanitizeRunRouteState } from '../src/runRouteState.js'

// Doc 25 UI-03. Six early-return screens of the run route re-declared the same page shell. They
// were NOT identical, and the differences are reachability statements rather than accidents, so the
// extracted RunScreen keeps them as explicit props. This file pins the resulting MARKUP as literal
// strings — the six screens are the only surface an operator sees when the run itself cannot be
// rendered, and every one of them is the last thing standing between a lost destructive operation
// and a blank page.

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const BODY = '<main class="body-marker">body</main>'
const HEAD = '<div class="topbar run-head"><span class="brand"><span class="dot">◉</span> LoopLab</span>'
const BACK = '<button class="btn sm ghost">← runs</button>'
const PILL = '<span class="pill">read-only review</span>'

const withRunScreen = async (fn) => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
    { url: 'https://looplab.test/', pretendToBeVisual: true })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Element: dom.window.Element, Node: dom.window.Node,
    IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  let root
  try {
    const [{ createRoot }, { default: RunScreen }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/RunScreen.jsx'),
    ])
    root = createRoot(document.querySelector('#root'))
    const body = React.createElement('main', { className: 'body-marker' }, 'body')
    const paint = async (props) => {
      await React.act(async () => {
        root.render(React.createElement(RunScreen, props, body))
      })
      return document.querySelector('#root').innerHTML
    }
    await fn(paint)
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
}

test('the shell renders each screen exactly as it was declared inline', async () => {
  await withRunScreen(async (paint) => {
    const onLeave = () => {}
    const onBack = () => {}

    // Screens 1, 2 and 4 — the three Start-over recovery screens. Owner-only: no `review-mode`
    // class, no read-only pill, and a toast, because a destructive operation reports its outcome.
    assert.equal(await paint({ onBack, onLeave, toast: 'a toast' }),
      `<div class="app">${HEAD}${BACK}</div>${BODY}`
      + '<div class="toast" role="status" aria-live="polite" aria-atomic="true">a toast</div></div>')
    assert.equal(await paint({ onBack, onLeave, toast: null }),
      `<div class="app">${HEAD}${BACK}</div>${BODY}</div>`,
      'no pending toast must not leave an empty live region behind')

    // Screens 3 and 5 — the run-resource state and the generation fence. Both are reachable from a
    // review capability, so both carry the pill AND the class that hides it at ≤600px.
    assert.equal(await paint({ reviewMode: false, reviewPill: true, onBack, onLeave }),
      `<div class="app">${HEAD}${BACK}</div>${BODY}</div>`)
    assert.equal(await paint({ reviewMode: true, reviewPill: true, onBack: null, onLeave }),
      `<div class="app review-mode">${HEAD}${PILL}</div>${BODY}</div>`)

    // Screen 6 — the unresolved historical snapshot, which keeps the live badge in its head.
    assert.equal(await paint({
      reviewPill: true,
      onBack,
      onLeave,
      head: React.createElement(React.Fragment, null,
        React.createElement('span', { className: 'spacer' }),
        React.createElement('span', { className: 'live on' },
          React.createElement('span', { className: 'led' }), 'current run: ', 'live')),
    }), `<div class="app">${HEAD}${BACK}<span class="spacer"></span>`
      + '<span class="live on"><span class="led"></span>current run: live</span></div>'
      + `${BODY}</div>`)

    // The default is the STRICTER shape: a caller that forgets `reviewPill` gets the owner-only head
    // the three Start-over screens declared, not a review affordance it never had.
    assert.equal(await paint({ onBack: null, onLeave }),
      `<div class="app">${HEAD}</div>${BODY}</div>`)
  })
})

test('RunView holds exactly one topbar and reaches the rest through the shell', async () => {
  const runView = await readFile(new URL('../src/RunView.jsx', import.meta.url), 'utf8')
  const count = pattern => (runView.match(pattern) || []).length
  // EXACT counts, not `in`: a commented-out copy of a re-declared shell would push these over and
  // fail, which a positive substring pin would not.
  assert.equal(count(/<div className="topbar run-head">/g), 1,
    'only the workspace itself may declare the run topbar; the six screens share RunScreen')
  assert.equal(count(/className="brand"/g), 1)
  assert.equal(count(/<RunScreen\b/g), 6)
  assert.equal(count(/<\/RunScreen>/g), 6)
  // The per-screen variations, counted where they belong.
  assert.equal(count(/<RunScreen reviewMode=\{reviewMode\} reviewPill\n/g), 2,
    'the run-resource state and the generation fence are the two review-reachable screens')
  assert.equal(count(/reviewPill/g), 3,
    'the history screen shows the pill too, and the three Start-over screens must not')
  assert.equal(count(/toast=\{toast\}/g), 3,
    'only the three Start-over screens ever showed a toast')
  assert.equal(count(/onLeave=\{leaveRetainedPanelRoute\}/g), 6,
    'every screen leaves through the retained-work confirmation, never a bare onBack')
})

test('the two screens that omit the review affordances cannot be reached by a reviewer', () => {
  // Screen 6 (`historyActive && !hist`) carries the read-only pill but NOT the `review-mode` class,
  // and that asymmetry is preserved rather than tidied because a review capability can never put a
  // sequence on the route in the first place. Driven through the real sanitizer and parser.
  assert.equal(sanitizeRunRouteState(
    { generation: 'a'.repeat(64), sequence: 7 }, { reviewMode: false }).sequence, 7)
  assert.equal(sanitizeRunRouteState(
    { generation: 'a'.repeat(64), sequence: 7 }, { reviewMode: true }).sequence, null)
  const link = `#/run/demo?gen=${'a'.repeat(64)}&seq=7`
  assert.equal(parseRunRouteState(link, { reviewMode: false }).state.sequence, 7)
  const review = parseRunRouteState(link, { reviewMode: true })
  assert.equal(review.state.sequence, null)
  assert.ok(review.issues.some(issue => issue.includes('unavailable in review links')))
  // The Start-over screens' half of the same invariant is driven in startOverSaga.test.js
  // ('review mode can never carry a Start-over recovery state').
})
