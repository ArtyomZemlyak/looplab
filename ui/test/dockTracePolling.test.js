import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
let vite

test.before(async () => {
  vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
})

test.after(async () => { await vite?.close() })

const flush = async () => {
  await Promise.resolve()
  await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
}

function installBrowser(fetchImpl) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    url: 'http://localhost/', pretendToBeVisual: true,
  })
  const intervals = new Map()
  let nextInterval = 1
  const values = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    location: dom.window.location,
    sessionStorage: dom.window.sessionStorage,
    localStorage: dom.window.localStorage,
    HTMLElement: dom.window.HTMLElement,
    Element: dom.window.Element,
    Node: dom.window.Node,
    Event: dom.window.Event,
    MouseEvent: dom.window.MouseEvent,
    getComputedStyle: dom.window.getComputedStyle,
    requestAnimationFrame: dom.window.requestAnimationFrame,
    cancelAnimationFrame: dom.window.cancelAnimationFrame,
    IS_REACT_ACT_ENVIRONMENT: true,
    fetch: fetchImpl,
    setInterval: (callback, ms) => {
      const id = nextInterval++
      intervals.set(id, { callback, ms })
      return id
    },
    clearInterval: id => intervals.delete(id),
  }
  const previous = Object.fromEntries(Object.keys(values)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(values)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  return {
    container: dom.window.document.getElementById('root'),
    tick(ms) {
      for (const timer of [...intervals.values()]) if (timer.ms === ms) timer.callback()
    },
    close() {
      dom.window.close()
      for (const [key, descriptor] of Object.entries(previous)) {
        if (descriptor === undefined) delete globalThis[key]
        else Object.defineProperty(globalThis, key, descriptor)
      }
    },
  }
}

const response = body => ({
  ok: true, status: 200, headers: { get: () => null }, json: async () => body,
})

const tracePayload = (name, nodeId = 7, attempt = 0) => ({
  node_id: nodeId,
  attempt,
  nodes: [{ span_id: name, name, kind: 'operation', start: 0, duration_s: 1, children: [] }],
  projection: {
    schema: 2, truncated: false, total_spans: 1, visible_spans: 1, omitted_spans: 0,
  },
})

const eventProps = overrides => ({
  e: { seq: 1, type: 'node_building', data: { node_id: 7, generation: 0 } },
  onFocusEvent: () => {},
  focusLabel: 'Inspect node',
  autoOpen: true,
  runId: 'run-a',
  liveBuilding: { 7: 0 },
  ...overrides,
})

async function renderDockExport(name, props, fetchImpl, exercise) {
  const browser = installBrowser(fetchImpl)
  let root
  try {
    const dock = await vite.ssrLoadModule('/src/Dock.jsx')
    const { createRoot } = await import('react-dom/client')
    root = createRoot(browser.container)
    await React.act(async () => { root.render(React.createElement(dock[name], props)); await flush() })
    await exercise({ browser, container: browser.container, root, Component: dock[name] })
  } finally {
    if (root) await React.act(async () => { root.unmount(); await flush() })
    browser.close()
  }
}

test('LiveTrace aborts a hung old scope and keeps at most one request in flight per scope', async () => {
  const firstGeneration = 'a'.repeat(64)
  const nextGeneration = 'b'.repeat(64)
  const requests = []
  const fetchImpl = (_url, options = {}) => new Promise(() => {
    requests.push({ signal: options.signal })
  })
  await renderDockExport('LiveTrace', {
    runId: 'run-a', generation: firstGeneration, active: true,
  }, fetchImpl,
    async ({ browser, container, root, Component }) => {
      await React.act(async () => {
        container.querySelector('.lt-toggle').dispatchEvent(new MouseEvent('click', { bubbles: true }))
        await flush()
      })
      assert.equal(requests.length, 1)
      await React.act(async () => { browser.tick(3000); browser.tick(3000); await flush() })
      assert.equal(requests.length, 1, 'a never-settling read must own the only flight')

      await React.act(async () => {
        root.render(React.createElement(Component, {
          runId: 'run-a', generation: nextGeneration, active: true,
        }))
        await flush()
      })
      assert.equal(requests[0].signal.aborted, true, 'scope cleanup must abort the old read')
      assert.equal(requests.length, 2, 'the new generation starts independently')
      await React.act(async () => { browser.tick(3000); await flush() })
      assert.equal(requests.length, 2, 'the new scope also serializes its polls')
    })
  assert.equal(requests[1].signal.aborted, true, 'unmount must abort the current read')
})

test('LiveTrace keeps last-good evidence through a failed poll and clears stale on recovery', async () => {
  const outcomes = [
    response({ tail: [{ kind: 'generation', span_id: 'one', text: 'proven thought' }], projection: {} }),
    new Error('offline'),
    response({ tail: [{ kind: 'tool', span_id: 'two', tool: 'recovered tool' }], projection: {} }),
  ]
  const fetchImpl = () => {
    const outcome = outcomes.shift()
    return outcome instanceof Error ? Promise.reject(outcome) : Promise.resolve(outcome)
  }
  await renderDockExport('LiveTrace', { runId: 'run-a', generation: 2, active: true }, fetchImpl,
    async ({ browser, container }) => {
      await React.act(async () => {
        container.querySelector('.lt-toggle').dispatchEvent(new MouseEvent('click', { bubbles: true }))
        await flush()
      })
      assert.match(container.textContent, /proven thought/)

      await React.act(async () => { browser.tick(3000); await flush() })
      assert.match(container.textContent, /proven thought/,
        'a transient refresh failure must not replace evidence with an empty trace')
      assert.match(container.textContent, /showing confirmed spans while retrying/i)

      await React.act(async () => { browser.tick(3000); await flush() })
      assert.match(container.textContent, /recovered tool/)
      assert.doesNotMatch(container.textContent, /showing confirmed spans/i)
  })
})

test('LiveTrace sends and validates the exact run generation before committing a 200 response', async () => {
  const generation = 'a'.repeat(64)
  const foreignGeneration = 'b'.repeat(64)
  const urls = []
  const outcomes = [
    response({ run_generation: generation,
      tail: [{ kind: 'generation', span_id: 'one', text: 'generation-a evidence' }], projection: {} }),
    response({ run_generation: foreignGeneration,
      tail: [{ kind: 'generation', span_id: 'two', text: 'foreign evidence' }], projection: {} }),
  ]
  const fetchImpl = url => {
    urls.push(String(url))
    return Promise.resolve(outcomes.shift())
  }
  await renderDockExport('LiveTrace', { runId: 'run-a', generation, active: true }, fetchImpl,
    async ({ browser, container }) => {
      await React.act(async () => {
        container.querySelector('.lt-toggle').dispatchEvent(new MouseEvent('click', { bubbles: true }))
        await flush()
      })
      assert.match(urls[0], new RegExp(`expected_generation=${generation}`))
      assert.match(container.textContent, /generation-a evidence/)

      await React.act(async () => { browser.tick(3000); await flush() })
      assert.match(container.textContent, /generation-a evidence/,
        'last-good evidence remains until the correctly scoped generation returns')
      assert.doesNotMatch(container.textContent, /foreign evidence/)
      // The refusal now says WHICH failure it was. It used to read "Trace refresh failed; showing
      // confirmed spans while retrying" — the same sentence a dropped connection printed — so the
      // operator was told to wait out a condition that waiting cannot clear. The evidence rule is
      // unchanged and asserted above; only the words are.
      assert.match(container.textContent, /run generation that was replaced; reload the run/i)
      assert.doesNotMatch(container.textContent, /retrying/i,
        'a superseded read must not promise a recovery that cannot arrive on this scope')
    })
})

test('EventRow node polling aborts a hung request on identity change and unmount', async () => {
  const requests = []
  const fetchImpl = (url, options = {}) => {
    requests.push({ url: String(url), signal: options.signal })
    return requests.length === 1
      ? Promise.resolve(response(tracePayload('scoped-evidence')))
      : new Promise(() => {})
  }
  await renderDockExport('EventRow', eventProps(), fetchImpl,
    async ({ browser, container, root, Component }) => {
      assert.equal(requests.length, 1)
      assert.match(container.textContent, /scoped-evidence/)
      await React.act(async () => { browser.tick(4000); browser.tick(4000); await flush() })
      assert.equal(requests.length, 2, 'one node lifecycle may have only one in-flight trace read')

      await React.act(async () => {
        root.render(React.createElement(Component, eventProps({ runId: 'run-b' })))
        await flush()
      })
      assert.equal(requests[1].signal.aborted, true)
      assert.equal(requests.length, 3)
      assert.match(requests[2].url, /\/runs\/run-b\/nodes\/7\/trace\?attempt=0/)
      assert.doesNotMatch(container.textContent, /scoped-evidence/,
        'last-good evidence from the old run must not cross the identity fence')
    })
  assert.equal(requests[2].signal.aborted, true)
})

test('EventRow retains a proven node trace after failure and retry replaces it on success', async () => {
  const outcomes = [
    response(tracePayload('first-evidence')),
    new Error('offline'),
    response(tracePayload('recovered-evidence')),
  ]
  await renderDockExport('EventRow', eventProps(), () => {
    const outcome = outcomes.shift()
    return outcome instanceof Error ? Promise.reject(outcome) : Promise.resolve(outcome)
  }, async ({ browser, container }) => {
    assert.match(container.textContent, /first-evidence/)
    await React.act(async () => { browser.tick(4000); await flush() })
    assert.match(container.textContent, /first-evidence/)
    assert.match(container.textContent, /showing confirmed spans/i)

    const retry = [...container.querySelectorAll('button')]
      .find(button => button.textContent.trim() === 'Retry trace')
    await React.act(async () => { retry.dispatchEvent(new MouseEvent('click', { bubbles: true })); await flush() })
    assert.match(container.textContent, /recovered-evidence/)
    assert.doesNotMatch(container.textContent, /showing confirmed spans/i)
  })
})

test('OpTrace aborts its one-shot request on scope change and recovers through Retry', async () => {
  const requests = []
  const outcomes = [null, new Error('offline'), response({
    spans: tracePayload('operation-recovered').nodes, projection: {},
  })]
  const fetchImpl = (url, options = {}) => {
    const outcome = outcomes.shift()
    requests.push({ url: String(url), signal: options.signal })
    return outcome == null ? new Promise(() => {})
      : outcome instanceof Error ? Promise.reject(outcome) : Promise.resolve(outcome)
  }
  await renderDockExport('OpTrace', { runId: 'run-a', traceId: 'trace-a' }, fetchImpl,
    async ({ container, root, Component }) => {
      await React.act(async () => {
        root.render(React.createElement(Component, { runId: 'run-a', traceId: 'trace-b' }))
        await flush()
      })
      assert.equal(requests[0].signal.aborted, true)
      assert.match(container.textContent, /Trace unavailable/i)
      const retry = [...container.querySelectorAll('button')]
        .find(button => button.textContent.trim() === 'Retry trace')
      await React.act(async () => { retry.dispatchEvent(new MouseEvent('click', { bubbles: true })); await flush() })
      assert.match(container.textContent, /operation-recovered/)
  })
})

test('OpTrace refuses a fulfilled response from another run generation before commit', async () => {
  const generation = 'a'.repeat(64)
  const foreignGeneration = 'b'.repeat(64)
  const requests = []
  const outcomes = [
    response({ run_generation: foreignGeneration,
      spans: tracePayload('foreign-operation').nodes, projection: {} }),
    response({ run_generation: generation,
      spans: tracePayload('current-operation').nodes, projection: {} }),
  ]
  await renderDockExport('OpTrace', {
    runId: 'run-a', traceId: 'trace-a', expectedGeneration: generation,
  }, url => {
    requests.push(String(url))
    return Promise.resolve(outcomes.shift())
  }, async ({ container }) => {
    assert.match(requests[0], new RegExp(`expected_generation=${generation}`))
    assert.doesNotMatch(container.textContent, /foreign-operation/)
    // Refusing a foreign-generation payload is not a read failure, and it stopped being reported as
    // one: "Trace unavailable" told the operator the spans could not be read, when what happened is
    // that the run they are looking at was replaced. Only reloading helps, so only that is offered.
    assert.match(container.textContent, /run generation that was replaced; reload the run/i)
    assert.doesNotMatch(container.textContent, /retrying automatically/i)

    const retry = [...container.querySelectorAll('button')]
      .find(button => button.textContent.trim() === 'Retry trace')
    await React.act(async () => {
      retry.dispatchEvent(new MouseEvent('click', { bubbles: true })); await flush()
    })
    assert.match(container.textContent, /current-operation/)
    assert.doesNotMatch(container.textContent, /Trace unavailable|reload the run/i)
  })
})

test('a one-shot trace read recovers by itself, within a budget, and never for a superseded one', async () => {
  // The operator's complaint, driven: the routes behind these two surfaces measured 4-15 s per call
  // on the live server against an 8 s client deadline, and a ONE-SHOT read that lost that race
  // parked on "Trace unavailable" until someone clicked. It now re-reads itself a bounded number of
  // times (traceScrollModel.js::traceRetryMs) and only then hands the operator the manual control.
  const attempts = []
  const outcomes = [new Error('slow'), new Error('slow'), response({
    spans: tracePayload('recovered-operation').nodes, projection: {},
  })]
  const fetchImpl = url => {
    attempts.push(String(url))
    const outcome = outcomes.shift()
    return outcome instanceof Error ? Promise.reject(outcome) : Promise.resolve(outcome)
  }
  await renderDockExport('OpTrace', { runId: 'run-a', traceId: 'trace-a' }, fetchImpl,
    async ({ browser, container }) => {
      assert.equal(attempts.length, 1)
      assert.match(container.textContent, /retrying automatically/i,
        'a receipt that is about to re-read itself must say so')
      // The retry WAITS. A restart that read on the spot would spend the whole budget in one frame
      // and re-ask a route that is failing precisely because it is slow.
      assert.equal(attempts.length, 1, 'arming a retry must not itself issue one')
      await React.act(async () => { browser.tick(5000); await flush() })
      assert.equal(attempts.length, 2)
      await React.act(async () => { browser.tick(5000); await flush() })
      assert.equal(attempts.length, 3)
      assert.match(container.textContent, /recovered-operation/,
        'the operator never had to touch anything')
      // Recovery costs exactly the reads it took: no duplicate re-read as the retry machine disarms.
      await React.act(async () => { browser.tick(5000); await flush() })
      assert.equal(attempts.length, 3)
    })
})

test('an exhausted retry budget parks on the manual control instead of hammering the route', async () => {
  const attempts = []
  const fetchImpl = () => { attempts.push(1); return Promise.reject(new Error('down')) }
  await renderDockExport('OpTrace', { runId: 'run-a', traceId: 'trace-a' }, fetchImpl,
    async ({ browser, container }) => {
      for (const expected of [2, 3]) {
        await React.act(async () => { browser.tick(5000); await flush() })
        assert.equal(attempts.length, expected)
      }
      // TRACE_RETRY_MAX automatic re-reads, then it stops: a route that is genuinely down must not
      // be re-asked forever at seconds per call — the same bound armTraceScroll holds for the
      // scroll loader. What remains is the honest control, and it must still be usable.
      await React.act(async () => { browser.tick(5000); browser.tick(5000); await flush() })
      assert.equal(attempts.length, 3, 'the budget is spent and nothing is scheduled')
      assert.doesNotMatch(container.textContent, /retrying automatically/i,
        'a parked surface must not claim a retry that is not coming')
      const retry = [...container.querySelectorAll('button')]
        .find(button => button.textContent.trim() === 'Retry trace')
      await React.act(async () => {
        retry.dispatchEvent(new MouseEvent('click', { bubbles: true })); await flush()
      })
      assert.equal(attempts.length, 4, 'the operator can always ask now')
      // …and asking REFILLS the budget, exactly as focusing the scroll affordance does.
      await React.act(async () => { browser.tick(5000); await flush() })
      assert.equal(attempts.length, 5)
    })
})
