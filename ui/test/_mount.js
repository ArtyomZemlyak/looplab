// The shared MOUNT harness (doc 50 `largest-ui-components-are-never-mounted`, doc 52 row 26): the
// extraction of the pattern `cardKanban.test.js` proved — a real component loaded through Vite's
// SSR transform and rendered with `renderToStaticMarkup` — plus the globals the three largest
// components (`RunList`, `RunView`, `AssistantBar`) read at module scope or during render and a
// `HypothesisBoard` never needed: a jsdom `window`/`document`/`location`/storage, `matchMedia`,
// `requestAnimationFrame`, `ResizeObserver`, and a `fetch` stub keyed by path.
//
// WHAT A STATIC RENDER IS, so a test written on it claims no more than it drives: React runs the
// component's render function and NOT its effects, so nothing polls, nothing subscribes, nothing
// fetches — `harness.fetch.calls` stays empty and a test may assert exactly that (a read that runs
// during render would be the bug). What it DOES see is every gate a prop decides at render time: a
// restored navigation state selecting a view (`aria-pressed`), review mode marking the workspace
// read-only (a root class), the Assistant collapsing to nothing (`hidden`). Those are the flips the
// source-pin tests could not see, and each mount test asserts one.
//
// No fake timers, on purpose: nothing a static render runs schedules one, and a timer that fired
// would be a render-time side effect the `fetch.calls` assertion is there to catch. Each test file
// runs in its own `node --test` process, so the globals installed here never leak across files.
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const DOM_GLOBALS = ['window', 'document', 'location', 'navigator', 'history', 'localStorage',
  'sessionStorage', 'HTMLElement', 'Node', 'CustomEvent', 'Event']
// The one warning a static render of a layout-effect component always prints; everything else
// `console.error` receives is a real render problem and stays visible.
const SSR_LAYOUT_EFFECT_WARNING = 'useLayoutEffect does nothing on the server'

export function installDom({ url = 'http://localhost/' } = {}) {
  if (globalThis.__looplabMountDom) return globalThis.__looplabMountDom
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url })
  for (const key of DOM_GLOBALS) {
    try { globalThis[key] = dom.window[key] } catch {
      Object.defineProperty(globalThis, key, { value: dom.window[key], configurable: true })
    }
  }
  const matchMedia = query => ({
    matches: false, media: query, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
    dispatchEvent() { return false },
  })
  globalThis.matchMedia = matchMedia
  dom.window.matchMedia = matchMedia
  globalThis.requestAnimationFrame = callback => setTimeout(callback, 0)
  globalThis.cancelAnimationFrame = id => clearTimeout(id)
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const original = console.error
  console.error = (...args) => {
    if (!String(args[0]).includes(SSR_LAYOUT_EFFECT_WARNING)) original(...args)
  }
  globalThis.__looplabMountDom = dom
  return dom
}

// A `fetch` keyed by PATH: `routes` maps a pathname to the JSON it answers with; an unstubbed path
// answers 404 rather than an empty 200, so a render that reads a route nobody declared fails loudly
// instead of rendering "no data" convincingly. Every call is recorded on `stub.calls`.
export function fetchStub(routes = {}) {
  const calls = []
  const stub = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost/')
    calls.push({ path: url.pathname, method: String(init.method || 'GET').toUpperCase() })
    const known = Object.hasOwn(routes, url.pathname)
    return new Response(JSON.stringify(known ? routes[url.pathname] : { error: 'unstubbed route' }), {
      status: known ? 200 : 404, headers: { 'content-type': 'application/json' },
    })
  }
  stub.calls = calls
  return stub
}

export async function mountHarness({ routes = {} } = {}) {
  installDom()
  const fetch = fetchStub(routes)
  globalThis.fetch = fetch
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  return {
    vite,
    fetch,
    load: path => vite.ssrLoadModule(path),
    render: (Component, props = {}) => renderToStaticMarkup(React.createElement(Component, props)),
    close: () => vite.close(),
  }
}
