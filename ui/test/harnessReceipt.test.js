import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { createHash } from 'node:crypto'
import { fetchStub, jsonResponse, mountLive, settle, until } from './_mount.js'

const generation = 'a'.repeat(64)
const receipt = { version: 1, generation, terminal: false,
  command: { id: 'cmd_' + createHash('sha256').update('original-key').digest('hex').slice(0, 32), event_type: 'inject_node', status: 'executing',
    event_seq: 4, error_code: '', retryable: false } }

test('recovery lookup is explicit, sends a key only in a header and hides failed refreshes', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { default: HarnessReceipt, validReceipt } = await harness.load('/src/HarnessReceipt.jsx')
    assert.ok(validReceipt(receipt, generation))
    assert.ok(!validReceipt({ ...receipt, terminal: true }, generation))
    assert.ok(!validReceipt(receipt, 'c'.repeat(64)))
    assert.ok(!validReceipt({ ...receipt, command: { ...receipt.command, event_type: '' } }, generation))
    assert.ok(!validReceipt({ ...receipt, command: { ...receipt.command, event_type: 'node_evaluated' } }, generation))
    assert.ok(!validReceipt({ ...receipt, generation: undefined }, undefined))
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
    globalThis.fetch = fetchStub({ '/api/runs/demo/command-receipt': {
      ...receipt, command: { ...receipt.command, id: `cmd_${'c'.repeat(32)}` },
    } })
    await submit()
    await settle()
    assert.ok(!view.container.querySelector('[aria-label="Saved command receipt"]'), 'a different command ID cannot answer the original key')
    assert.match(view.container.textContent, /Receipt unavailable or changed/)
    assert.equal(globalThis.fetch.calls.length, 1)
    // HTTP success is not a verdict. A domain outcome in place of a control
    // receipt must withdraw the last good result and wait for an explicit read.
    globalThis.fetch = fetchStub({ '/api/runs/demo/command-receipt': {
      ...receipt, terminal: true, command: { ...receipt.command, event_type: 'node_evaluated', status: 'succeeded' },
    } })
    await submit()
    await until(() => view.container.textContent.includes('Receipt unavailable or changed'), 'invalid HTTP 200')
    assert.ok(!view.container.querySelector('[aria-label="Saved command receipt"]'))
    assert.equal(input.value, 'original-key')
    await settle()
    assert.equal(globalThis.fetch.calls.length, 1, 'invalid payload must not retry itself')
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
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

test('without secure key hashing, RU/EN guidance preserves the key and permits explicit ID observation', async () => {
  const harness = await mountLive({ visible: true })
  const originalCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  try {
    localStorage.setItem('looplab.language', 'ru')
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: {} })
    const { default: HarnessReceipt } = await harness.load('/src/HarnessReceipt.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/demo/command-receipt': receipt })
    const view = await harness.mount(HarnessReceipt, { runId: 'demo', generation })
    const setInput = async value => React.act(async () => {
      const input = view.container.querySelector('input')
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value)
      input.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    const submit = async () => React.act(async () => {
      view.container.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }))
    })
    await setInput('original-key')
    await submit()
    await until(() => view.container.textContent.includes('Проверка ключа недоступна'), 'hash refusal')
    assert.equal(view.container.querySelector('input').value, 'original-key')
    assert.equal(globalThis.fetch.calls.length, 0)
    await React.act(async () => window.dispatchEvent(new CustomEvent('looplab:language', { detail: 'en' })))
    assert.match(view.container.textContent, /Key verification is unavailable.*original Command ID from Events/)
    assert.equal(view.container.querySelector('input').value, 'original-key')
    await settle()
    assert.equal(globalThis.fetch.calls.length, 0)
    await React.act(async () => {
      const select = view.container.querySelector('select')
      select.value = 'id'; select.dispatchEvent(new window.Event('change', { bubbles: true }))
    })
    await setInput(receipt.command.id)
    await submit()
    await until(() => view.container.textContent.includes('inject_node · executing'), 'explicit ID observation')
    assert.equal(globalThis.fetch.calls.length, 1)
    assert.ok(globalThis.fetch.calls.every(call => call.method === 'GET'))
  } finally {
    if (originalCrypto) Object.defineProperty(globalThis, 'crypto', originalCrypto)
    else delete globalThis.crypto
    localStorage.removeItem('looplab.language')
    await harness.close()
  }
})

test('changing the run or generation clears recovery input and never reuses its original lookup', async () => {
  const harness = await mountLive({ visible: true })
  try {
    const { default: HarnessReceipt } = await harness.load('/src/HarnessReceipt.jsx')
    let release
    const late = new Promise(resolve => { release = resolve })
    globalThis.fetch = fetchStub({
      '/api/runs/demo/command-receipt': () => late,
      '/api/runs/other/command-receipt': receipt,
    })
    const view = await harness.mount(HarnessReceipt, { runId: 'demo', generation })
    await React.act(async () => {
      const input = view.container.querySelector('input')
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'original-key')
      input.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    await React.act(async () => {
      view.container.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }))
    })
    await until(() => globalThis.fetch.calls.length === 1, 'original lookup')
    await view.rerender({ runId: 'other', generation })
    assert.equal(view.container.querySelector('input').value, '', 'key must not cross runs')
    await React.act(async () => { release(receipt) })
    await settle()
    assert.equal(globalThis.fetch.calls.length, 1, 'context change is not an explicit read')
    assert.ok(!view.container.querySelector('[aria-label="Saved command receipt"]'))
    await React.act(async () => {
      const select = view.container.querySelector('select')
      select.value = 'id'; select.dispatchEvent(new window.Event('change', { bubbles: true }))
    })
    await React.act(async () => {
      const input = view.container.querySelector('input')
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, receipt.command.id)
      input.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    await view.rerender({ runId: 'other', generation: 'd'.repeat(64) })
    assert.equal(view.container.querySelector('input').value, '')
    assert.equal(view.container.querySelector('select').value, 'key')
    await settle()
    assert.equal(globalThis.fetch.calls.length, 1)
  } finally { await harness.close() }
})

test('a hash completed after changing generation cannot send the abandoned key', async () => {
  const harness = await mountLive({ visible: true })
  const originalCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  let release
  try {
    let hashing = false
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { subtle: {
      digest: () => { hashing = true; return new Promise(resolve => { release = resolve }) },
    } } })
    const { default: HarnessReceipt } = await harness.load('/src/HarnessReceipt.jsx')
    globalThis.fetch = fetchStub({ '/api/runs/demo/command-receipt': receipt })
    const view = await harness.mount(HarnessReceipt, { runId: 'demo', generation })
    await React.act(async () => {
      const input = view.container.querySelector('input')
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'original-key')
      input.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    await React.act(async () => {
      view.container.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }))
    })
    await until(() => hashing, 'pending hash')
    await view.rerender({ runId: 'demo', generation: 'd'.repeat(64) })
    await React.act(async () => { release(createHash('sha256').update('original-key').digest()) })
    await settle()
    assert.equal(globalThis.fetch.calls.length, 0)
    assert.equal(view.container.querySelector('input').value, '')
    assert.ok(!view.container.querySelector('[aria-label="Saved command receipt"]'))
  } finally {
    release?.(new Uint8Array(32))
    if (originalCrypto) Object.defineProperty(globalThis, 'crypto', originalCrypto)
    else delete globalThis.crypto
    await harness.close()
  }
})
