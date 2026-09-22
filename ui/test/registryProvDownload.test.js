// THE W3C-PROV DOWNLOAD, rendered and clicked (review 2026-09-22, UI-08).
//
// `panels.jsx::RegistryPanel` re-implemented `accessibility.jsx::downloadBlob` inline and without its
// two Firefox fixes — it clicked a DETACHED anchor (Firefox fires no download for a synthetic click
// on one) and revoked the blob URL on the same tick (which can abort the download before it starts)
// — and its async click handler had no catch: a failed `/prov` read was an unhandled rejection and
// the button simply did nothing. The panel now saves through `downloadBlob` and says when it could
// not. This drives the real panel through a JSDOM client root, because the defect is what a CLICK
// does, which no static render can see.
import assert from 'node:assert/strict'
import test, { after, before } from 'node:test'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const PROV = { entity: { 'sol:0:0': { 'prov:label': 'solution node 0 · generation 0' } } }

let dom = null
let root = null
let vite = null
let RegistryPanel = null
const previous = {}
const previousUrl = { create: URL.createObjectURL, revoke: URL.revokeObjectURL }
const rejections = []
const onRejection = reason => { rejections.push(reason) }

// What the test arms per case, and what the page did.
let provAnswer = null
let createObjectUrl = null
const clicks = []
const blobs = []
const revoked = []

const response = (status, body) => ({
  ok: status >= 200 && status < 300, status, headers: { get: () => null }, json: async () => body,
})

before(async () => {
  process.on('unhandledRejection', onRejection)
  dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: cb => setTimeout(cb, 0), cancelAnimationFrame: h => clearTimeout(h),
    IS_REACT_ACT_ENVIRONMENT: true,
    fetch: async url => (String(url).endsWith('/prov') ? provAnswer() : response(200, [])),
  }
  for (const [name, value] of Object.entries(installed)) {
    previous[name] = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
  }
  // JSDOM implements neither object URLs nor navigation: record what the page asked for instead,
  // including whether the anchor was IN the document at the moment it was clicked.
  URL.createObjectURL = blob => createObjectUrl(blob)
  URL.revokeObjectURL = href => { revoked.push(href) }
  // `revokedSameTick`: was the URL already released by the time the click's own task finished (a
  // microtask later)? Revoking synchronously after `click()` is what can abort the download.
  dom.window.HTMLAnchorElement.prototype.click = function click() {
    const entry = { download: this.download, href: this.href, attached: this.isConnected }
    clicks.push(entry)
    queueMicrotask(() => { entry.revokedSameTick = revoked.length > 0 })
  }
  vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  ;({ RegistryPanel } = await vite.ssrLoadModule('/src/panels.jsx'))
  const { createRoot } = await import('react-dom/client')
  root = createRoot(document.getElementById('root'))
})

after(async () => {
  if (root) await act(async () => root.unmount())
  if (vite) await vite.close()
  for (const [name, descriptor] of Object.entries(previous)) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
  URL.createObjectURL = previousUrl.create
  URL.revokeObjectURL = previousUrl.revoke
  process.off('unhandledRejection', onRejection)
})

const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })

async function clickDownload(key) {
  clicks.length = 0
  blobs.length = 0
  revoked.length = 0
  await act(async () => root.render(React.createElement(RegistryPanel, {
    key, state: { run_id: 'demo', nodes: {} }, onClose() {},
  })))
  const button = [...document.querySelectorAll('button')]
    .find(candidate => candidate.textContent.includes('W3C-PROV graph'))
  assert.ok(button, 'the PROV download button renders')
  await act(async () => { button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })) })
  await settle()
  return document.getElementById('root')
}

test('a successful export saves through downloadBlob: attached anchor, revoke deferred', async () => {
  provAnswer = () => response(200, PROV)
  createObjectUrl = blob => { blobs.push(blob); return 'blob:https://looplab.test/prov-1' }
  const page = await clickDownload('ok')
  assert.equal(clicks.length, 1, 'exactly one download is started')
  assert.deepEqual(clicks[0], {
    download: 'demo_prov.json', href: 'blob:https://looplab.test/prov-1', attached: true,
    revokedSameTick: false,
  }, 'Firefox saves only an anchor that is IN the document, and only while its URL is alive')
  assert.deepEqual(revoked, ['blob:https://looplab.test/prov-1'], 'the URL is released after the click')
  assert.equal(blobs[0].type, 'application/json')
  assert.deepEqual(JSON.parse(await blobs[0].text()), PROV)
  assert.equal(document.querySelectorAll('a[download]').length, 0, 'the anchor is removed again')
  assert.equal(page.querySelector('[role="alert"]'), null, 'no failure notice on success')
  assert.deepEqual(rejections, [])
})

test('a failed PROV read is said inline, not swallowed as an unhandled rejection', async () => {
  provAnswer = () => response(500, { detail: 'the provenance projection failed' })
  createObjectUrl = () => { throw new Error('must not be reached') }
  const page = await clickDownload('server-error')
  assert.equal(clicks.length, 0)
  const alert = page.querySelector('[role="alert"]')
  assert.ok(alert, 'the failure must be visible beside the button')
  assert.match(alert.textContent, /W3C-PROV/)
  assert.match(alert.textContent, /the provenance projection failed/)
  assert.deepEqual(rejections, [], 'the click handler must not leak a rejection')
})

test('a browser that refuses the object URL is said inline too', async () => {
  provAnswer = () => response(200, PROV)
  createObjectUrl = () => { throw new Error('blob URLs are blocked here') }
  const page = await clickDownload('blocked')
  assert.equal(clicks.length, 0)
  assert.match(page.querySelector('[role="alert"]')?.textContent || '', /blob URLs are blocked here/)
  assert.deepEqual(rejections, [])
})
