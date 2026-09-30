import test from 'node:test'
import assert from 'node:assert/strict'
import { click, mountLive } from './_mount.js'

let harness
let AssistantModePicker
test.before(async () => {
  harness = await mountLive()
  ;({ default: AssistantModePicker } = await harness.load('/src/AssistantModePicker.jsx'))
})
test.after(async () => { await harness?.close() })

test('permission choices are disclosed deliberately and preserve the supplied mode', async () => {
  const changes = []
  const props = { mode: 'acceptEdits', onChange: value => changes.push(value) }
  const mounted = await harness.mount(AssistantModePicker, props)
  try {
    const picker = mounted.container.querySelector('details')
    const summary = picker.querySelector('summary')
    assert.equal(picker.open, false)
    assert.match(summary.textContent, /Permissions · Auto-edit/)
    assert.equal(picker.querySelector('[aria-pressed="true"]').textContent.startsWith('Auto-edit'), true)
    assert.deepEqual(changes, [], 'opening a composer must not change its permissions')
    await click(summary)
    assert.equal(picker.open, true)
    const options = [...picker.querySelectorAll('button')]
    assert.equal(options.length, 4)
    const ask = options.find(button => button.textContent.startsWith('Ask'))
    await click(ask)
    assert.deepEqual(changes, ['default'], 'selection passes the canonical server mode')
    assert.equal(picker.open, false)
    assert.equal(document.activeElement, summary, 'focus returns to the visible permission control')
    await mounted.rerender({ ...props, mode: 'default' })
    assert.match(summary.textContent, /Permissions · Ask/)
    assert.match(mounted.container.querySelector('#assistant-mode-hint').textContent, /for this turn/)
  } finally { await mounted.unmount() }
})

test('paused permission choices can be inspected without changing the mode', async () => {
  const changes = []
  const mounted = await harness.mount(AssistantModePicker, {
    mode: 'plan', disabled: true, disabledReason: 'Opening chat',
    onChange: value => changes.push(value),
  })
  try {
    const picker = mounted.container.querySelector('details')
    await click(picker.querySelector('summary'))
    assert.equal(picker.open, true)
    const options = [...picker.querySelectorAll('button')]
    assert.ok(options.every(button => button.disabled && button.title === 'Opening chat'))
    await click(options[3])
    assert.deepEqual(changes, [])
    assert.equal(picker.querySelector('[aria-pressed="true"]'), options[0])
  } finally { await mounted.unmount() }
})
