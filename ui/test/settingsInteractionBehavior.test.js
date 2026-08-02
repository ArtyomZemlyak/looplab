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
    // Read the real deadline rather than restating it: this used to match a hardcoded 15_000, and
    // when the browser deadline was raised to outlive the server's 60s provider wall the intercept
    // stopped matching, so `healthDeadline` was never captured and the bounded half of this test
    // stopped running. Filled in below once Settings.jsx is loaded through vite; nothing sets a
    // timer of this length before the first click.
    let healthTimeoutMs = null
    const installed = {
      window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
      location: dom.window.location, sessionStorage: dom.window.sessionStorage,
      MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
      CustomEvent: dom.window.CustomEvent, IS_REACT_ACT_ENVIRONMENT: true,
      requestAnimationFrame: callback => native.setTimeout(callback, 0),
      cancelAnimationFrame: handle => native.clearTimeout(handle),
      setTimeout: (callback, delay, ...args) => {
        if (healthTimeoutMs != null && delay === healthTimeoutMs) {
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
      healthTimeoutMs = settingsModule.LLM_HEALTH_TIMEOUT_MS
      assert.ok(Number.isFinite(healthTimeoutMs) && healthTimeoutMs > 0,
        'Settings.jsx no longer exports the browser deadline this test intercepts')
      root = createRoot(document.getElementById('root'))
      // A paid probe is attributable to ONE saved configuration — that is what the result gets
      // fenced against when a save lands mid-flight — so it is refused until the saved revisions
      // are known, and it takes the shared settings-action lock through `beginAction`. Rendering
      // with neither (as this test used to) leaves the button permanently disabled, which made
      // "one probe" pass for the wrong reason: zero probes were started.
      let actions = 0
      const healthProps = {
        savedSettingsRevision: 'settings-r1',
        savedSecretRevision: 'secret-r1',
        beginAction: () => { actions += 1; return { id: actions } },
        finishAction: () => {},
      }
      await act(async () => {
        root.render(React.createElement(settingsModule.LlmHealth, { ...healthProps,
          savedSettingsRevision: '', savedSecretRevision: '' }))
        await flush()
      })
      const ungated = document.querySelector('button')
      assert.equal(ungated.disabled, true,
        'a probe with no saved identity has nothing to fence its result against')
      await act(async () => {
        ungated.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
        await flush()
      })
      assert.equal(healthCalls, 0, 'a disabled paid probe must not reach the provider')

      await act(async () => {
        root.render(React.createElement(settingsModule.LlmHealth, healthProps))
        await flush()
      })
      const button = document.querySelector('button')
      await act(async () => {
        button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
        button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
        await flush()
      })
      assert.equal(healthCalls, 1, 'same-tick double activation starts one paid provider probe')
      assert.equal(actions, 1, 'the second activation must not even take the settings-action lock')
      assert.equal(button.disabled, true)
      assert.ok(healthDeadline)
      await act(async () => { healthDeadline(); await flush() })
      assert.equal(button.disabled, false)
      // A timed-out probe left a persisted recovery fence, so the outcome is UNKNOWN, not failed —
      // the provider may well have been billed. The primary action therefore becomes the replay-only
      // lookup for that operation id rather than another billable check. This assertion used to read
      // /Test LLM/, from before the fence existed, and would now pass on a button that silently
      // re-bills the operator.
      assert.match(button.textContent, /Check previous result/)
      // The chip must say both halves of the truth the fence records: the browser has NO verified
      // result (so "failed" would be a lie — the provider may have been billed), and the offered
      // next step reuses the operation rather than starting a second call.
      const chipTitle = document.querySelector('.llm-health .chip').title
      assert.match(chipTitle, /no verified result/i)
      assert.match(chipTitle, /without starting a new provider call/i)
      assert.equal(healthCalls, 1, 'a timed-out probe must not auto-retry against the provider')

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
