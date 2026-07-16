import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('owner auth and public shared chat expose fenced, retryable resource truth without raw errors', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const deadlines = new Map()
  const nativeSetTimeout = globalThis.setTimeout
  const nativeClearTimeout = globalThis.clearTimeout
  let timerId = 0
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => nativeSetTimeout(callback, 0),
    cancelAnimationFrame: handle => nativeClearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise(resolve => requests.push({
      url: String(url), options, resolve,
    })),
    setTimeout: (callback, delay, ...args) => {
      if (delay !== 10_000 && delay !== 15_000) return nativeSetTimeout(callback, delay, ...args)
      const id = ++timerId
      deadlines.set(id, { callback, delay })
      return id
    },
    clearTimeout: id => {
      if (!deadlines.delete(id)) nativeClearTimeout(id)
    },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  const settle = async () => {
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  }
  const reply = (request, payload, status = 200) => act(async () => {
    request.resolve({
      ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
    })
    await settle()
  })
  const click = button => act(async () => {
    button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    await settle()
  })
  const submitTwice = form => act(async () => {
    form.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
    form.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
    await settle()
  })
  const setInput = (input, value) => act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value)
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
    await settle()
  })
  const fireDeadline = delay => act(async () => {
    const deadline = [...deadlines.values()].find(entry => entry.delay === delay)
    assert.ok(deadline, `expected a ${delay}ms deadline`)
    deadline.callback()
    await settle()
  })
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: OwnerAuth }, { default: SharedAssistant }] = await Promise.all([
      import('react-dom/client'),
      vite.ssrLoadModule('/src/OwnerAuth.jsx'),
      vite.ssrLoadModule('/src/SharedAssistant.jsx'),
    ])
    root = createRoot(document.getElementById('root'))

    await act(async () => { root.render(React.createElement(OwnerAuth, null,
      React.createElement('div', { 'data-owner-ready': true }, 'Owner controls'))); await settle() })
    assert.equal(requests.length, 1)
    assert.match(requests[0].url, /\/api\/auth\/status$/)
    assert.ok(requests[0].options.signal instanceof AbortSignal)
    await reply(requests[0], { required: true, authenticated: false })
    const input = document.getElementById('owner-token')
    assert.equal(document.activeElement, input)
    await setInput(input, 'secret-owner-entry')

    await submitTwice(document.querySelector('form'))
    assert.equal(requests.length, 2, 'double submit stays single-flight')
    assert.match(requests[1].url, /\/api\/auth\/verify$/)
    assert.equal(requests[1].options.headers['X-LoopLab-Token'], 'secret-owner-entry')
    assert.ok(requests[1].options.signal instanceof AbortSignal)
    await reply(requests[1], { detail: 'private provider URL and account metadata' }, 500)
    assert.match(document.querySelector('[role="alert"]').textContent, /could not be verified.*entry was kept/i)
    assert.doesNotMatch(document.body.textContent, /private provider|account metadata/i)
    assert.equal(input.value, 'secret-owner-entry')
    assert.equal(document.activeElement, document.querySelector('[role="alert"]'))

    await submitTwice(document.querySelector('form'))
    assert.equal(requests.length, 3)
    await fireDeadline(10_000)
    assert.equal(requests[2].options.signal.aborted, true)
    assert.match(document.querySelector('[role="alert"]').textContent, /verification timed out.*entry was kept/i)
    assert.equal(input.value, 'secret-owner-entry')
    await reply(requests[2], { ok: true, required: true })
    assert.equal(document.querySelector('[data-owner-ready]'), null, 'late verification cannot unlock the gate')
    assert.equal(sessionStorage.getItem('ll.owner-token'), null, 'late verification cannot persist the token')

    await submitTwice(document.querySelector('form'))
    assert.equal(requests.length, 4)
    await reply(requests[3], { ok: true, required: true })
    assert.equal(document.querySelector('[data-owner-ready]')?.textContent, 'Owner controls')
    assert.equal(sessionStorage.getItem('ll.owner-token'), 'secret-owner-entry')

    await act(async () => { root.unmount(); await settle() })
    root = createRoot(document.getElementById('root'))
    await act(async () => { root.render(React.createElement(SharedAssistant, {
      sid: 'shared/one', onBack() {},
    })); await settle() })
    assert.match(requests[4].url, /\/api\/assistant\/shared\/shared%2Fone$/)
    assert.ok(requests[4].options.signal instanceof AbortSignal)
    const firstSession = {
      meta: { shared: true, title: 'First shared chat' },
      messages: [{ role: 'assistant', content: 'Last good transcript' }],
    }
    await reply(requests[4], firstSession)
    assert.match(document.body.textContent, /First shared chat.*Last good transcript/)
    assert.equal(document.querySelector('[role="log"]').getAttribute('aria-live'), 'off')

    const refresh = [...document.querySelectorAll('button')].find(node => node.textContent === 'Refresh')
    await act(async () => {
      refresh.click(); refresh.click(); await settle()
    })
    assert.equal(requests.length, 6, 'double refresh stays single-flight')
    await fireDeadline(15_000)
    const staleAlert = document.querySelector('[role="alert"]')
    assert.match(staleAlert.textContent, /loading timed out.*last loaded transcript.*Retry/i)
    assert.match(document.body.textContent, /Last good transcript/)
    assert.equal(document.activeElement, staleAlert)
    await reply(requests[5], {
      meta: { shared: true, title: 'Late wrong chat' }, messages: [],
    })
    assert.doesNotMatch(document.body.textContent, /Late wrong chat/)

    const retry = [...document.querySelectorAll('button')].find(node => node.textContent === 'Retry')
    await act(async () => { retry.click(); retry.click(); await settle() })
    assert.equal(requests.length, 7, 'double retry stays single-flight')
    await reply(requests[6], {
      meta: { shared: true, title: 'Malformed' },
      messages: [{ role: 'assistant', content: 'bad', activity: [{ type: 'tools' }] }],
    })
    assert.match(document.querySelector('[role="alert"]').textContent,
      /could not be loaded.*last loaded transcript/i)
    assert.doesNotMatch(document.body.textContent, /Malformed|\bbad\b/)
    assert.match(document.body.textContent, /Last good transcript/)

    await click([...document.querySelectorAll('button')].find(node => node.textContent === 'Retry'))
    assert.equal(requests.length, 8)
    await reply(requests[7], {
      meta: { shared: true, title: 'Updated shared chat' }, messages: [],
    })
    assert.match(document.body.textContent, /Updated shared chat.*has no messages/)

    await act(async () => { root.render(React.createElement(SharedAssistant, {
      sid: 'second', onBack() {},
    })); await settle() })
    const secondRequest = requests[8]
    await act(async () => { root.render(React.createElement(SharedAssistant, {
      sid: 'third', onBack() {},
    })); await settle() })
    assert.equal(secondRequest.options.signal.aborted, true)
    await reply(secondRequest, {
      meta: { shared: true, title: 'Superseded chat' }, messages: [],
    })
    assert.doesNotMatch(document.body.textContent, /Superseded chat/)
    await reply(requests[9], {
      meta: { shared: true, title: 'Current chat' }, messages: [],
    })
    assert.match(document.body.textContent, /Current chat/)
  } finally {
    if (root) await act(async () => { root.unmount(); await settle() })
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})
