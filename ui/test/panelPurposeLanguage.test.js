import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { readFileSync } from 'node:fs'
import { fetchStub, jsonResponse, mountLive, settle } from './_mount.js'

// The per-tab purpose copy of the Memory and Authoring panels used to be MODULE constants built
// with `uiText`, so it was translated once, at chunk load: a page opened in English kept the
// English purpose paragraph after the operator switched to Russian (and the reverse), while every
// other line of the same panel followed the switch. The chunk is loaded in English here BEFORE
// the switch, which is exactly the order that froze it.
const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))

for (const [panel, english, props] of [
  ['MemoryPanel', 'What generalizes.', {}],
  ['AuthoringPanel', 'Role prompt overrides.', {}],
]) {
  test(`${panel} purpose copy follows a language switch made after the chunk loaded`, async () => {
    const harness = await mountLive(); localStorage.clear(); localStorage.setItem('looplab.language', 'en')
    const backend = fetchStub()
    globalThis.fetch = (...args) => String(args[0]).endsWith('/locales/ru.json')
      ? Promise.resolve(jsonResponse(catalogue)) : backend(...args)
    const locale = await harness.load('/src/uiLanguage.js')
    const panels = await harness.load('/src/panels.jsx')
    const view = await harness.mount(() => {
      locale.useUILanguage()
      return React.createElement(panels[panel], { onClose() {}, ...props })
    })
    try {
      await settle()
      assert.ok(view.container.textContent.includes(english), `${panel} renders its English purpose first`)
      await React.act(async () => { locale.setUILanguage('ru'); await locale.loadUILanguage() })
      await settle()
      const russian = catalogue.messages[english]
      assert.ok(russian && russian !== english, english)
      assert.ok(view.container.textContent.includes(russian), `${panel} purpose copy is Russian after the switch`)
      assert.ok(!view.container.textContent.includes(english), `${panel} kept the English purpose copy`)
    } finally { await harness.close() }
  })
}
