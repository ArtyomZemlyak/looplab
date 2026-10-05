import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { harnessAgentInstruction, harnessMcpDescriptor, harnessServerUrl, validHarnessHandoff } from '../src/harnessHandoff.js'
import { fetchStub, jsonResponse, mountLive, until } from './_mount.js'
import { harnessText } from '../src/harnessText.js'

const generation = 'a'.repeat(64)
const names = items => ({ items, total: items.length, truncated: false })
const handoff = {
  version: 1, generation, run_id: 'demo', run_uid: 'incarnation', event_seq: 12,
  mode: 'external_harness', engine_running: false, agent_connection: 'not_measured',
  credential_configured: true, server_paths: { run_root: 'C:/Runs', run_dir: 'C:/Runs/demo' },
  workspace: { kind: 'repository', source_paths: names(['C:/Project with spaces']),
    edit_surface: names(['train.py']), protected_names: names(['score.py']), operator_stages: names(['train', 'score']) },
  credential_policy: 'Supply only LOOPLAB_HARNESS_TOKEN separately. Remove LOOPLAB_UI_TOKEN.',
  scope: 'This token is not restricted to one run. Use a separate server/root.',
  recovery: 'Read receipts and checkpoints before resubmitting to the same run.',
}

test('Russian handoff preserves permission boundaries, recovery identities and unknown server policies', () => {
  const instruction = harnessAgentInstruction(handoff, 'http://localhost:8775/', 'ru')
  assert.match(instruction, /^Продолжай этот существующий/)
  assert.match(instruction, /connection_check.*generation_at_handoff/)
  assert.match(instruction, /command_receipt/)
  assert.match(instruction, /upstream_request.*expected_request_hash/)
  assert.match(instruction, /expected_content_hash/)
  assert.match(instruction, /Исходная generation внутри тела не заменяется текущей/)
  assert.match(instruction, /diagnostic_only.*не разрешают/)
  assert.match(instruction, /до 700 символов/)
  assert.match(instruction, /score.py/)
  assert.match(instruction, /This token is not restricted to one run/)
  assert.match(harnessAgentInstruction(handoff, 'http://localhost/', 'en'), /upstream_request/)
  assert.equal(harnessText('ru', 'Unknown future restriction'), 'Unknown future restriction')
})

test('language change updates copied feedback and receipt form without losing the recovery identity', async () => {
  const harness = await mountLive({ visible: true })
  const writes = []
  const previous = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => writes.push(value) } })
  try {
    localStorage.setItem('looplab.language', 'ru')
    const { default: HarnessHandoff } = await harness.load('/src/HarnessHandoff.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-handoff': handoff })
    const view = await harness.mount(HarnessHandoff, { runId: 'demo', generation, seq: 12 })
    await React.act(async () => { view.container.querySelector('summary').click() })
    await until(() => view.container.textContent.includes('C:/Runs/demo'), 'Russian handoff')
    assert.match(view.container.textContent, /Подключить внешнего агента/)
    assert.match(view.container.textContent, /подключение агента не измеряется/)
    assert.match(view.container.textContent, /Статус Connected подтверждает только процесс stdio/)
    assert.match(view.container.textContent, /Продолжить работу после потери/)
    assert.doesNotMatch(view.container.textContent, /Reconnect or recover|Copy agent instruction/)
    await React.act(async () => {
      [...view.container.querySelectorAll('button')].find(b => b.textContent === 'Скопировать инструкцию агенту').click()
      const input = view.container.querySelector('.harness-recovery input')
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'original-key')
      input.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    assert.match(writes[0], /^Продолжай/)
    assert.match(view.container.textContent, /Инструкция скопирована/)
    await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'en' })))
    assert.match(view.container.textContent, /Agent instruction copied/)
    assert.equal(view.container.querySelector('.harness-recovery input').value, 'original-key')
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally {
    localStorage.removeItem('looplab.language')
    if (previous) Object.defineProperty(navigator, 'clipboard', previous)
    else delete navigator.clipboard
    await harness.close()
  }
})

test('connection help loads after an explicit click and opens with one fenced read', async () => {
  const harness = await mountLive({ visible: true })
  try {
    localStorage.setItem('looplab.language', 'ru')
    const { default: HarnessConnection } = await harness.load('/src/HarnessConnection.jsx')
    const reads = []
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-handoff': request => {
      reads.push(request.url)
      return handoff
    } })
    const view = await harness.mount(HarnessConnection, { runId: 'demo', generation, seq: 12 })
    assert.equal(globalThis.fetch.calls.length, 0)
    assert.equal(view.container.querySelector('button').textContent, 'Подключить внешнего агента')
    await React.act(async () => {
      view.container.querySelector('button').focus()
      view.container.querySelector('button').click()
    })
    await until(() => view.container.textContent.includes('C:/Runs/demo'), 'loaded connection help')
    assert.ok(view.container.querySelector('.harness-handoff').open)
    assert.ok(document.activeElement === view.container.querySelector('.harness-handoff > summary'),
      'the newly opened instruction inherits focus from the removed connection trigger')
    assert.equal(globalThis.fetch.calls.length, 1)
    assert.equal(reads[0].searchParams.get('expected_generation'), generation)
    assert.match(view.container.textContent, /Продолжить работу после потери/)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally {
    localStorage.removeItem('looplab.language')
    await harness.close()
  }
})

test('delayed connection import preserves focus moved to another control and reads only on request', async () => {
  let release
  globalThis.__looplabConnectionFocus = new Promise(resolve => { release = resolve })
  const harness = await mountLive({ visible: true, plugins: [{
    name: 'doc72-delayed-connection-import', enforce: 'pre', transform(code, id) {
      if (id.replaceAll('\\', '/').endsWith('/src/HarnessHandoff.jsx')) return {
        code: `await globalThis.__looplabConnectionFocus;\n${code}`, map: null }
    },
  }] })
  let view
  try {
    localStorage.clear()
    const { default: Connection } = await harness.load('/src/HarnessConnection.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-handoff': handoff })
    function Surface() {
      return React.createElement(React.Fragment, null,
        React.createElement(Connection, { runId: 'demo', generation, seq: 12 }),
        React.createElement('input', { 'aria-label': 'Next question', defaultValue: 'Keep this question' }))
    }
    view = await harness.mount(Surface)
    const trigger = view.container.querySelector('button')
    trigger.focus()
    await React.act(async () => trigger.click())
    await until(() => view.container.textContent.includes('Loading connection help'), 'pending import')
    assert.equal(globalThis.fetch.calls.length, 0)
    const next = view.container.querySelector('input')
    await React.act(async () => { next.focus(); release() })
    await until(() => view.container.textContent.includes('C:/Runs/demo'), 'delayed connection help')
    assert.ok(document.activeElement === next, 'late import must not take focus from another control')
    assert.equal(next.value, 'Keep this question')
    assert.ok(view.container.querySelector('.harness-handoff').open)
    assert.equal(globalThis.fetch.calls.length, 1)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally {
    release(); await view?.unmount(); await harness.close(); delete globalThis.__looplabConnectionFocus
  }
})

test('handoff strips URL credentials/query/fragment, retains proxy and excludes unknown response fields', () => {
  const href = 'https://owner:private@host/user/u/proxy/8765/index.html?token=private#/run/demo'
  assert.equal(harnessServerUrl(href), 'https://host/user/u/proxy/8765')
  assert.ok(validHarnessHandoff(handoff, 'demo', generation))
  assert.ok(!validHarnessHandoff(handoff, 'other', generation))
  assert.ok(!validHarnessHandoff(handoff, 'demo', 'b'.repeat(64)))
  assert.ok(!validHarnessHandoff({ ...handoff, credential_configured: 'yes' }, 'demo', generation))
  const poisoned = { ...handoff, token: 'private', server_paths: { ...handoff.server_paths, token: 'private' },
    workspace: { ...handoff.workspace, edit_surface: { ...handoff.workspace.edit_surface, token: 'private' } } }
  const instruction = harnessAgentInstruction(poisoned, href)
  assert.ok(!instruction.includes('private'))
  assert.match(instruction, /score.py/)
  assert.match(instruction, /CURRENT generation/)
  assert.match(instruction, /connection_check.*generation_at_handoff/)
  assert.match(instruction, /command receipts|receipts and checkpoints/)
  const descriptor = JSON.parse(harnessMcpDescriptor(href))
  assert.deepEqual(descriptor, { command: 'looplab', args: ['harness-mcp'],
    env: { LOOPLAB_HARNESS_URL: 'https://host/user/u/proxy/8765' } })
  const codex = harnessMcpDescriptor(href, 'codex')
  assert.match(codex, /\[mcp_servers.looplab\]/)
  assert.match(codex, /env_vars = \["LOOPLAB_HARNESS_TOKEN"\]/)
  assert.doesNotMatch(codex, /private|LOOPLAB_UI_TOKEN/)
  const claude = JSON.parse(harnessMcpDescriptor(href, 'claude')).mcpServers.looplab
  assert.equal(claude.env.LOOPLAB_HARNESS_TOKEN, '${LOOPLAB_HARNESS_TOKEN:-}')
  assert.equal(claude.env.LOOPLAB_HARNESS_URL, 'https://host/user/u/proxy/8765')
  assert.throws(() => harnessMcpDescriptor(href, 'unknown'))
})

test('handoff opens on demand, copies verified context and withdraws stale context after refresh failure', async () => {
  const harness = await mountLive({ visible: true })
  const writes = []
  const previous = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => writes.push(text) } })
  try {
    const { default: HarnessHandoff } = await harness.load('/src/HarnessHandoff.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-handoff': handoff })
    const props = { runId: 'demo', generation, seq: 12 }
    const view = await harness.mount(HarnessHandoff, props)
    assert.equal(globalThis.fetch.calls.length, 0)
    await React.act(async () => { view.container.querySelector('summary').click() })
    await until(() => view.container.textContent.includes('C:/Runs/demo'), 'verified handoff')
    assert.match(view.container.textContent, /Stopped at last read/)
    assert.match(view.container.textContent, /agent connection is not measured/)
    await React.act(async () => {
      [...view.container.querySelectorAll('button')].find(b => b.textContent === 'Copy MCP configuration').click()
    })
    assert.match(writes[0], /env_vars = \["LOOPLAB_HARNESS_TOKEN"\]/)
    const picker = view.container.querySelector('select')
    await React.act(async () => {
      picker.value = 'claude'; picker.dispatchEvent(new window.Event('change', { bubbles: true }))
    })
    assert.match(view.container.querySelector('pre').textContent, /mcpServers/)
    assert.match(view.container.textContent, /Pending approval means/)
    assert.match(view.container.textContent, /Connected confirms the stdio process only/)
    assert.match(view.container.textContent, /Tool calls can still need client approval/)
    assert.doesNotMatch(view.container.textContent, /MCP configuration copied/)
    await React.act(async () => {
      [...view.container.querySelectorAll('button')].find(b => b.textContent === 'Copy agent instruction').click()
    })
    assert.equal(writes.length, 2)
    assert.match(writes[1], /harness-contract/)
    assert.match(writes[1], /connection_check/)
    assert.match(writes[1], /permission_denials/)
    assert.match(writes[1], /isError\/is_error/)
    assert.match(view.container.textContent, /instruction copied/)
    const identity = view.container.querySelector('.harness-recovery input')
    await React.act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(identity, 'original-key')
      identity.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-handoff': () => jsonResponse({}, 409) })
    await view.rerender({ ...props, seq: 13 })
    await until(() => view.container.textContent.includes('Connection context unavailable'), 'stale handoff')
    assert.ok(!view.container.textContent.includes('Copy agent instruction'))
    assert.ok(!view.container.textContent.includes('C:/Runs/demo'))
    assert.equal(view.container.querySelector('.harness-recovery input').value, 'original-key',
      'an event/context refresh must not lose the recovery identity while the operator types')
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally {
    if (previous) Object.defineProperty(navigator, 'clipboard', previous)
    else delete navigator.clipboard
    await harness.close()
  }
})

test('operator sees missing credential setup and clipboard fallback without an automatic write', async () => {
  const harness = await mountLive({ visible: true })
  const previous = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('denied') } } })
  try {
    const { default: HarnessHandoff } = await harness.load('/src/HarnessHandoff.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/demo/harness-handoff': { ...handoff, credential_configured: false } })
    const view = await harness.mount(HarnessHandoff, { runId: 'demo', generation, seq: 12 })
    await React.act(async () => { view.container.querySelector('summary').click() })
    await until(() => view.container.textContent.includes('Operator setup required'), 'credential guidance')
    await React.act(async () => {
      [...view.container.querySelectorAll('button')].find(b => b.textContent === 'Copy agent instruction').click()
    })
    assert.match(view.container.textContent, /Clipboard unavailable/)
    assert.match(view.container.textContent, /Preview instruction and workspace permissions/)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally {
    if (previous) Object.defineProperty(navigator, 'clipboard', previous)
    else delete navigator.clipboard
    await harness.close()
  }
})
