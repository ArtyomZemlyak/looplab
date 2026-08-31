// THE REOPEN BUTTON, RENDERED — the half every previous guard missed.
//
// `dccad06f` shipped the reopen feature end to end: an event type, five control-validation rows, a
// fold handler, `CONTROL.reopenCard`, and a `reopenable` authority gate. It was unreachable from the
// browser THREE separate times, and the first two were caught and fixed while the third survived,
// because every guard tests the MODEL and the dispatch text and nothing rendered a dropped card and
// looked for the button.
//
// The third: the form required `status === 'dropped'` while sitting inside
// `{onControl && !terminal && <details>}`, and `terminal` is `status === 'dropped' || merged_into`.
// Mutually exclusive — the button could render for NO card. It is now a SIBLING of that disclosure,
// gated on the dropped + reopenable pair, and this file is what makes that checkable: it renders a
// real dropped reopenable card through the real board and asserts the control exists.
//
// The complementary assertion matters as much: a LIVE card must still not offer it, and the other
// controls must still be hidden on a terminal card — edit/priority/resources/drop are about work in
// flight, and reopening is the one action a stopped card still has.
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

let dom = null
let root = null
let vite = null
const previous = {}

const card = (over = {}) => ({
  id: 'card-0', statement: 'Does a stronger teacher help?', status: 'proposed',
  kind: 'experiment', evidence: [], blockers: [], reopenable: false, ...over,
})

// `cardBoardModel.js::cardRows` takes `state.cards` as a RECORD KEYED BY ID — "the mapping key is
// authoritative. Never let a malformed/spoofed body id change joins or receipts" — not an array.
// Handing it a list renders ZERO cards and the board silently falls through to the legacy
// "0 tracked" hypotheses panel, which is exactly what the first cut of this file did: two
// assertions failed and blamed the component.
const asState = cards => ({ cards: Object.fromEntries(cards.map(c => [c.id, c])), run_id: 'r' })


async function board(cards) {
  if (!vite) {
    dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
      url: 'https://looplab.test/', pretendToBeVisual: true,
    })
    const installed = {
      window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
      location: dom.window.location, sessionStorage: dom.window.sessionStorage,
      MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
      requestAnimationFrame: cb => setTimeout(cb, 0), cancelAnimationFrame: h => clearTimeout(h),
      IS_REACT_ACT_ENVIRONMENT: true,
      fetch: () => new Promise(() => {}),
    }
    for (const [name, value] of Object.entries(installed)) {
      previous[name] = Object.getOwnPropertyDescriptor(globalThis, name)
      Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const { createRoot } = await import('react-dom/client')
    root = createRoot(document.getElementById('root'))
  }
  const mod = await vite.ssrLoadModule('/src/CardBoard.jsx')
  await act(async () => root.render(React.createElement(mod.HypothesisBoard, {
    state: asState(cards), runId: 'r', runGeneration: 1,
    onSelect() {}, onClose() {}, onToast() {},
  })))
  return document.getElementById('root').innerHTML
}

after(async () => {
  if (vite) await vite.close()
  for (const [name, descriptor] of Object.entries(previous)) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
})

test('a DROPPED REOPENABLE card renders the reopen control', async () => {
  const html = await board([card({ status: 'dropped', reopenable: true,
                                   dropped_reason: 'operator stopped it' })])
  assert.ok(html.includes('Reopen this Card'),
    'the reopen button must render for a dropped reopenable card — this is the assertion the two '
    + 'earlier unreachability fixes lacked, which is why the third survived them')
  assert.ok(html.includes('Reopen reason for card-0'),
    'its labelled input must render with it, or the form is a button with nowhere to type')
})

test('a dropped card that is NOT reopenable offers no reopen control', async () => {
  const html = await board([card({ status: 'dropped', reopenable: false })])
  assert.ok(!html.includes('Reopen this Card'),
    '`reopenable` is the server-side authority gate; rendering the button without it offers an '
    + 'action the server will refuse')
})

test('a LIVE card offers no reopen control', async () => {
  const html = await board([card({ status: 'proposed', reopenable: true })])
  assert.ok(!html.includes('Reopen this Card'),
    'there is nothing to reopen on a card that was never stopped')
})

test('the other operator controls STAY hidden on a dropped card', async () => {
  const html = await board([card({ status: 'dropped', reopenable: true })])
  assert.ok(html.includes('Reopen this Card'), 'precondition: the reopen control renders')
  assert.ok(!html.includes('Operator controls for card-0'),
    'edit / priority / resources / drop are about work IN FLIGHT and must not return on a terminal '
    + 'card — moving the reopen form out of that disclosure must not drag the disclosure with it')
})
