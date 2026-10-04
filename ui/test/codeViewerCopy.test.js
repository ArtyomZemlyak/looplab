import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { mountLive, click } from './_mount.js'

async function setup(writeText) {
  const h = await mountLive({ visible: true })
  const previous = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const { default: CodeViewer } = await h.load('/src/CodeViewer.jsx')
  return { h, CodeViewer, close: async () => {
    if (previous) Object.defineProperty(navigator, 'clipboard', previous)
    else delete navigator.clipboard
    await h.close()
  } }
}

const button = (mounted, name) => [...mounted.container.querySelectorAll('button')]
  .find(item => item.textContent === name)

test('copy acknowledgment is scoped to the source, label and exact copied text, including diff payloads', async () => {
  const writes = []
  const { h, CodeViewer, close } = await setup(async text => writes.push(text))
  try {
    let props = { code: 'initial\n', label: 'Base', draftScope: 'base' }
    const mounted = await h.mount(CodeViewer, props)
    for (const change of [{ code: 'new\n' }, { label: 'Edit' }, { draftScope: 'other-node' },
      { copyText: 'complete file\r\n', diff: [{ line: 'diff preview', kind: 'add' }] }, { copyText: '' }]) {
      await click(button(mounted, 'Copy'))
      assert.ok(button(mounted, 'Copied'))
      props = { ...props, ...change }
      await mounted.rerender(props)
      assert.ok(button(mounted, 'Copy'), 'changed source has no prior copy acknowledgment')
      assert.equal(button(mounted, 'Copied'), undefined)
    }
    await click(button(mounted, 'Copy'))
    assert.equal(writes.at(-1), '', 'an empty copyText is not replaced with code or diff text')
    await mounted.rerender({ ...props, allowCopy: false })
    assert.equal(button(mounted, 'Copy') || button(mounted, 'Copied'), undefined)
    await mounted.rerender({ ...props, allowCopy: true })
    assert.ok(button(mounted, 'Copy'), 're-enabling Copy starts without an old acknowledgment')
  } finally { await close() }
})

test('out-of-order clipboard success and failure cannot overwrite the latest request feedback', async () => {
  const pending = []
  const { h, CodeViewer, close } = await setup(text => new Promise((resolve, reject) => {
    pending.push({ text, resolve, reject })
  }))
  try {
    const mounted = await h.mount(CodeViewer, { code: 'current source\n', label: 'Source' })
    await click(button(mounted, 'Copy'))
    await click(button(mounted, 'Copy'))
    await React.act(async () => { pending[1].resolve() })
    assert.ok(button(mounted, 'Copied'))
    await React.act(async () => { pending[0].reject(new Error('old denied request')) })
    assert.ok(button(mounted, 'Copied'), 'old failure must not clear a newer success')
    await click(button(mounted, 'Copied'))
    await click(button(mounted, 'Copy'))
    await React.act(async () => { pending[3].reject(new Error('latest denied request')) })
    assert.ok(button(mounted, 'Copy'))
    await React.act(async () => { pending[2].resolve() })
    assert.ok(button(mounted, 'Copy'), 'old success must not acknowledge a newer failed request')
    assert.equal(button(mounted, 'Copied'), undefined)
    assert.deepEqual(pending.map(item => item.text), Array(4).fill('current source\n'))
  } finally { await close() }
})

test('a previous copy timeout cannot prematurely clear a later copy acknowledgment', async t => {
  const { h, CodeViewer, close } = await setup(async () => {})
  try {
    const mounted = await h.mount(CodeViewer, { code: 'source\n' })
    t.mock.timers.enable({ apis: ['setTimeout'] })
    const tick = milliseconds => React.act(async () => { t.mock.timers.tick(milliseconds) })
    await click(button(mounted, 'Copy'))
    await tick(700)
    await click(button(mounted, 'Copied'))
    await tick(700)
    assert.ok(button(mounted, 'Copied'), 'first timeout cannot clear the second acknowledgment')
    await tick(699)
    assert.ok(button(mounted, 'Copied'))
    await tick(1)
    assert.ok(button(mounted, 'Copy'))
  } finally { t.mock.timers.reset(); await close() }
})
