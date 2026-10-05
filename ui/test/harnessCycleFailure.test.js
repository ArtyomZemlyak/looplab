import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, mountLive, settle, until } from './_mount.js'

const generation = 'a'.repeat(64)
const progress = language => ({ generation, event_seq: 12, at_node: 2, complete: true,
  next_step: { language, code: 'choose_direction', owner: 'external_agent',
    title: 'Choose the next experiment', detail: 'Read measured evidence first.',
    reads: ['GET /api/runs/{run_id}/state'], action: null, phase_id: 'developer' },
  source_health: { events: { read_complete: true }, ...Object.fromEntries(['decisions', 'reviews', 'checkpoints']
    .map(kind => [kind, { accepted_rows: 0, read_complete: true }])) },
  history: Object.fromEntries(['decisions', 'reviews', 'checkpoints'].map(kind => [kind,
    { total: 0, offset: 0, limit: 20, items: [], has_more: false }])),
  candidate_requirements: { effective_concepts: true, hypothesis_statement: true },
  candidate_blockers_if_expanding: [], candidate_decisions_per_idea: {},
  finish_report_due: false, finish_reviews_due: [], finish_pending_nodes: [2],
  pending_checkpoint_count: 0, pending_checkpoints: [], pending_checkpoints_truncated: false,
})
const names = items => ({ items, total: items.length, truncated: false })
const handoff = { version: 1, generation, run_id: 'demo', run_uid: 'incarnation', event_seq: 12,
  mode: 'external_harness', engine_running: false, agent_connection: 'not_measured',
  credential_configured: true, server_paths: { run_root: 'C:/Runs', run_dir: 'C:/Runs/demo' },
  workspace: { kind: 'repository', source_paths: names(['C:/Project']), edit_surface: names(['train.py']),
    protected_names: names(['score.py']), operator_stages: names(['train', 'score']) },
  credential_policy: 'Supply only LOOPLAB_HARNESS_TOKEN separately. Remove LOOPLAB_UI_TOKEN.',
  scope: 'This token is not restricted to one run. Use a separate server/root.',
  recovery: 'Read receipts and checkpoints before resubmitting to the same run.',
}

test('initial connection render failure restores focus after its trigger disappears', async () => {
  const harness = await mountLive({ visible: true, plugins: [{
    name: 'doc72-initial-connection-failure', enforce: 'pre', transform(code, id) {
      if (id.replaceAll('\\', '/').endsWith('/src/HarnessHandoff.jsx')) return {
        code: code.replace('const [language] = useAssistantLanguage()',
          `throw new Error('doc72 injected initial connection render failure');
           const [language] = useAssistantLanguage()`), map: null }
    },
  }] })
  const originalError = console.error
  console.error = (...args) => {
    const message = args.map(String).join(' ')
    if (!message.includes('doc72 injected') && !message.includes('The above error occurred')) originalError(...args)
  }
  let view
  try {
    localStorage.clear()
    const { default: Connection } = await harness.load('/src/HarnessConnection.jsx')
    const { default: Panel } = await harness.load('/src/PanelShell.jsx')
    const backend = fetchStub({})
    globalThis.fetch = backend
    function Dialog() {
      return React.createElement(Panel, { title: 'Agent cycle', onClose: () => {} },
        React.createElement(Connection, { runId: 'demo', generation, seq: 12 }),
        React.createElement('button', null, 'Read history'))
    }
    view = await harness.mount(Dialog)
    const connect = [...view.container.querySelectorAll('button')].find(row => row.textContent === 'Connect external agent')
    connect.focus(); await click(connect)
    await until(() => view.container.querySelector('[role="alert"]'), 'initial connection failure')
    await settle()
    const reload = view.container.querySelector('[role="alert"] button')
    assert.ok(document.activeElement === reload, 'lost focus must move to the surviving recovery action')
    assert.equal(backend.calls.length, 0, 'a render failure must not execute a recovery read or command')
  } finally {
    await view?.unmount(); console.error = originalError; await harness.close()
  }
})

for (const [target, failure] of [['HarnessCycleBody', 'import'], ['HarnessCycleBody', 'render'],
  ['HarnessHandoff', 'render'], ['HarnessReceipt', 'render']]) {
  test(`${target} ${failure} failure preserves surrounding controls, focus and explicit closing`, async () => {
    const bodyFailure = target === 'HarnessCycleBody'
    let release
    globalThis.__looplabCycleFailure = { promise: new Promise(resolve => { release = resolve }), fail: false }
    const harness = await mountLive({ visible: true, plugins: [{
      name: 'doc72-cycle-body-failure', enforce: 'pre',
      transform(code, id) {
        if (!id.replaceAll('\\', '/').endsWith(`/src/${target}.jsx`)) return
        return { code: failure === 'import'
          ? `await globalThis.__looplabCycleFailure.promise;
              throw new Error('doc72 injected cycle import failure'); export default function Body() { return null }`
          : code.replace('const [language] = useAssistantLanguage()',
            `if (globalThis.__looplabCycleFailure.fail) throw new Error('doc72 injected cycle render failure');
             const [language] = useAssistantLanguage()`), map: null }
      },
    }] })
    const originalError = console.error, errors = []
    console.error = (...args) => {
      const message = args.map(String).join(' ')
      if (message.includes('doc72 injected') || message.includes('The above error occurred')) errors.push(message)
      else originalError(...args)
    }
    let view
    try {
      localStorage.clear()
      const { HarnessProgressPanel } = await harness.load('/src/HarnessProgressPanel.jsx')
      const { default: Boundary } = await harness.load('/src/LazyBoundary.jsx')
      let eventSeq = 12
      const backend = fetchStub({ '/api/runs/demo/harness-progress': ({ url }) =>
        ({ ...progress(url.searchParams.get('language')), event_seq: eventSeq }),
      '/api/runs/demo/harness-handoff': () => ({ ...handoff, event_seq: eventSeq }) })
      globalThis.fetch = backend
      function Workspace({ seq = 12 }) {
        const [open, setOpen] = React.useState(false)
        return React.createElement(React.Fragment, null,
          React.createElement('button', { onClick: () => setOpen(true) }, 'Open cycle'),
          React.createElement('textarea', { 'aria-label': 'Saved draft', defaultValue: 'Keep my next question' }),
          open && React.createElement(Boundary, { label: 'agent panel', mode: 'overlay', resetKey: `demo:${generation}`,
            onClose: () => setOpen(false) }, React.createElement(HarnessProgressPanel,
            { runId: 'demo', expectedGeneration: generation, seq, externalMode: true, configStatus: 'ready',
              onClose: () => setOpen(false), onOpenEvents: () => {} })))
      }
      view = await harness.mount(Workspace)
      const button = name => [...view.container.querySelectorAll('button')].find(row => row.textContent === name)
      const opener = button('Open cycle')
      opener.focus(); await click(opener)
      await until(() => button('Connect external agent'), 'dialog shell')
      const dialog = view.container.querySelector('[role="dialog"]')
      const connect = button('Connect external agent')
      if (failure === 'render') await until(() => view.container.textContent.includes('Before another candidate'), 'real cycle body')
      else await until(() => view.container.textContent.includes('Loading requirements and history'), 'pending body')
      let clientPicker
      if (!bodyFailure) {
        await click(connect)
        await until(() => view.container.textContent.includes('C:/Runs/demo'), 'real connection help')
        clientPicker = view.container.querySelector('.harness-handoff select')
        await React.act(async () => {
          clientPicker.value = 'claude'; clientPicker.dispatchEvent(new Event('change', { bubbles: true }))
        })
        assert.match(view.container.textContent, /Pending approval means/)
      }
      const focusTarget = bodyFailure ? connect : button('Open events')
      focusTarget.focus()
      await React.act(async () => {
        globalThis.__looplabCycleFailure.fail = true
        release()
      })
      if (failure === 'render') {
        if (target !== 'HarnessReceipt') eventSeq = 13
        await view.rerender({ seq: eventSeq })
      }
      await until(() => view.container.querySelector('[role="alert"], [role="alertdialog"]'), 'body failure')
      await settle()
      assert.equal(view.container.querySelector('[role="dialog"]'), dialog, 'a body failure must retain the original modal')
      if (bodyFailure) assert.equal(button('Connect external agent'), connect)
      assert.equal(document.activeElement, focusTarget, 'late failure must retain focus in surviving controls')
      assert.equal(view.container.querySelector('textarea').value, 'Keep my next question')
      if (bodyFailure) {
        assert.doesNotMatch(view.container.textContent, /No current external-cycle gate|No knowledge reviews due/)
        assert.match(view.container.textContent, /Requirements and history/)
      } else {
        assert.match(view.container.textContent, /Before another candidate/)
        assert.match(view.container.textContent, /Effective concept tags are required/)
        assert.ok(button('Open events'), 'measured history remains reachable')
        if (target === 'HarnessReceipt') {
          assert.equal(view.container.querySelector('.harness-handoff select'), clientPicker)
          assert.equal(clientPicker.value, 'claude', 'a recovery failure must retain client selection')
          assert.ok(button('Copy agent instruction'), 'verified connection instructions remain available')
          assert.equal(button('Read saved receipt'), undefined, 'failed recovery form does not claim a receipt')
        } else assert.equal(button('Copy agent instruction'), undefined, 'failed handoff offers no unverified instruction')
      }
      const errorsAfterFailure = errors.length
      await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'ru' })))
      await until(() => view.container.textContent.includes('Перезагрузить LoopLab'), 'Russian failure')
      await settle()
      assert.equal(errors.length, errorsAfterFailure, 'locale and read updates must not retry the failed reader')
      assert.equal(view.container.querySelector('[role="dialog"]'), dialog)
      if (bodyFailure) {
        await click(button('Подключить внешнего агента'))
        await until(() => view.container.textContent.includes('C:/Runs/demo'), 'independent connection instructions')
        assert.ok(view.container.querySelector('.harness-handoff').open)
      }
      assert.equal(backend.calls.filter(row => row.path.endsWith('/harness-handoff')).length, 1)
      assert.ok(backend.calls.every(row => row.method === 'GET'), 'opening recovery help executes no commands')
      await click(view.container.querySelector('.panel-close'))
      assert.equal(view.container.querySelector('[aria-modal="true"]'), null)
      assert.equal(document.activeElement, opener, 'Close returns to the original opener')
      await click(opener)
      if (!bodyFailure) {
        await until(() => button('Connect external agent'), 'reopened connection entry')
        await click(button('Connect external agent'))
      }
      await until(() => view.container.querySelector('[role="alert"]'), 'reopened failure')
      await React.act(async () => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
      assert.equal(view.container.querySelector('[aria-modal="true"]'), null)
      assert.equal(document.activeElement, opener, 'Escape returns to the original opener')
      assert.equal(view.container.querySelector('textarea').value, 'Keep my next question')
    } finally {
      release(); await view?.unmount(); console.error = originalError
      await harness.close(); delete globalThis.__looplabCycleFailure
    }
  })
}
