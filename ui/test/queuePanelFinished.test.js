import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import React from 'react'

import { fetchStub, jsonResponse, mountLive, settle } from './_mount.js'

// Measured 2026-10-11 on the offline demo (finished): Progress → Queue called itself "the live
// work-list"; in Russian its title read «Квью», the badge and every counter stayed English (`Stat`
// skipped `uiText`, as did the badge), and the intro was a broken machine translation.

const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8'))
const LIVE = 'The next experiment is chosen by the search policy; this is the live work-list — cancel a pending experiment, or add one from a node’s “Explore from here” / “Merge with…” menu on the graph.'
const FINISHED = 'This run has finished, so nothing is being built or evaluated. An experiment added from a node’s “Explore from here” / “Merge with…” menu on the graph is listed here until the run serves it.'
const COUNTERS = ['building', 'training / evaluating', 'waiting for a slot']

const run = finished => ({ run_id: 'demo', goal: 'g', direction: 'min', finished,
  phase: finished ? 'finished' : 'running', nodes: {} })

test('the Queue intro of a finished run is not a live work-list, in either language', async () => {
  const harness = await mountLive()
  localStorage.clear(); localStorage.setItem('looplab.language', 'en')
  const backend = fetchStub()
  globalThis.fetch = (...args) => String(args[0]).endsWith('/locales/ru.json')
    ? Promise.resolve(jsonResponse(catalogue)) : backend(...args)
  const locale = await harness.load('/src/uiLanguage.js')
  const { QueuePanel } = await harness.load('/src/panels.jsx')
  const view = await harness.mount(({ finished }) => {
    locale.useUILanguage()
    return React.createElement(QueuePanel, { state: run(finished), runId: 'demo', onClose() {} })
  }, { finished: true })
  try {
    await settle()
    assert.ok(view.container.textContent.includes(FINISHED))
    assert.ok(!view.container.textContent.includes('live work-list'))
    await view.rerender({ finished: false })
    assert.ok(view.container.textContent.includes(LIVE), 'a live run keeps its work-list sentence')
    await view.rerender({ finished: true })

    await React.act(async () => { locale.setUILanguage('ru'); await locale.loadUILanguage() })
    await settle()
    const text = view.container.textContent
    for (const english of ['Queue', FINISHED, '{0} planned / in-flight', ...COUNTERS]) {
      const russian = catalogue.messages[english]
      assert.ok(russian && russian !== english, `no Russian entry: ${english}`)
      assert.ok(text.includes(russian.replace('{0}', '0')), `not translated: ${english}`)
    }
    assert.ok(!/Квью|planned \/ in-flight|building|waiting for a slot/.test(text), text)
  } finally {
    localStorage.clear()
    await harness.close()
  }
})
