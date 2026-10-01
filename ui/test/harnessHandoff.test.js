import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { harnessAgentInstruction, harnessMcpDescriptor, harnessServerUrl, validHarnessHandoff } from '../src/harnessHandoff.js'
import { fetchStub, jsonResponse, mountLive, until } from './_mount.js'

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
