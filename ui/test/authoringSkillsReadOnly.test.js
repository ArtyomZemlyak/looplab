import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const ROOT_ID = `root-sha256:${'0'.repeat(64)}`
const REVISION = value => `sha256:${value.repeat(64)}`

test('Authoring renders nested skill packages read-only and keeps root skills writable', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const requests = []
  const nativeSetTimeout = globalThis.setTimeout
  const nativeClearTimeout = globalThis.clearTimeout
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
    HTMLTextAreaElement: dom.window.HTMLTextAreaElement,
    requestAnimationFrame: callback => nativeSetTimeout(callback, 0),
    cancelAnimationFrame: handle => nativeClearTimeout(handle),
    IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise(resolve => requests.push({
      url: String(url), options, resolve,
    })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  const reply = async (request, payload, status = 200) => act(async () => {
    request.resolve({
      ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
    })
    await Promise.resolve()
  })
  const click = node => act(async () => {
    node.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    await Promise.resolve()
  })
  const button = text => [...document.querySelectorAll('button')]
    .find(node => node.textContent.trim().startsWith(text))
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, panels, { createInspectorDraftStore }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/panels.jsx'),
      vite.ssrLoadModule('/src/inspectorDraftStore.js'),
    ])
    const draftStore = createInspectorDraftStore()
    draftStore.updateField('panel:authoring', 'documents', {
      ['skills\u0000package/SKILL.md']: {
        kind: 'skills', name: 'package/SKILL.md',
        savedText: '# stale package', draftText: '# forged recovered draft',
        savedRevision: REVISION('3'), observedText: '# stale package',
        observedRevision: REVISION('3'), savedTargetRootId: ROOT_ID,
        observedTargetRootId: ROOT_ID, truncated: false, readOnly: true,
        conflict: true, rootConflict: false, observationIncomplete: false,
        error: 'stale recovery', recoveryOperationId: 'forged', recoveryStorageRaw: 'forged',
      },
    }, {})
    root = createRoot(document.getElementById('root'))
    await act(async () => root.render(React.createElement(panels.AuthoringPanel, {
      onClose() {}, draftStore,
    })))
    await reply(requests.at(-1), {
      dir: '/prompts', target_root_id: ROOT_ID, files: [], truncated_files: 0,
      inventory_incomplete: false,
    })
    await click(button('skills'))
    await reply(requests.at(-1), {
      dir: '/skills', target_root_id: ROOT_ID, truncated_files: 1,
      inventory_incomplete: true,
      files: [
        {
          name: 'root.md', text: '# root', revision: REVISION('1'), truncated: false,
          read_only: false,
        },
        {
          name: 'package/SKILL.md', text: '# packaged', revision: REVISION('2'), truncated: false,
          read_only: true,
        },
      ],
    })

    assert.match(button('package/SKILL.md').textContent, /read-only package/)
    await click(button('package/SKILL.md'))
    const packaged = document.querySelector('textarea[aria-label="View package/SKILL.md"]')
    assert.ok(packaged)
    assert.equal(packaged.disabled, true)
    assert.equal(packaged.value, '# packaged')
    assert.equal(button('Save').disabled, true)
    assert.match(document.body.textContent,
      /package>\/SKILL\.md: read-only.*flat Save\/recovery API rejects slash paths/is)
    assert.match(document.body.textContent,
      /candidates need cross-task promotion for production.*not reviewed here/is)
    assert.match(document.body.textContent,
      /1\+ files omitted \(cap\)/is)
    assert.match(document.body.textContent,
      /package scan capped; omissions possible/is)
    assert.doesNotMatch(document.body.textContent, /forged recovered draft|stale recovery/i)
    const requestCount = requests.length
    await click(button('Save'))
    assert.equal(requests.length, requestCount, 'disabled read-only Save emitted a request')

    await click(button('root.md'))
    const writable = document.querySelector('textarea[aria-label="Edit root.md"]')
    assert.ok(writable)
    assert.equal(writable.disabled, false)
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        dom.window.HTMLTextAreaElement.prototype, 'value').set
      setter.call(writable, '# changed root')
      writable.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(button('Save').disabled, false)
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})
