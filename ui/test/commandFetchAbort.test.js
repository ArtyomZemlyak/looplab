// A caller's abort settles a command read AT ONCE, whatever the transport does with the signal
// (review 2026-09-22, UI-05). `commandFetch` raced the fetch against its own 8 s deadline only, so a
// transport that ignores AbortSignal — every test double that leaves a request unanswered — kept the
// read, and its timer, pending until the deadline fired: three UI test files each held the process
// ~8 s past their last test. A browser's fetch rejects on abort by itself, so the product sees the
// same AbortError it always did; what changed is that it no longer depends on the transport.
import test from 'node:test'
import assert from 'node:assert/strict'

const withSilentTransport = async (body) => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'fetch')
  const handed = []
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true, writable: true,
    // Never settles and never looks at `options.signal`: the transport this change is about.
    value: (url, options = {}) => { handed.push(options); return new Promise(() => {}) },
  })
  try { return await body(handed) } finally {
    if (previous) Object.defineProperty(globalThis, 'fetch', previous)
    else delete globalThis.fetch
  }
}

test('an abort settles a command read that the transport would leave pending', async () => {
  await withSilentTransport(async handed => {
    const { commandRead } = await import('../src/commandProtocol.js')
    const controller = new AbortController()
    const started = Date.now()
    const pending = commandRead('/api/runs/r/commands/c', { signal: controller.signal })
    await new Promise(resolve => setTimeout(resolve, 10))
    controller.abort()
    await assert.rejects(pending, error => error?.name === 'AbortError')
    assert.ok(Date.now() - started < 2_000, 'the abort settled the read, not its 8 s deadline')
    assert.ok(handed.length === 1 && handed[0].signal, 'the transport was still handed a signal')
  })
})

test('an already-aborted signal settles the read without waiting for the deadline', async () => {
  await withSilentTransport(async () => {
    const { commandRead } = await import('../src/commandProtocol.js')
    const controller = new AbortController()
    controller.abort()
    const started = Date.now()
    await assert.rejects(commandRead('/api/runs/r/commands/c', { signal: controller.signal }),
      error => error?.name === 'AbortError')
    assert.ok(Date.now() - started < 2_000)
  })
})

test('a settled read leaves no listener on a long-lived caller signal', async () => {
  // The abort participant is unlinked with `forwardAbort` in the same `finally`: a component that
  // reuses one signal across many reads must not collect a dead listener per settled read.
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'fetch')
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true, writable: true,
    value: async () => new Response('{"ok":true}', { status: 200, headers: { 'content-type': 'application/json' } }),
  })
  try {
    const { commandRead } = await import('../src/commandProtocol.js')
    const live = new Set()
    const signal = {
      aborted: false, reason: undefined,
      addEventListener: (type, listener) => { if (type === 'abort') live.add(listener) },
      removeEventListener: (type, listener) => { if (type === 'abort') live.delete(listener) },
    }
    for (let i = 0; i < 5; i += 1) await commandRead('/api/runs/r/commands/c', { signal })
    assert.equal(live.size, 0, 'every listener a read linked was unlinked when it settled')
  } finally {
    if (previous) Object.defineProperty(globalThis, 'fetch', previous)
    else delete globalThis.fetch
  }
})
