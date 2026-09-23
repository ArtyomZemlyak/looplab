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
//
// THE LIVE HALF (`mountLive`, review 2026-09-22, UI-05) is what a static render cannot be: a real
// `createRoot` under jsdom inside React's act environment, so the component's EFFECTS run — it
// polls, it opens its streams, it reads — and a test drives its DATA FLOW end to end: what the
// server answers, in what order, and what the operator then sees. It was built inside
// `assistantTranscriptRenders.test.js` (the first mount of a large component with its effects
// running) and hoisted here so the next one starts from the harness instead of a copy of it.
//
// TIME, for the live half. Nothing here fakes a clock and nothing here RACES one. A live test waits
// with `until`, which re-checks a predicate between event-loop turns under a CEILING only a hang
// reaches — never a window a loaded box can miss: a request/response chain takes a number of
// turns, not a number of milliseconds, so the load of the box changes how long a pass takes and not
// whether it passes. A test that needs a production timer to FIRE (a request deadline, a backoff)
// drives it with node:test's `mock.timers` (`useAttentionDeadline.test.js` is the worked example),
// never by compressing the production delay into a shorter real one — the shape that failed under
// load (a 60 ms stand-in deadline racing a 30 ms observation window). The helpers below keep REAL
// timer references captured when this module loads, so they still turn the event loop inside a test
// that has mocked `setTimeout`.
import assert from 'node:assert/strict'
import { performance } from 'node:perf_hooks'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const DOM_GLOBALS = ['window', 'document', 'location', 'navigator', 'history', 'localStorage',
  'sessionStorage', 'HTMLElement', 'Node', 'CustomEvent', 'Event']
// What event dispatch, form input and focus management read on top of the static set — the list
// the Assistant's live mount needed. A live test that needs more passes `globals`.
const LIVE_DOM_GLOBALS = ['Element', 'HTMLTextAreaElement', 'HTMLInputElement', 'MouseEvent',
  'KeyboardEvent', 'getComputedStyle', 'DocumentFragment']
// The one warning a static render of a layout-effect component always prints; everything else
// `console.error` receives is a real render problem and stays visible.
const SSR_LAYOUT_EFFECT_WARNING = 'useLayoutEffect does nothing on the server'
// Captured at module load, before any test can mock the globals (see TIME above).
const realSetTimeout = globalThis.setTimeout

// `visible`: jsdom's default document is a HIDDEN tab (`document.hidden === true`, visibility
// 'prerender'), so a poll that pauses in a background tab stays paused under it. A live drive of
// what an operator watching the page sees asks for a visible one. One document per process, so
// the first caller decides, and a later caller asking for the other kind is refused, not ignored.
export function installDom({ url = 'http://localhost/', visible = false } = {}) {
  if (globalThis.__looplabMountDom) {
    assert.equal(globalThis.__looplabMountDom.window.document.hidden, !visible,
      'this process already installed a document of the other visibility')
    return globalThis.__looplabMountDom
  }
  const dom = new JSDOM('<!doctype html><html><body></body></html>',
    { url, pretendToBeVisual: visible })
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

// A JSON response, fresh per call (a body can be read once).
export const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
})

// A request a drive deliberately leaves UNANSWERED: it settles only when its own signal aborts, so
// it can never publish a state change mid-drive, and a component that abandons it is seen doing so.
// Until the live harness CLOSES: then the server it stood in for is gone and every request still
// held is dropped the way a closed connection drops it. A read wrapped in a deadline with no abort
// handle (`requestDeadline.js::boundedRequest`) otherwise kept its 8 s timer, and with it the whole
// file's process, alive past the last test — measured 8.6 s on the Assistant drive.
const heldRequests = new Set()
export const unanswered = init => new Promise((_resolve, reject) => {
  const held = { reject }
  heldRequests.add(held)
  init?.signal?.addEventListener('abort', () => {
    heldRequests.delete(held)
    reject(init.signal.reason)
  }, { once: true })
})
const hangUp = () => {
  for (const held of heldRequests) held.reject(new TypeError('fetch failed'))
  heldRequests.clear()
}

// A `fetch` keyed by PATH: `routes` maps a pathname — or `'<METHOD> <pathname>'` to answer only
// that method — to the JSON it answers with, or to a HANDLER `({ method, path, url, init }) => …`
// that returns a `Response`, a promise of one (a request held open), or a JSON value. An unstubbed
// path answers 404 rather than an empty 200, so a render that reads a route nobody declared fails
// loudly instead of rendering "no data" convincingly. Every call is recorded on `stub.calls`, with
// the request body a live drive asserts on (`null` for a read).
export function fetchStub(routes = {}) {
  const calls = []
  const stub = async (input, init = {}) => {
    const href = typeof input === 'string' || input instanceof URL ? String(input) : input.url
    const url = new URL(href, 'http://localhost/')
    const method = String(init.method || 'GET').toUpperCase()
    const path = url.pathname
    calls.push({ path, method, body: init.body ?? null })
    const key = [`${method} ${path}`, path].find(name => Object.hasOwn(routes, name))
    if (key === undefined) return jsonResponse({ error: 'unstubbed route' }, 404)
    const route = routes[key]
    if (typeof route !== 'function') return jsonResponse(route)
    const answer = await route({ method, path, url, init })
    return answer instanceof Response ? answer : jsonResponse(answer)
  }
  stub.calls = calls
  return stub
}

// A live event-stream BODY the test writes frame by frame. `send(event, data, { id })` enqueues one
// frame — with an `id:` line when given, the cursor the run stream's frames carry — and `close()`
// ends it the way a server does. Given the request's `signal`, an abort ERRORS the body the way a
// browser's aborted fetch does, so a component that cancels its stream sees what it would see in a
// browser; without one the body ignores aborts (the Assistant drive's streams, which predate this).
export function sseStream({ signal } = {}) {
  const encoder = new TextEncoder()
  let controller
  let open = true
  const body = new ReadableStream({ start(c) { controller = c } })
  signal?.addEventListener('abort', () => {
    if (!open) return
    open = false
    try { controller.error(signal.reason) } catch { /* already errored or closed */ }
  }, { once: true })
  return {
    body,
    get open() { return open },
    response: () => new Response(body, {
      status: 200, headers: { 'content-type': 'text/event-stream' },
    }),
    send(event, data, { id } = {}) {
      assert.ok(open, `a ${event} frame was sent on a stream the component already closed`)
      const payload = typeof data === 'string' ? data : JSON.stringify(data)
      controller.enqueue(encoder.encode(
        `${id == null ? '' : `id: ${id}\n`}event: ${event}\ndata: ${payload}\n\n`))
    },
    close() {
      if (!open) return
      open = false
      controller.close()
    },
  }
}

// One real zero-delay turn of the event loop.
export const tick = () => new Promise(resolve => realSetTimeout(resolve, 0))

// Let everything already in flight land: a few act-wrapped turns, each flushing what the last one
// scheduled (a response resolving, the render it causes, the effect that render runs).
export async function settle(rounds = 3) {
  for (let round = 0; round < rounds; round += 1) {
    await React.act(async () => { await tick(); await tick() })
  }
}

// Wait until `predicate` holds, turning the event loop (inside `act`) between checks. The bound is
// a CEILING that only a hang reaches — `ceilingMs` of real time — never a race window: see TIME.
export async function until(predicate, what, { ceilingMs = 20_000 } = {}) {
  const started = performance.now()
  for (;;) {
    if (predicate()) return
    if (performance.now() - started > ceilingMs) assert.fail(`timed out waiting for ${what}`)
    await React.act(async () => { await new Promise(resolve => realSetTimeout(resolve, 5)) })
  }
}

// A click the way a user makes one: bubbling, cancelable, dispatched inside `act`. The event is
// returned so a test can read `defaultPrevented` (an in-app route taking the navigation).
export async function click(element, init = {}) {
  const event = new window.MouseEvent('click', { bubbles: true, cancelable: true, ...init })
  await React.act(async () => { element.dispatchEvent(event) })
  return event
}

// Animation frames the drive runs BY HAND. `installDom` turns a frame into a zero-delay timer,
// which runs whenever the event loop gets there; held, a frame a commit scheduled runs exactly when
// the test says — after the DOM it was scheduled against has changed under it, which is the case a
// frame must survive. `cancelAnimationFrame` drops a held frame the way the browser's would.
export function holdFrames() {
  const held = new Map()
  let lastId = 0
  const previous = [globalThis.requestAnimationFrame, globalThis.cancelAnimationFrame]
  globalThis.requestAnimationFrame = callback => {
    lastId += 1
    held.set(lastId, callback)
    return lastId
  }
  globalThis.cancelAnimationFrame = id => { held.delete(id) }
  return {
    take() { const frames = [...held.values()]; held.clear(); return frames },
    release() { [globalThis.requestAnimationFrame, globalThis.cancelAnimationFrame] = previous },
  }
}

export async function mountHarness({ routes = {}, visible = false } = {}) {
  installDom({ visible })
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

// The static harness plus a LIVE `mount`. `react-dom/client` is imported here, lazily, on purpose:
// a test that installs React's DevTools hook at module scope (the Assistant drive counts renders
// through it) must have it in place before the renderer is first evaluated, and a static import in
// this module would evaluate the renderer before the test module's own body runs.
//
// `mount(Component, props)` renders into a fresh container and returns `{ container, rerender,
// unmount }`: `rerender(props)` is an UPDATE of the same root — React compares this render's hooks
// with the previous one's, so a prop flip that changes the hook order is caught — never a remount;
// `unmount()` settles first, so a response already in flight lands on the live tree it was meant
// for, and a second call is a no-op. The `fetch` a drive answers with is installed by the test
// (`globalThis.fetch = fetchStub(routes)`) before it mounts, because the first effects read at
// once. `globals` names further jsdom constructors a component reads; `visible` is `installDom`'s.
export async function mountLive({ routes = {}, globals = [], visible = false } = {}) {
  const harness = await mountHarness({ routes, visible })
  const dom = globalThis.__looplabMountDom
  for (const key of [...LIVE_DOM_GLOBALS, ...globals]) globalThis[key] = dom.window[key]
  globalThis.IS_REACT_ACT_ENVIRONMENT = true
  const { createRoot } = await import('react-dom/client')
  const mounted = new Set()
  const mount = async (Component, props = {}) => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    const render = next => React.act(async () => {
      root.render(React.createElement(Component, next))
    })
    await render(props)
    let unmounted = false
    const handle = {
      container,
      rerender: next => render(next),
      async unmount() {
        if (unmounted) return
        unmounted = true
        mounted.delete(handle)
        await settle()
        await React.act(async () => { root.unmount() })
        container.remove()
      },
    }
    mounted.add(handle)
    return handle
  }
  return {
    ...harness,
    mount,
    // Unmount whatever a failed test left mounted before the server goes, so no effect of it
    // outlives the modules it runs; then drop every request the drive left held open.
    close: async () => {
      for (const handle of [...mounted]) await handle.unmount()
      hangUp()
      await harness.close()
    },
  }
}
