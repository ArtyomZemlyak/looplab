import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import { usePoll } from '../src/hooks.js'

const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

const flush = async () => {
  await Promise.resolve()
  await Promise.resolve()
}

test('usePoll serializes interval ticks and collapses overlap into one catch-up read', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const timers = new Map()
  let nextTimer = 1
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    IS_REACT_ACT_ENVIRONMENT: true,
    setInterval: callback => {
      const id = nextTimer++
      timers.set(id, callback)
      return id
    },
    clearInterval: id => timers.delete(id),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }

  let root
  try {
    const { createRoot } = await import('react-dom/client')
    const requests = []
    const Harness = ({ scope }) => {
      usePoll(alive => {
        const request = deferred()
        requests.push({ scope, alive, ...request })
        return request.promise
      }, 1000, [scope])
      return null
    }
    root = createRoot(document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness, { scope: 'a' })) })
    assert.equal(requests.length, 1)
    const interval = [...timers.values()][0]

    await React.act(async () => {
      interval()
      interval()
      interval()
      await flush()
    })
    assert.equal(requests.length, 1, 'an unsettled read must own the single flight')

    await React.act(async () => {
      requests[0].resolve()
      await flush()
    })
    assert.equal(requests.length, 2, 'overlapping ticks collapse into one catch-up read')

    await React.act(async () => {
      requests[1].resolve()
      await flush()
      interval()
      await flush()
    })
    assert.equal(requests.length, 3)

    const old = requests[2]
    await React.act(async () => {
      root.render(React.createElement(Harness, { scope: 'b' }))
      await flush()
    })
    assert.equal(old.alive(), false, 'dependency cleanup fences the prior request scope')
    assert.equal(requests.at(-1).scope, 'b')
    assert.equal(requests.length, 4, 'the new dependency scope starts independently')

    await React.act(async () => {
      old.resolve()
      requests.at(-1).resolve()
      await flush()
    })
    assert.equal(requests.length, 4, 'settling an obsolete scope cannot schedule another tick')
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
})
