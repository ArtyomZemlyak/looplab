import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import axe from 'axe-core'
import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const blockingViolations = violations => violations
  .filter(violation => violation.impact === 'critical' || violation.impact === 'serious')
  .map(violation => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map(node => ({
      target: node.target,
      failureSummary: node.failureSummary,
    })),
  }))

test('Attention Center stays on the owner plane and out of review/shared routes', async () => {
  const [appSource, workspaceSource, centerSource] = await Promise.all([
    readFile(new URL('../src/App.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/OwnerWorkspace.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/AttentionCenter.jsx', import.meta.url), 'utf8'),
  ])

  assert.equal((workspaceSource.match(/<AttentionComponent\b/g) || []).length, 1)
  assert.match(workspaceSource, /<AttentionComponent \/>/)
  assert.match(workspaceSource, /const AttentionCenter = lazy\(/,
    'the owner-only poller must remain deferred until the owner workspace mounts')
  const reviewReturn = appSource.indexOf("if (route.view === 'review') return")
  const sharedReturn = appSource.indexOf("if (route.view === 'shared') return")
  const ownerGate = appSource.indexOf('<OwnerAuth label={routeLabel}>')
  const ownerMount = appSource.indexOf('<OwnerWorkspace route={route}>')
  assert.ok(reviewReturn >= 0 && ownerMount > reviewReturn,
    'the review route must return before owner-only Attention Center effects can mount')
  assert.ok(sharedReturn > reviewReturn && ownerGate > sharedReturn && ownerMount > ownerGate,
    'the public shared route must return before OwnerAuth and owner-only Attention Center mount')

  assert.doesNotMatch(centerSource, /requestPermission\s*\(/,
    'permission prompts belong only to the explicit enable operation')
  assert.doesNotMatch(centerSource,
    /\b(?:assistantResolve|post|putText|sendRunCommand|runCommand)\s*\(/,
    'the center must not embed a backend mutation primitive in a passive effect')
})

test('Attention Center trigger and open dialog satisfy the accessibility/passive-effect contract',
  async () => {
    const dom = new JSDOM(
      '<!doctype html><html lang="en"><head><title>Attention contract</title></head>'
      + '<body><main><h1>Owner workspace</h1><div id="root"></div></main></body></html>',
      { url: 'https://looplab.test/', pretendToBeVisual: true, runScripts: 'outside-only' },
    )
    let permissionRequests = 0
    let presentations = 0
    let storageWrites = 0
    const fetchCalls = []
    class NotificationApi {
      static permission = 'default'
      static async requestPermission() { permissionRequests += 1; return 'denied' }
      constructor() { presentations += 1 }
    }
    class Channel {
      postMessage() {}
      close() {}
    }
    const originalSetItem = dom.window.Storage.prototype.setItem
    dom.window.Storage.prototype.setItem = function (...args) {
      storageWrites += 1
      return originalSetItem.apply(this, args)
    }
    const fetchStub = async (input, options = {}) => {
      const url = String(input)
      fetchCalls.push({ url, method: String(options.method || 'GET').toUpperCase() })
      const payload = url.includes('/api/assistant/permissions')
        ? { pending: [] }
        : { items: [], partial: false, truncated: false, next_cursor: null }
      return { ok: true, json: async () => payload }
    }
    const installed = {
      window: dom.window,
      document: dom.window.document,
      navigator: dom.window.navigator,
      location: dom.window.location,
      localStorage: dom.window.localStorage,
      sessionStorage: dom.window.sessionStorage,
      Storage: dom.window.Storage,
      HTMLElement: dom.window.HTMLElement,
      Element: dom.window.Element,
      Node: dom.window.Node,
      Event: dom.window.Event,
      CustomEvent: dom.window.CustomEvent,
      KeyboardEvent: dom.window.KeyboardEvent,
      MouseEvent: dom.window.MouseEvent,
      getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
      requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
      cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
      fetch: fetchStub,
      Notification: NotificationApi,
      BroadcastChannel: Channel,
      IS_REACT_ACT_ENVIRONMENT: true,
    }
    const previousGlobals = Object.fromEntries(Object.keys(installed)
      .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }

    const vite = await createServer({
      root: UI_ROOT,
      configFile: false,
      appType: 'custom',
      logLevel: 'silent',
      server: { middlewareMode: true },
    })
    let root
    try {
      const [{ createRoot }, { default: AttentionCenter }] = await Promise.all([
        import('react-dom/client'), vite.ssrLoadModule('/src/AttentionCenter.jsx'),
      ])
      root = createRoot(document.querySelector('#root'))
      await React.act(async () => {
        root.render(React.createElement(AttentionCenter))
        await new Promise(resolve => setTimeout(resolve, 0))
      })
      for (let attempt = 0; attempt < 5 && fetchCalls.length < 2; attempt++) {
        await React.act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
      }

      const trigger = document.querySelector('.attention-trigger')
      assert.ok(trigger)
      assert.equal(trigger.tagName, 'BUTTON')
      assert.equal(trigger.type, 'button')
      assert.equal(trigger.getAttribute('aria-haspopup'), 'dialog')
      assert.equal(trigger.getAttribute('aria-expanded'), 'false')
      assert.equal(trigger.getAttribute('aria-label'), 'Open attention center')
      const controlledId = trigger.getAttribute('aria-controls')
      assert.ok(controlledId)
      assert.equal(document.getElementById(controlledId), null,
        'the closed drawer is absent from the accessibility tree')
      assert.equal(document.querySelector('[role="dialog"]'), null)

      assert.equal(fetchCalls.length, 2)
      assert.ok(fetchCalls.some(call => call.url.includes('/api/attention?limit=200')))
      assert.ok(fetchCalls.some(call => call.url.includes('/api/assistant/permissions')))
      assert.ok(fetchCalls.every(call => call.method === 'GET'),
        'passive polling must never issue a backend mutation')
      assert.equal(permissionRequests, 0,
        'mounting and polling must never ask for notification permission')
      assert.equal(presentations, 0, 'mounting must not display an OS notification')
      assert.equal(storageWrites, 0, 'mounting must not mutate attention preferences')

      trigger.focus()
      await React.act(async () => { trigger.click() })
      assert.equal(trigger.getAttribute('aria-expanded'), 'true')
      const dialog = document.getElementById(controlledId)
      assert.ok(dialog)
      assert.equal(dialog.getAttribute('role'), 'dialog')
      assert.equal(dialog.getAttribute('aria-modal'), 'true')
      assert.ok(document.getElementById(dialog.getAttribute('aria-labelledby')))
      assert.ok(document.getElementById(dialog.getAttribute('aria-describedby')))
      const close = dialog.querySelector('[data-dialog-initial-focus]')
      assert.equal(document.activeElement, close,
        'opening moves focus to the explicit initial dialog control')

      dom.window.eval(axe.source)
      const results = await dom.window.axe.run(dom.window.document, {
        runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
        rules: {
          // jsdom has no layout engine, so rendered contrast cannot be computed here.
          'color-contrast': { enabled: false },
        },
      })
      const blocking = blockingViolations(results.violations)
      assert.equal(blocking.length, 0, JSON.stringify(blocking, null, 2))

      const focusable = [...dialog.querySelectorAll(
        'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      )]
      const first = focusable[0]
      const last = focusable.at(-1)
      last.focus()
      await React.act(async () => {
        last.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Tab', bubbles: true, cancelable: true,
        }))
      })
      assert.equal(document.activeElement, first, 'Tab wraps within the drawer')

      await React.act(async () => {
        first.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Escape', bubbles: true, cancelable: true,
        }))
      })
      assert.equal(document.querySelector('[role="dialog"]'), null)
      assert.equal(trigger.getAttribute('aria-expanded'), 'false')
      assert.equal(document.activeElement, trigger, 'closing restores focus to the trigger')
      assert.equal(permissionRequests, 0)
      assert.equal(storageWrites, 0)
    } finally {
      if (root) await React.act(async () => { root.unmount() })
      await vite.close()
      dom.window.close()
      for (const [key, descriptor] of Object.entries(previousGlobals)) {
        if (descriptor === undefined) delete globalThis[key]
        else Object.defineProperty(globalThis, key, descriptor)
      }
    }
  })
