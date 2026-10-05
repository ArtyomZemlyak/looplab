import test from 'node:test'
import assert from 'node:assert/strict'
import { click, mountLive } from './_mount.js'
import { SETTINGS_SCHEMA } from './settingsSchemaFixture.js'
import { toForm } from '../src/settingsSchema.js'

let harness
let SettingsForm
test.before(async () => {
  harness = await mountLive()
  ;({ default: SettingsForm } = await harness.load('/src/SettingsForm.jsx'))
})
test.after(async () => { await harness?.close() })

test('model-first setup preserves values, permissions, and the selected section when All is opened', async () => {
  const changes = []
  const defaults = Object.fromEntries(Object.values(SETTINGS_SCHEMA.fieldByKey)
    .map(field => [field.key, field.default]))
  const props = { schema: SETTINGS_SCHEMA, form: toForm(defaults, SETTINGS_SCHEMA), mode: 'essential',
    onChange: (...args) => changes.push(args), onToggleAgent: (...args) => changes.push(args) }
  const mounted = await harness.mount(SettingsForm, props)
  try {
    const { container } = mounted
    const tabs = [...container.querySelectorAll('[role="tab"]')]
    assert.deepEqual(tabs.map(tab => tab.textContent), ['Model', 'Experiments & resources', 'Time & model budgets'])
    assert.equal(tabs[0].getAttribute('aria-selected'), 'true')
    assert.deepEqual([...container.querySelectorAll('[name]')].map(input => input.name),
      ['output_language', 'llm_model', 'llm_base_url', 'llm_api_key', 'backend'])
    assert.ok(container.querySelector('[name="llm_base_url"]'))
    assert.deepEqual(changes, [], 'opening setup does not change saved defaults')
    await click(tabs[1])
    const input = container.querySelector('[name="eval_parallel"]')
    assert.equal(input.value, '0')
    const field = input.closest('.sf-field')
    assert.match(document.getElementById(input.getAttribute('aria-describedby')).textContent, /0 = AUTO/)
    const disclosures = [...field.querySelectorAll('details')]
    assert.ok(disclosures.every(details => !details.open))
    assert.match(disclosures.find(details => details.textContent.includes('Technical details')).textContent,
      /Live Strategist\/operator updates settle 0 to serial width 1/)
    const permissions = disclosures.find(details => details.classList.contains('sf-runtime-access'))
    await click(permissions.querySelector('summary'))
    assert.equal(permissions.open, true)
    assert.deepEqual(changes, [], 'reading runtime permissions does not grant them')
    await click(permissions.querySelector('button'))
    assert.deepEqual(changes, [['eval_parallel', 'strategist']])
    await mounted.rerender({ ...props, mode: 'all', form: { ...props.form, eval_parallel: '2' } })
    assert.equal(container.querySelector('[role="tab"][aria-selected="true"]').textContent, 'Search & policy')
    assert.equal(container.querySelector('[name="eval_parallel"]').value, '2')
    assert.ok(container.querySelector('[name="policy"]'))
    assert.equal(container.querySelectorAll('.sf-runtime-access').length, 0)
  } finally { await mounted.unmount() }
})
