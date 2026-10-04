import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, mountLive, settle, unanswered, until } from './_mount.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'

const run = { run_id: 'demo', generation, finished: true, phase: 'finished', engine_running: false,
  direction: 'max', source_integrity: { complete: true }, best_metric_caveats: [],
  result_summary: { first: { node_id: 0, attempt: 0, value: 0.4, confirmed: false, seeds: null },
    selected: { node_id: 2, attempt: 1, value: 0.5, confirmed: false, seeds: null } } }

// Reject a real dynamic import, after the user has begun drafting. The surrounding
// OwnerWorkspace is real too: without a local boundary its Assistant fallback replaces
// the whole chat. No production loader or React.lazy implementation is mocked.
for (const target of ['AssistantRunResult', 'AssistantResults']) {
  test(`${target} import failure preserves Assistant, draft and focus`, async () => {
    let release
    globalThis.__looplabResultLoadFailure = { promise: new Promise(resolve => { release = resolve }) }
    const harness = await mountLive({ visible: true, plugins: [{
      name: 'doc72-result-load-failure', enforce: 'pre',
      transform(_code, id) {
        if (id.replaceAll('\\', '/').endsWith(`/src/${target}.jsx`)) return {
          code: `await globalThis.__looplabResultLoadFailure.promise;
            throw new Error('doc72 injected ${target} import failure');
            export default function UnavailableResult() { return null }`, map: null,
        }
      },
    }] })
    const originalError = console.error
    const expectedErrors = []
    console.error = (...args) => {
      const message = args.map(String).join(' ')
      if (message.includes('doc72 injected') || message.includes('The above error occurred')) {
        expectedErrors.push(message)
      } else originalError(...args)
    }
    let mounted
    try {
      localStorage.clear()
      sessionStorage.clear()
      const { default: AssistantBar } = await harness.load('/src/AssistantBar.jsx')
      const { default: OwnerWorkspace } = await harness.load('/src/OwnerWorkspace.jsx')
      const backend = fetchStub({
        'GET /api/assistant/commands': { commands: [] },
        'GET /api/assistant/sessions': { sessions: [] },
        'GET /api/assistant/watches': { watches: [] },
        'GET /api/runs': [run],
        'GET /api/runs/demo/result-notices': payload([node]),
        'GET /api/assistant/permissions': ({ init }) => unanswered(init),
        'GET /api/assistant/progress': ({ init }) => unanswered(init),
      })
      globalThis.fetch = backend
      mounted = await harness.mount(OwnerWorkspace, { route: { view: 'run', id: 'demo' },
        children: React.createElement('main', null, 'Run workspace'),
        AssistantComponent: AssistantBar, AttentionComponent: () => null })
      const { container } = mounted
      const side = container.querySelector('button.cmdbar-drawer-btn')
      if (side) await click(side)
      await until(() => container.querySelector('.asst-result-details'), 'finished result disclosure')
      const disclosure = container.querySelector('.asst-result-details')
      if (target === 'AssistantRunResult') await React.act(async () => {
        disclosure.open = true
        disclosure.dispatchEvent(new Event('toggle'))
      })
      const input = container.querySelector('[aria-label="Assistant message"]')
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
          .set.call(input, 'Keep my pending question')
        input.dispatchEvent(new Event('input', { bubbles: true }))
        input.focus()
      })
      await React.act(async () => release())
      await until(() => container.querySelector('[role="alert"]'), 'local load failure')
      await settle()
      assert.ok(container.contains(input), 'the chat composer must survive the reader failure')
      assert.equal(input.value, 'Keep my pending question')
      assert.equal(document.activeElement, input, 'late load failure must not steal draft focus')
      assert.match(container.textContent, /Run workspace/)
      assert.doesNotMatch(container.textContent, /Assistant could not be opened/)
      assert.match(container.querySelector('[role="alert"]').textContent, /Reload LoopLab/)
      await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'ru' })))
      assert.match(container.querySelector('[role="alert"]').textContent, /Перезагрузить LoopLab/)
      assert.equal(document.activeElement, input)
      if (target === 'AssistantRunResult') {
        await until(() => container.querySelector('.asst-result-notice'), 'independent completion notices')
        assert.ok(container.querySelector('.asst-result-notice'), 'completion notices remain readable')
        await React.act(async () => {
          disclosure.open = false
          disclosure.dispatchEvent(new Event('toggle'))
        })
        assert.equal(disclosure.querySelector('[role="alert"]'), null)
      } else {
        await React.act(async () => {
          disclosure.open = true
          disclosure.dispatchEvent(new Event('toggle'))
        })
        await until(() => container.querySelector('.asst-run-result'), 'independent selected result')
      }
      assert.equal(input.value, 'Keep my pending question')
      assert.ok(expectedErrors.some(message => message.includes('doc72 injected')))
      assert.equal(backend.calls.some(call => call.method !== 'GET'), false)
    } finally {
      release()
      await mounted?.unmount()
      console.error = originalError
      await harness.close()
      delete globalThis.__looplabResultLoadFailure
    }
  })
}
