import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

import { installNavigationLossGuard } from '../src/navigationLossGuard.js'
import { SETTINGS_SCHEMA } from './settingsSchemaFixture.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('settings navigation, paid health, and invalid-field focus are real interactions', async t => {
  await t.test('navigation guard cancels or explicitly allows a route change and warns on unload', () => {
    const dom = new JSDOM('<!doctype html><html><body></body></html>', {
      url: 'https://looplab.test/#/settings',
    })
    const allowRef = { current: false }
    let allow = false
    dom.window.confirm = () => allow
    const cleanup = installNavigationLossGuard({
      allowRef, guardedHash: '#/settings', message: () => 'Discard draft?', win: dom.window,
    })
    try {
      const unload = new dom.window.Event('beforeunload', { cancelable: true })
      dom.window.dispatchEvent(unload)
      assert.equal(unload.defaultPrevented, true)

      dom.window.history.replaceState(null, '', '/#/runs')
      dom.window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'))
      assert.equal(dom.window.location.hash, '#/settings')
      assert.equal(allowRef.current, false)

      allow = true
      dom.window.history.replaceState(null, '', '/#/runs')
      dom.window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'))
      assert.equal(dom.window.location.hash, '#/runs')
      assert.equal(allowRef.current, true)
    } finally {
      cleanup()
      dom.window.close()
    }
  })

  await t.test('Test LLM is immediate single-flight, bounded, and invalid summary focuses its field', async () => {
    const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
      url: 'https://looplab.test/#/settings', pretendToBeVisual: true,
    })
    const native = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout }
    const healthTimer = {}
    let healthDeadline = null
    let healthCalls = 0
    const installed = {
      window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
      location: dom.window.location, sessionStorage: dom.window.sessionStorage,
      MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
      CustomEvent: dom.window.CustomEvent, IS_REACT_ACT_ENVIRONMENT: true,
      requestAnimationFrame: callback => native.setTimeout(callback, 0),
      cancelAnimationFrame: handle => native.clearTimeout(handle),
      setTimeout: (callback, delay, ...args) => {
        if (delay === 15_000) {
          healthDeadline = () => callback(...args)
          return healthTimer
        }
        return native.setTimeout(callback, delay, ...args)
      },
      clearTimeout: handle => {
        if (handle !== healthTimer) native.clearTimeout(handle)
      },
      fetch: async url => {
        assert.match(String(url), /\/api\/llm\/health$/)
        healthCalls += 1
        return new Promise(() => {})
      },
    }
    const previous = Object.fromEntries(Object.keys(installed)
      .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
    const flush = async () => {
      for (let index = 0; index < 8; index += 1) await Promise.resolve()
      await new Promise(resolve => native.setTimeout(resolve, 0))
      for (let index = 0; index < 8; index += 1) await Promise.resolve()
    }
    let vite, root
    try {
      for (const [key, value] of Object.entries(installed)) {
        Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
      }
      vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom',
        logLevel: 'silent', server: { middlewareMode: true } })
      const [{ createRoot }, settingsModule, formModule] = await Promise.all([
        import('react-dom/client'), vite.ssrLoadModule('/src/Settings.jsx'),
        vite.ssrLoadModule('/src/SettingsForm.jsx'),
      ])
      root = createRoot(document.getElementById('root'))
      await act(async () => {
        root.render(React.createElement(settingsModule.LlmHealth))
        await flush()
      })
      const button = document.querySelector('button')
      await act(async () => {
        button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
        button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
        await flush()
      })
      assert.equal(healthCalls, 1, 'same-tick double activation starts one paid provider probe')
      assert.equal(button.disabled, true)
      assert.ok(healthDeadline)
      await act(async () => { healthDeadline(); await flush() })
      assert.equal(button.disabled, false)
      assert.match(button.textContent, /Test LLM/)
      assert.match(document.querySelector('.llm-health .chip').title,
        /timed out.*No automatic retry/i)

      const form = Object.fromEntries(Object.values(SETTINGS_SCHEMA.fieldByKey).map(field => [
        field.key,
        field.type === 'bool' ? false : field.type === 'enum' ? field.options[0] : '',
      ]))
      await act(async () => {
        root.render(React.createElement(formModule.default, {
          form, onChange() {}, schema: SETTINGS_SCHEMA,
          errors: { select_verifier_samples: 'Enter a valid value.' },
          focusKey: 'select_verifier_samples', focusRequest: 1,
        }))
        await flush()
      })
      await act(async () => { await flush(); await flush() })
      assert.ok(document.querySelector('[name="select_verifier_samples"]'))
      assert.equal(document.activeElement?.getAttribute('name'), 'select_verifier_samples')
      assert.equal(document.activeElement?.getAttribute('aria-invalid'), 'true')
    } finally {
      if (root) await act(async () => { root.unmount(); await flush() })
      if (vite) await vite.close()
      for (const [key, descriptor] of Object.entries(previous)) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor)
        else delete globalThis[key]
      }
      dom.window.close()
    }
  })
})
