// The Assistant's provider-failure card, met by every first-time user with no model: it said only
// "check the connection", while the Offline demo on the Runs page needs no model at all; and on the
// Russian UI its title was translated but its message printed in English (`<p>{error.message}</p>`).
// Found by sending a goal to the Assistant on a box with no model (2026-10-10). Driven through the
// real `Turn` with the real catalogue, switched to Russian after mount.
import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { readFileSync } from 'node:fs'
import { fetchStub, jsonResponse, mountLive, settle } from './_mount.js'

import { assistantErrorInfo } from '../src/assistantErrors.js'

const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))

test('only an unreachable provider points to the Offline demo', () => {
  assert.match(assistantErrorInfo('', 'unavailable').hint, /Offline demo/)
  for (const kind of ['rate_limit', 'credentials', 'provider_error'])
    assert.equal(assistantErrorInfo('', kind).hint, undefined, kind)
})

test('the failure card names the Offline demo and translates every line', async () => {
  const harness = await mountLive(); localStorage.clear(); localStorage.setItem('looplab.language', 'en')
  const backend = fetchStub()
  globalThis.fetch = (...args) => String(args[0]).endsWith('/locales/ru.json')
    ? Promise.resolve(jsonResponse(catalogue)) : backend(...args)
  const locale = await harness.load('/src/uiLanguage.js')
  const { Turn } = await harness.load('/src/AssistantChat.jsx')
  const failed = { role: 'assistant', content: 'assistant error: connection refused', error_kind: 'unavailable' }
  const view = await harness.mount(() => {
    locale.useUILanguage()
    return React.createElement(Turn, { m: failed, runsById: {}, onRetry() {}, onOpenSettings() {} })
  })
  try {
    await settle()
    const info = assistantErrorInfo('', 'unavailable')
    for (const line of [info.title, info.message, info.hint])
      assert.ok(view.container.textContent.includes(line), line)
    await React.act(async () => { locale.setUILanguage('ru'); await locale.loadUILanguage() })
    await settle()
    for (const line of [info.title, info.message, info.hint]) {
      const russian = catalogue.messages[line]
      assert.ok(russian && russian !== line, line)
      assert.ok(view.container.textContent.includes(russian), `not translated: ${line}`)
    }
  } finally { await harness.close() }
})
