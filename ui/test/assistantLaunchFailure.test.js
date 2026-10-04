import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, fetchStub, jsonResponse, mountLive, settle, unanswered, until } from './_mount.js'

const sid = '0123456789abcdef'
const meta = { id: sid, title: 'Launch plan', mode: 'plan', updated: 1700000000,
  shared: false, share_count: 0, share_ids: [], live_share_ids: [],
  share_expires_at: null, share_live: false }
const spec = { proposal_id: 'p1', run_id: 'failure-demo',
  task: { kind: 'quadratic', goal: 'min (x-3)^2', direction: 'min' },
  settings: { backend: 'toy', max_nodes: 3, max_seconds: 30 } }
const messages = [{ role: 'user', content: '/new improve the baseline', turn_id: 'g0', mode: 'plan' },
  { role: 'assistant', content: 'This is the retained launch proposal.', turn_id: 'g0', proposals: [spec] }]
const preview = body => ({ ok: true, validation_token: 'checked-plan', warnings: [], preview: {
  run_id: body.run_id, source: 'inline', source_task_file: null, task: body.task,
  referenced_paths: [], settings: { backend: 'toy', llm_model: 'local-model', max_nodes: 3,
    n_seeds: 1, max_parallel: 1, parallel_build: 0, eval_parallel: 1, llm_parallel: 1,
    max_seconds: 30, max_eval_seconds: 10, llm_budget_usd: 0, llm_cost_limit: 0 },
} })

for (const target of ['FirstRunModelStatus', 'NewRunStarter', 'AssistantModelCheck', 'LaunchCard', 'LaunchGuidance']) {
  test(`${target} rejected import preserves chat; nested failures retain their parent controls`, async () => {
    let release
    globalThis.__looplabLaunchLoadFailure = { promise: new Promise(resolve => { release = resolve }) }
    const harness = await mountLive({ visible: true, plugins: [{
      name: 'doc72-launch-load-failure', enforce: 'pre',
      transform(_code, id) {
        if (id.replaceAll('\\', '/').endsWith(`/src/${target}.jsx`)) return {
          code: `await globalThis.__looplabLaunchLoadFailure.promise;
            throw new Error('doc72 injected ${target} import failure');
            export default function UnavailableLaunch() { return null }`, map: null,
        }
      },
    }] })
    const originalError = console.error
    const expectedErrors = []
    console.error = (...args) => {
      const message = args.map(String).join(' ')
      if (message.includes('doc72 injected') || message.includes('The above error occurred')) expectedErrors.push(message)
      else originalError(...args)
    }
    let mounted
    try {
      localStorage.clear(); sessionStorage.clear()
      const proposal = target === 'LaunchCard' || target === 'LaunchGuidance'
      if (proposal) localStorage.setItem('ll.asstSid', sid)
      const { default: Bar } = await harness.load('/src/AssistantBar.jsx')
      const { default: Workspace } = await harness.load('/src/OwnerWorkspace.jsx')
      const statusKeys = []
      const backend = fetchStub({
        'GET /api/assistant/commands': { commands: [] },
        'GET /api/assistant/sessions': { sessions: proposal ? [meta] : [] },
        [`GET /api/assistant/sessions/${sid}`]: { messages, meta },
        'GET /api/assistant/watches': { watches: [] }, 'GET /api/runs': [],
        'GET /api/settings': { settings: { llm_model: 'local-model', llm_base_url: 'http://localhost/v1' },
          settings_revision: 'one', secret_revision: 'one' },
        'GET /api/assistant/permissions': ({ init }) => unanswered(init),
        'GET /api/assistant/progress': ({ init }) => unanswered(init),
        'POST /api/start/preflight': ({ init }) => preview(JSON.parse(init.body)),
        'POST /api/start': () => jsonResponse({ message: 'reply lost' }, 503),
        'GET /api/start/failure-demo/status': ({ init }) => {
          statusKeys.push(new Headers(init.headers).get('Idempotency-Key'))
          return { run_id: 'failure-demo', ok: false, status: 'uncertain',
            started: false, can_retry: false, paid_effect_unknown: true }
        },
      })
      globalThis.fetch = backend
      mounted = await harness.mount(Workspace, { route: { view: 'list' },
        children: React.createElement('main', null, 'Runs workspace'),
        AssistantComponent: Bar, AttentionComponent: () => null })
      const { container } = mounted
      const button = name => [...container.querySelectorAll('button')].find(row => row.textContent === name)
      const side = container.querySelector('button.cmdbar-drawer-btn')
      if (side) await click(side)
      if (proposal) await until(() => container.textContent.includes(messages[1].content), 'saved proposal transcript')
      else {
        await until(() => button('Start a new run'), 'first-run entry')
        if (target === 'AssistantModelCheck') {
          await until(() => button('Check connection…'), 'connection check disclosure')
          await click(button('Check connection…'))
        }
        await click(button('Start a new run'))
      }
      let form, start, startupKey
      if (target === 'LaunchGuidance') {
        await until(() => button('Validate — free')?.disabled === false, 'hydrated proposal')
        form = container.querySelector('form.asst-launch')
        await click(button('Validate — free'))
        await until(() => button('Start run')?.disabled === false, 'authoritatively checked plan')
        start = button('Start run')
      }
      const input = container.querySelector('[aria-label="Assistant message"]')
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
          .set.call(input, 'Keep my next question')
        input.dispatchEvent(new Event('input', { bubbles: true })); input.focus()
      })
      await React.act(async () => release())
      await until(() => container.querySelector('[role="alert"]'), 'local import failure')
      await settle()
      assert.ok(container.contains(input), 'the composer survives the optional import failure')
      assert.equal(input.value, 'Keep my next question')
      assert.equal(document.activeElement, input, 'late failure must retain draft focus')
      const errorsAfterFailure = expectedErrors.length
      assert.doesNotMatch(container.textContent, /Assistant could not be opened/)
      assert.match(container.textContent, /Runs workspace/)
      if (proposal) assert.match(container.textContent, /This is the retained launch proposal/)
      if (target === 'AssistantModelCheck') {
        assert.ok(container.querySelector('.asst-model-status'), 'saved model status survives')
        assert.ok(button('Model settings'), 'settings remain reachable on check failure')
      }
      if (target === 'LaunchGuidance') {
        assert.equal(container.querySelector('form.asst-launch'), form, 'guidance failure must not remount the checked plan')
        assert.equal(button('Start run'), start)
        assert.equal(start.disabled, false, 'the validation receipt is retained')
        await click(start)
        const recovery = () => [...form.querySelectorAll('button')].find(row => row.textContent === 'Check startup')
        await until(() => recovery()?.disabled === false, 'original startup recovery survives guidance failure')
        startupKey = JSON.parse(backend.calls.find(row => row.path === '/api/start').body).idempotency_key
        await click(recovery())
        await until(() => backend.calls.some(row => row.path.endsWith('/status')), 'read original startup')
        await settle()
        assert.deepEqual(statusKeys, [startupKey], 'status reads the original startup identity')
        assert.equal(backend.calls.filter(row => row.path === '/api/start').length, 1)
        assert.equal(button('Start run'), undefined, 'unknown startup never permits another start')
        assert.ok(form.querySelector('.asst-launch-progress').textContent.includes('startup') ||
          form.querySelector('.asst-launch-progress').textContent.includes('Startup'), 'current technical status remains readable')
      }
      await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'ru' })))
      await until(() => container.querySelector('[role="alert"]')?.textContent.includes('Перезагрузить LoopLab'), 'Russian local error')
      await settle()
      assert.equal(expectedErrors.length, errorsAfterFailure, 'phase and locale updates cannot retry the failed reader')
      assert.equal(input.value, 'Keep my next question')
      if (target !== 'LaunchGuidance') assert.equal(backend.calls.some(row => row.method !== 'GET'), false)
      assert.ok(expectedErrors.some(message => message.includes('doc72 injected')))
    } finally {
      release(); await mounted?.unmount(); console.error = originalError
      await harness.close(); delete globalThis.__looplabLaunchLoadFailure
    }
  })
}
