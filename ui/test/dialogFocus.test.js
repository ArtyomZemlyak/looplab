import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

test('dialog focus is trapped, restored, and respects a nested popup Escape', async () => {
  const dom = new JSDOM('<!doctype html><html><body><button id="outside">Open</button><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
    KeyboardEvent: dom.window.KeyboardEvent, Event: dom.window.Event,
    IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previousGlobals = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }

  try {
    const [{ createRoot }, { useDialogFocus }] = await Promise.all([
      import('react-dom/client'), import('../src/useDialogFocus.js'),
    ])
    let closeCount = 0
    const Harness = () => {
      const dialogRef = React.useRef(null)
      useDialogFocus(dialogRef, () => { closeCount++ })
      return React.createElement('div', { ref: dialogRef, role: 'dialog', 'aria-modal': 'true', tabIndex: -1 },
        React.createElement('button', { id: 'first' }, 'First'),
        React.createElement('button', {
          id: 'nested', onKeyDown: event => { if (event.key === 'Escape') event.preventDefault() },
        }, 'Nested popup'),
        React.createElement('button', { id: 'last' }, 'Last'))
    }

    const outside = document.querySelector('#outside')
    outside.focus()
    const root = createRoot(document.querySelector('#root'))
    await React.act(async () => { root.render(React.createElement(Harness)) })
    assert.equal(document.activeElement?.id, 'first')

    const key = (target, keyName, options = {}) => React.act(() => {
      target.dispatchEvent(new KeyboardEvent('keydown', {
        key: keyName, bubbles: true, cancelable: true, ...options,
      }))
    })
    const first = document.querySelector('#first')
    const nested = document.querySelector('#nested')
    const last = document.querySelector('#last')

    last.focus(); await key(last, 'Tab')
    assert.equal(document.activeElement, first, 'Tab wraps from the final control')
    first.focus(); await key(first, 'Tab', { shiftKey: true })
    assert.equal(document.activeElement, last, 'Shift+Tab wraps from the first control')

    nested.focus(); await key(nested, 'Escape')
    assert.equal(closeCount, 0, 'an Escape consumed by a nested popup does not close its dialog')
    await key(first, 'Escape')
    assert.equal(closeCount, 1, 'an unconsumed Escape closes only the active dialog')

    await React.act(async () => { root.unmount() })
    assert.equal(document.activeElement, outside, 'unmount restores the invoking control')

    let parentCloses = 0
    let childCloses = 0
    const NestedHarness = () => {
      const [childOpen, setChildOpen] = React.useState(false)
      const parentRef = React.useRef(null)
      const childRef = React.useRef(null)
      useDialogFocus(parentRef, () => { parentCloses++ })
      useDialogFocus(childRef, () => { childCloses++; setChildOpen(false) }, childOpen)
      return React.createElement('div', { ref: parentRef, role: 'dialog', 'aria-modal': 'true', tabIndex: -1 },
        React.createElement('button', { id: 'open-child', onClick: () => setChildOpen(true) }, 'Open child'),
        childOpen && React.createElement('div', {
          ref: childRef, role: 'dialog', 'aria-modal': 'true', tabIndex: -1,
        }, React.createElement('button', { id: 'child-action' }, 'Child action')))
    }

    outside.focus()
    const nestedRoot = createRoot(document.querySelector('#root'))
    await React.act(async () => { nestedRoot.render(React.createElement(NestedHarness)) })
    const childTrigger = document.querySelector('#open-child')
    await React.act(async () => { childTrigger.click() })
    const childAction = document.querySelector('#child-action')
    assert.equal(document.activeElement, childAction)

    await key(childAction, 'Escape')
    assert.equal(childCloses, 1)
    assert.equal(parentCloses, 0, 'one Escape closes only the topmost dialog')
    assert.equal(document.activeElement, childTrigger, 'child close restores its invoking control')

    await key(childTrigger, 'Escape')
    assert.equal(parentCloses, 1, 'the next Escape reaches the parent dialog')
    await React.act(async () => { nestedRoot.unmount() })
    assert.equal(document.activeElement, outside, 'parent unmount restores the external invoker')
  } finally {
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previousGlobals)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
  }
})
