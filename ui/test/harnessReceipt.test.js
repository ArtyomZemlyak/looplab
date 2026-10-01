import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, jsonResponse, mountLive, until } from './_mount.js'

const generation = 'a'.repeat(64)
const receipt = { version: 1, generation, terminal: false,
  command: { id: `cmd_${'b'.repeat(32)}`, event_type: 'inject_node', status: 'executing',
    event_seq: 4, error_code: '', retryable: false } }

test('recovery lookup is explicit, sends a key only in a header and hides failed refreshes', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { default: HarnessReceipt, validReceipt } = await harness.load('/src/HarnessReceipt.jsx')
    assert.ok(validReceipt(receipt, generation))
    assert.ok(!validReceipt({ ...receipt, terminal: true }, generation))
    assert.ok(!validReceipt(receipt, 'c'.repeat(64)))
    const requests = []
    globalThis.fetch = fetchStub({ '/api/runs/demo/command-receipt': request => {
      requests.push(request)
      return { ...receipt, payload: 'must not render', idempotency_key_digest: 'private' }
    } })
    const view = await harness.mount(HarnessReceipt, { runId: 'demo', generation })
    const storedBefore = [JSON.stringify(localStorage), JSON.stringify(sessionStorage)]
    assert.equal(globalThis.fetch.calls.length, 0)
    await React.act(async () => { view.container.querySelector('summary').click() })
    const input = view.container.querySelector('input')
    const setInput = async value => React.act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value)
      input.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    await setInput('original-key')
    assert.equal(globalThis.fetch.calls.length, 0, 'typing must not send a request')
    const submit = async () => React.act(async () => {
      view.container.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }))
    })
    await submit()
    await until(() => view.container.textContent.includes('inject_node · executing'), 'saved receipt')
    assert.equal(requests.length, 1)
    assert.equal(requests[0].init.headers['Idempotency-Key'], 'original-key')
    assert.ok(!requests[0].url.href.includes('original-key'))
    assert.match(view.container.textContent, /Reading it did not continue the command/)
    assert.ok(!view.container.textContent.includes('must not render'))
    assert.ok(!view.container.textContent.includes('private'))
    globalThis.fetch = fetchStub({ '/api/runs/demo/command-receipt': () => jsonResponse({}, 409) })
    await submit()
    await until(() => view.container.textContent.includes('Receipt unavailable or changed'), 'failed refresh')
    assert.ok(!view.container.textContent.includes('inject_node · executing'))
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
    await setInput('another-key')
    assert.ok(!view.container.textContent.includes('Receipt unavailable or changed'), 'new identity withdraws old result')
    const select = view.container.querySelector('select')
    await React.act(async () => {
      select.value = 'id'; select.dispatchEvent(new window.Event('change', { bubbles: true }))
    })
    await setInput(receipt.command.id)
    globalThis.fetch = fetchStub({ '/api/runs/demo/command-receipt': request => {
      assert.equal(request.url.searchParams.get('command_id'), receipt.command.id)
      assert.ok(!Object.hasOwn(request.init.headers, 'Idempotency-Key'))
      return { ...receipt, terminal: true, command: { ...receipt.command, status: 'succeeded' } }
    } })
    await submit()
    await until(() => view.container.textContent.includes('inject_node · succeeded'), 'terminal receipt')
    assert.match(view.container.textContent, /does not prove an experiment evaluated/)
    assert.deepEqual([JSON.stringify(localStorage), JSON.stringify(sessionStorage)], storedBefore)
  } finally { await harness.close() }
})
