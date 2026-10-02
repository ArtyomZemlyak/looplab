import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'
import { modelConnectionView, publishModelCheck, readModelCheck, MODEL_CHECK_EVENT } from '../src/modelConnection.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const snapshot = (credential = { status: 'missing' }) => ({
  settings: { llm_model: 'qwen3:8b', llm_base_url: 'http://localhost:11434/v1' },
  settings_revision: 'settings-1', secret_revision: 'secret-1', credential,
})

test('first-run status does not mistake saved local defaults or a missing key for a live model', () => {
  assert.match(modelConnectionView(snapshot(), null).text, /Connection unverified/)
  assert.equal(modelConnectionView(snapshot(), null).tone, '')
  assert.match(modelConnectionView(snapshot({ status: 'endpoint_mismatch' }), null).text,
    /Shared key needs attention/)
  assert.match(modelConnectionView({ error: true }, null).text, /Could not read/)
})

test('only a check for both current revisions can change the first-run status', () => {
  const old = { settingsRevision: 'settings-1', secretRevision: 'old', outcome: 'passed' }
  assert.match(modelConnectionView(snapshot(), old).text, /Connection unverified/)
  const current = { ...old, secretRevision: 'secret-1' }
  assert.match(modelConnectionView(snapshot(), current).text, /Last explicit test passed/)
  assert.match(modelConnectionView(snapshot(), { ...current, outcome: 'failed' }).text,
    /Last test failed/)
  assert.match(modelConnectionView(snapshot(), { ...current, outcome: 'unknown' }).text,
    /outcome unknown/)
})

test('Russian model guidance preserves the same revision fence and explicit outcomes', () => {
  const current = { settingsRevision: 'settings-1', secretRevision: 'secret-1', outcome: 'passed' }
  assert.equal(modelConnectionView(snapshot(), current, 'ru').tone, 'ok')
  assert.match(modelConnectionView(snapshot(), current, 'ru').text, /проверка связи пройдена/)
  assert.match(modelConnectionView(snapshot(), { ...current, secretRevision: 'old' }, 'ru').text, /ещё не проверена/)
  assert.match(modelConnectionView(snapshot(), { ...current, outcome: 'unknown' }, 'ru').text, /Результат проверки неизвестен/)
  assert.match(modelConnectionView({ error: true }, current, 'ru').text, /Не удалось прочитать/)
  assert.match(modelConnectionView({ settings: {} }, current, 'ru').text, /укажите модель/)
})

test('check signal contains only revision identity and a bounded outcome', () => {
  const originalWindow = globalThis.window
  const originalEvent = globalThis.CustomEvent
  const seen = []
  globalThis.window = { dispatchEvent: event => seen.push(event) }
  globalThis.CustomEvent = class { constructor(type, options) { this.type = type; this.detail = options.detail } }
  try {
    publishModelCheck('settings-1', 'secret-1', 'passed')
    publishModelCheck('settings-1', 'secret-1', 'pretend-success')
    assert.equal(seen.length, 1)
    assert.equal(seen[0].type, MODEL_CHECK_EVENT)
    assert.deepEqual(seen[0].detail, {
      settingsRevision: 'settings-1', secretRevision: 'secret-1', outcome: 'passed',
    })
    assert.deepEqual(readModelCheck(), seen[0].detail,
      'the result remains available after Settings collapses the Assistant view')
  } finally {
    globalThis.window = originalWindow
    globalThis.CustomEvent = originalEvent
  }
})

test('first-run card reads settings without probing and refreshes after returning from Settings', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/#/', pretendToBeVisual: true,
  })
  let revision = 'settings-1'
  const calls = []
  let opened = 0
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, localStorage: dom.window.localStorage,
    sessionStorage: dom.window.sessionStorage, CustomEvent: dom.window.CustomEvent,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
    fetch: async (url, options) => {
      calls.push({ url: String(url), method: options?.method || 'GET' })
      return { ok: true, json: async () => ({ ...snapshot(), settings_revision: revision }) }
    },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let vite, root
  const flush = async () => {
    for (let i = 0; i < 8; i += 1) await Promise.resolve()
    await new Promise(resolve => setTimeout(resolve, 0))
  }
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom',
      logLevel: 'silent', server: { middlewareMode: true } })
    const [{ createRoot }, component, connection] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/FirstRunModelStatus.jsx'),
      vite.ssrLoadModule('/src/modelConnection.js'),
    ])
    root = createRoot(document.getElementById('root'))
    await act(async () => {
      root.render(React.createElement(component.default, { onSettings: () => { opened += 1 } }))
      await flush()
    })
    assert.match(document.querySelector('[role="status"]').textContent, /Connection unverified/)
    assert.deepEqual(calls, [{ url: '/api/settings', method: 'GET' }])

    await act(async () => {
      connection.publishModelCheck(revision, 'secret-1', 'passed')
      await flush()
    })
    assert.match(document.querySelector('[role="status"]').textContent, /Last explicit test passed/)

    await act(async () => {
      root.render(null)
      await flush()
    })
    await act(async () => {
      root.render(React.createElement(component.default, { onSettings: () => { opened += 1 } }))
      await flush()
    })
    assert.match(document.querySelector('[role="status"]').textContent, /Last explicit test passed/,
      'collapsing the Assistant while Settings tests the model must preserve the result')

    revision = 'settings-2'
    await act(async () => {
      dom.window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'))
      await flush()
    })
    assert.match(document.querySelector('[role="status"]').textContent, /Connection unverified/)
    assert.equal(calls.length, 3)
    await act(async () => {
      document.querySelector('.asst-new-run-hint button').click()
    })
    assert.equal(opened, 1)
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
