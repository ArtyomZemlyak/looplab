import test from 'node:test'
import assert from 'node:assert/strict'
import { click, fetchStub, jsonResponse, mountLive, settle, unanswered, until } from './_mount.js'
import { RAW_SETTINGS_SCHEMA, SETTINGS_SCHEMA } from './settingsSchemaFixture.js'

const validValue = field => field.type === 'bool' ? false : field.type === 'enum' ? field.options[0]
  : field.type === 'secret' ? null : field.type === 'int' ? field.minimum ?? (field.exclusiveMinimum ?? 0) + 1
    : field.type === 'float' ? field.minimum ?? (field.exclusiveMinimum ?? 0) + 0.5
      : field.type === 'list' ? [] : field.nullable ? null : ''
const defaults = Object.fromEntries(Object.values(SETTINGS_SCHEMA.fieldByKey)
  .map(field => [field.key, validValue(field)]))
const resource = { settings: defaults, defaults, overrides: {}, settings_revision: 'settings-r1',
  secret_revision: 'secret-r1', credential: { source: 'none', status: 'missing',
    stored: false, effective: false, active: false, clearable: false } }
const response = body => ({ ok: true, provider_attempted: true, outcome_unknown: false,
  operation_id: body.operation_id, settings_revision: body.expected_settings_revision,
  secret_revision: body.expected_secret_revision, effective_identity: `probe-v1:${'a'.repeat(64)}` })
let harness, Check, guard
test.before(async () => {
  harness = await mountLive()
  ;({ default: Check } = await harness.load('/src/AssistantModelCheck.jsx'))
  guard = await harness.load('/src/settingsLaunchGuard.js')
})
test.after(async () => { await harness?.close() })
const backend = health => fetchStub({
  'GET /api/settings': resource,
  'GET /api/settings/schema/2': { ...RAW_SETTINGS_SCHEMA, revision: '0'.repeat(64) },
  'POST /api/llm/health': health,
})
const providerButton = container => [...container.querySelectorAll('button')]
  .find(button => /Test active LLM|Check previous result/.test(button.textContent))

test('opening the inline check only reads saved settings; a double click starts one provider operation', async () => {
  let release
  const calls = backend(({ init }) => new Promise(resolve => {
    const body = JSON.parse(init.body)
    release = () => resolve(jsonResponse(response(body)))
  }))
  globalThis.fetch = calls
  const mounted = await harness.mount(Check)
  try {
    await until(() => providerButton(mounted.container)?.disabled === false, 'saved configuration to load')
    assert.equal(calls.calls.some(call => call.method === 'POST'), false)
    assert.match(mounted.container.textContent, /may be billed/)
    assert.equal(document.activeElement, mounted.container.querySelector('[aria-label="Model connection check"]'))
    const button = providerButton(mounted.container)
    await click(button)
    await click(button)
    assert.equal(calls.calls.filter(call => call.method === 'POST').length, 1)
    assert.equal(guard.getSnapshot().blocked, true)
    release()
    await until(() => mounted.container.textContent.includes('Active LLM responded'), 'the provider receipt')
    await settle()
    assert.equal(guard.getSnapshot().blocked, false)
  } finally { await mounted.unmount() }
})

test('leaving an in-flight check retains its recovery ID and remount only replays it', async () => {
  const calls = backend(({ init }) => {
    const body = JSON.parse(init.body)
    return body.replay_only ? jsonResponse(response(body)) : unanswered(init)
  })
  globalThis.fetch = calls
  let mounted = await harness.mount(Check)
  try {
    await until(() => providerButton(mounted.container)?.disabled === false, 'saved configuration')
    await click(providerButton(mounted.container))
    const first = calls.calls.find(call => call.method === 'POST')
    await mounted.unmount()
    assert.equal(guard.getSnapshot().blocked, true)
    mounted = await harness.mount(Check)
    await until(() => providerButton(mounted.container)?.textContent === 'Check previous result', 'previous operation recovery')
    assert.equal(calls.calls.filter(call => call.method === 'POST').length, 1, 'opening cannot retry the provider')
    await click(providerButton(mounted.container))
    await until(() => mounted.container.textContent.includes('Active LLM responded'), 'replayed receipt')
    const posts = calls.calls.filter(call => call.method === 'POST')
    assert.equal(posts.length, 2)
    assert.equal(JSON.parse(posts[1].body).operation_id, JSON.parse(first.body).operation_id)
    assert.equal(JSON.parse(posts[1].body).replay_only, true)
  } finally { await mounted.unmount() }
})

test('an incomplete saved configuration cannot enable a provider test', async () => {
  const calls = fetchStub({
    'GET /api/settings': { settings_revision: 'r', secret_revision: 's', settings: {} },
    'GET /api/settings/schema/2': { ...RAW_SETTINGS_SCHEMA, revision: '0'.repeat(64) },
  })
  globalThis.fetch = calls
  const mounted = await harness.mount(Check)
  try {
    await until(() => mounted.container.querySelector('[role="alert"]'), 'the read failure')
    assert.equal(providerButton(mounted.container).disabled, true)
    assert.equal(calls.calls.some(call => call.method === 'POST'), false)
  } finally { await mounted.unmount() }
})

test('a terminal unknown outcome blocks an ordinary new check and stays explicit', async () => {
  const calls = backend(({ init }) => ({ ...response(JSON.parse(init.body)), ok: false,
    outcome_unknown: true, code: 'provider_timeout' }))
  globalThis.fetch = calls
  const mounted = await harness.mount(Check)
  try {
    await until(() => providerButton(mounted.container)?.disabled === false, 'saved configuration')
    await click(providerButton(mounted.container))
    await until(() => mounted.container.textContent.includes('Provider outcome unresolved'), 'the unknown receipt')
    await settle()
    assert.equal(guard.getSnapshot().blocked, true)
    assert.ok([...mounted.container.querySelectorAll('button')]
      .some(button => button.textContent === 'Outcome unresolved' && button.disabled))
    assert.match(mounted.container.textContent, /Start new check \(may bill\)/)
    assert.equal(calls.calls.filter(call => call.method === 'POST').length, 1,
      'an unknown response cannot cause an automatic provider retry')
  } finally { await mounted.unmount() }
})
