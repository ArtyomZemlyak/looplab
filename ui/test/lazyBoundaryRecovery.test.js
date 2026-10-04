import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { click, mountLive, until } from './_mount.js'

test('default load failure focuses recovery, locale retains the failure, new identity recovers', async () => {
  const harness = await mountLive({ visible: true })
  const { default: LazyBoundary } = await harness.load('/src/LazyBoundary.jsx')
  let attempts = 0
  let reloads = 0
  function Reader({ fail }) {
    attempts += 1
    if (fail) throw new Error('injected boundary render failure')
    return React.createElement('p', null, 'Recovered result')
  }
  function Surface({ fail = true, language = 'en', identity = 'first' }) {
    return React.createElement(LazyBoundary, { label: 'Result', language, resetKey: identity,
      onReload: () => { reloads += 1 } }, React.createElement(Reader, { fail }))
  }
  const originalError = console.error
  console.error = (...args) => {
    const message = args.map(String).join(' ')
    if (!message.includes('injected boundary render failure') &&
        !message.includes('The above error occurred')) originalError(...args)
  }
  let mounted
  try {
    mounted = await harness.mount(Surface, {})
    const reload = mounted.container.querySelector('button')
    await until(() => document.activeElement === reload, 'default recovery focus')
    assert.equal(reload.textContent, 'Reload LoopLab')
    const failedAttempts = attempts
    await mounted.rerender({ language: 'ru', fail: false })
    assert.match(mounted.container.textContent, /Перезагрузить LoopLab/)
    assert.equal(attempts, failedAttempts, 'locale is not an automatic retry')
    await click(reload)
    assert.equal(reloads, 1, 'recovery runs only after explicit click')
    await mounted.rerender({ identity: 'second', fail: false })
    assert.match(mounted.container.textContent, /Recovered result/)
    assert.equal(mounted.container.querySelector('[role="alert"]'), null)
  } finally {
    await mounted?.unmount()
    console.error = originalError
    await harness.close()
  }
})
