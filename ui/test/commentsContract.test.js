import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import axe from 'axe-core'
import { JSDOM } from 'jsdom'
import React from 'react'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const GEN = 'a'.repeat(64)
const COMMENT_ID = `cmt_${'1'.repeat(32)}`
const TOKEN = 'rv_0123456789ab_abcdefghijklmnopqrstuvwxyzABCDEFG'
const HOSTILE_TEXT = '<img src=x onerror="window.__commentXss=1"><script>window.__commentXss=2</script>'
  + '<a href="javascript:window.__commentXss=3">click</a>'

const row = (overrides = {}) => ({
  comment_id: COMMENT_ID,
  node_id: 7,
  node_generation: 2,
  text: 'Original decision',
  actor_kind: 'deployment_owner',
  actor_label: 'private identity must not render',
  version: 3,
  resolved: false,
  created_at: 10,
  updated_at: 12,
  legacy: false,
  editable: true,
  ...overrides,
})

const response = payload => ({ ok: true, status: 200, json: async () => payload })

async function mountHarness({ url, fetchStub, load }) {
  const dom = new JSDOM(
    '<!doctype html><html lang="en"><head><title>Comments contract</title></head>'
      + '<body><main><h1>Run review</h1><div id="root"></div></main></body></html>',
    { url, pretendToBeVisual: true, runScripts: 'outside-only' },
  )
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    location: dom.window.location,
    localStorage: dom.window.localStorage,
    sessionStorage: dom.window.sessionStorage,
    HTMLElement: dom.window.HTMLElement,
    HTMLTextAreaElement: dom.window.HTMLTextAreaElement,
    Element: dom.window.Element,
    Node: dom.window.Node,
    Event: dom.window.Event,
    KeyboardEvent: dom.window.KeyboardEvent,
    MouseEvent: dom.window.MouseEvent,
    getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
    requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
    cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
    fetch: fetchStub,
    IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    // Four other mounted-contract files may create Vite servers in parallel under `node --test`.
    // Dependency discovery shares a default cache and can race another server's optimizer, leaving
    // an open scanner after one suite closes. SSR loads dependencies through Node directly here.
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true },
  })
  let root
  try {
    const [{ createRoot }, component] = await Promise.all([
      import('react-dom/client'), load(vite),
    ])
    root = createRoot(document.querySelector('#root'))
    return {
      dom,
      component,
      render: async element => {
        await React.act(async () => {
          root.render(element)
          await new Promise(resolve => setTimeout(resolve, 0))
        })
      },
      flush: async (turns = 3) => {
        for (let turn = 0; turn < turns; turn += 1) {
          await React.act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
        }
      },
      close: async () => {
        if (root) await React.act(async () => { root.unmount() })
        await vite.close()
        dom.window.close()
        for (const [key, descriptor] of Object.entries(previous)) {
          if (descriptor === undefined) delete globalThis[key]
          else Object.defineProperty(globalThis, key, descriptor)
        }
      },
    }
  } catch (error) {
    await vite.close()
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor === undefined) delete globalThis[key]
      else Object.defineProperty(globalThis, key, descriptor)
    }
    throw error
  }
}

const buttonByText = text => [...document.querySelectorAll('button')]
  .find(button => button.textContent.trim().includes(text))
const buttonWithExactText = text => [...document.querySelectorAll('button')]
  .find(button => button.textContent.trim() === text)
// Dialog focus/restore has its own mounted contract. Keeping this harness static avoids jsdom's
// focusin recursion while this test isolates review authority, data flow, and escaped rendering.
const StaticPanel = ({ title, children }) => React.createElement('section',
  { className: 'panel-test-shell', 'aria-label': title }, children)

test('owner thread uses exact-attempt reads and preserves an edit draft on CAS conflict', async () => {
  const calls = []
  const fetchStub = async (input, options = {}) => {
    const url = String(input)
    const method = String(options.method || 'GET').toUpperCase()
    calls.push({ url, method, body: options.body })
    if (method === 'POST') return response({
      id: `cmd_${'2'.repeat(32)}`,
      status: 'rejected',
      error: { code: 'comment_version_changed', retryable: false },
    })
    if (url.includes('/history?')) return response({
      comment_id: COMMENT_ID,
      versions: [{
        version: 3, action: 'edited', text: 'Original decision', resolved: false,
        actor_kind: 'local_operator', actor_label: 'ignored', updated_at: 12, event_seq: 30,
      }],
      next_cursor: null,
      has_more: false,
      run_generation: GEN,
    })
    return response({ comments: [row()], next_cursor: null, has_more: false, run_generation: GEN })
  }
  const harness = await mountHarness({
    url: 'https://looplab.test/',
    fetchStub,
    load: vite => vite.ssrLoadModule('/src/CommentsThread.jsx'),
  })
  try {
    const CommentsThread = harness.component.default
    await harness.render(React.createElement(CommentsThread, {
      runId: 'demo', nodeId: 7, nodeGeneration: 2, expectedGeneration: GEN,
    }))
    await harness.flush()

    assert.match(calls[0].url, /\/api\/runs\/demo\/comments\?/)
    assert.match(calls[0].url, /node_id=7/)
    assert.match(calls[0].url, /node_generation=2/)
    const composer = document.querySelector('.comment-composer textarea')
    assert.equal(composer.maxLength, 8192)
    assert.equal(calls.some(call => call.url.includes('/history?')), false,
      'history is owner-only and lazy')

    const history = buttonByText('History (3)')
    assert.ok(history)
    await React.act(async () => { history.click() })
    await harness.flush()
    assert.equal(calls.filter(call => call.url.includes('/history?')).length, 1)
    assert.match(document.querySelector('.comment-history-body').textContent, /Local operator/)
    await React.act(async () => { buttonByText('Hide history').click() })

    await React.act(async () => { buttonByText('Edit').click() })
    const editor = document.querySelector('.comment-editor textarea')
    assert.equal(editor.maxLength, 8192)
    assert.match(document.querySelector('.comment-editor-audit').textContent,
      /new audit version.*Prior text remains/i)
    const valueSetter = Object.getOwnPropertyDescriptor(
      harness.dom.window.HTMLTextAreaElement.prototype, 'value').set
    await React.act(async () => {
      valueSetter.call(editor, 'My concurrent draft')
      editor.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await React.act(async () => { buttonByText('Save comment').click() })
    await harness.flush()

    assert.equal(editor.value, 'My concurrent draft')
    assert.match(document.querySelector('[role="alert"]').textContent, /changed in another tab/i)
    assert.ok(buttonByText('Reload current'))
    assert.ok(buttonByText('Copy my draft'))
    assert.equal(document.querySelector('.comment-filter-bar').getAttribute('role'), 'group')
    const postCall = calls.find(call => call.method === 'POST')
    const submitted = JSON.parse(postCall.body)
    assert.deepEqual(submitted, {
      type: 'comment_edited',
      data: {
        comment_id: COMMENT_ID,
        node_id: 7,
        node_generation: 2,
        expected_version: 3,
        text: 'My concurrent draft',
      },
      expected_generation: GEN,
    })

    harness.dom.window.eval(axe.source)
    const results = await harness.dom.window.axe.run(harness.dom.window.document, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
      rules: { 'color-contrast': { enabled: false } },
    })
    const blocking = results.violations.filter(item => ['critical', 'serious'].includes(item.impact))
    assert.deepEqual(Array.from(blocking, item => item.id), [])
  } finally { await harness.close() }
})

test('a comment at the mutation cap remains auditable while every mutation stays hidden', async () => {
  const calls = []
  const capped = row({ version: 50, editable: false })
  const fetchStub = async input => {
    const url = String(input)
    calls.push(url)
    if (url.includes('/history?')) return response({
      comment_id: COMMENT_ID,
      versions: [{
        version: 50, action: 'edited', text: capped.text, resolved: false,
        actor_kind: 'deployment_owner', actor_label: 'ignored', updated_at: 12, event_seq: 99,
      }],
      next_cursor: null,
      has_more: false,
      run_generation: GEN,
    })
    return response({ comments: [capped], next_cursor: null, has_more: false, run_generation: GEN })
  }
  const harness = await mountHarness({
    url: 'https://looplab.test/',
    fetchStub,
    load: vite => vite.ssrLoadModule('/src/CommentsThread.jsx'),
  })
  try {
    await harness.render(React.createElement(harness.component.default, {
      runId: 'demo', nodeId: 7, nodeGeneration: 2, expectedGeneration: GEN,
    }))
    await harness.flush()
    assert.equal(Boolean(buttonWithExactText('Edit')), false)
    assert.equal(Boolean(buttonWithExactText('Resolve')), false)
    assert.match(document.body.textContent, /read-only.*audit history remains available/i)
    const history = buttonWithExactText('History (50)')
    assert.ok(history)
    await React.act(async () => { history.click() })
    await harness.flush()
    assert.equal(calls.filter(url => url.includes('/history?')).length, 1)
    assert.match(document.querySelector('.comment-history-body').textContent, /Deployment owner/)
  } finally { await harness.close() }
})

test('same-run generation reset clears old pages before the replacement request settles or fails', async () => {
  const nextGeneration = 'b'.repeat(64)
  let finishReplacement
  let reads = 0
  const replacement = new Promise(resolve => { finishReplacement = resolve })
  const fetchStub = async () => {
    reads += 1
    if (reads === 1) return response({
      comments: [row({ text: 'Generation A private decision' })],
      next_cursor: null,
      has_more: false,
      run_generation: GEN,
    })
    return replacement
  }
  const harness = await mountHarness({
    url: 'https://looplab.test/', fetchStub,
    load: vite => vite.ssrLoadModule('/src/CommentsThread.jsx'),
  })
  try {
    const CommentsThread = harness.component.default
    const props = {
      runId: 'same-run', nodeId: 7, nodeGeneration: 2,
    }
    await harness.render(React.createElement(CommentsThread, {
      ...props, expectedGeneration: GEN,
    }))
    await harness.flush()
    assert.match(document.body.textContent, /Generation A private decision/)

    await harness.render(React.createElement(CommentsThread, {
      ...props, expectedGeneration: nextGeneration,
    }))
    await harness.flush()
    assert.equal(reads, 2)
    assert.doesNotMatch(document.body.textContent, /Generation A private decision/,
      'old-generation comments must disappear as soon as the resource identity changes')
    assert.match(document.body.textContent, /Loading comments/)

    await React.act(async () => {
      finishReplacement({
        ok: false,
        status: 503,
        headers: { get: () => null },
        json: async () => ({ detail: 'replacement generation is temporarily unavailable' }),
      })
      await new Promise(resolve => setTimeout(resolve, 0))
    })
    await harness.flush()
    assert.doesNotMatch(document.body.textContent, /Generation A private decision/)
    const failure = document.querySelector('.comment-feed-error')
    assert.ok(failure)
    assert.equal(failure.classList.contains('stale'), false,
      'a failed generation-B read must not label generation-A pages as safe stale data')
    assert.doesNotMatch(failure.textContent, /Showing the last received comments/)
  } finally { await harness.close() }
})

test('a nonterminal command with a failed status read remains an unknown outcome, not a new intent', async () => {
  const calls = []
  const commandId = `cmd_${'8'.repeat(32)}`
  const fetchStub = async (input, options = {}) => {
    const url = String(input)
    const method = String(options.method || 'GET').toUpperCase()
    calls.push({ url, method })
    if (method === 'POST' && /\/commands$/.test(url)) {
      return response({ id: commandId, status: 'accepted' })
    }
    if (method === 'GET' && url.endsWith(`/commands/${commandId}`)) {
      return {
        ok: false,
        status: 400,
        headers: { get: () => null },
        json: async () => ({ detail: 'status observation failed' }),
      }
    }
    return response({ comments: [row()], next_cursor: null, has_more: false, run_generation: GEN })
  }
  const harness = await mountHarness({
    url: 'https://looplab.test/', fetchStub,
    load: vite => vite.ssrLoadModule('/src/CommentsThread.jsx'),
  })
  try {
    await harness.render(React.createElement(harness.component.default, {
      runId: 'demo', nodeId: 7, nodeGeneration: 2, expectedGeneration: GEN,
    }))
    await harness.flush()
    const composer = document.querySelector('.comment-composer textarea')
    const setter = Object.getOwnPropertyDescriptor(
      harness.dom.window.HTMLTextAreaElement.prototype, 'value').set
    await React.act(async () => {
      setter.call(composer, 'Do not duplicate me')
      composer.dispatchEvent(new Event('input', { bubbles: true }))
      buttonByText('Post comment').click()
      await new Promise(resolve => setTimeout(resolve, 350))
    })
    await harness.flush()

    assert.equal(composer.value, 'Do not duplicate me')
    assert.match(document.querySelector('.comment-composer [role="alert"]').textContent,
      /outcome is not known.*draft is preserved/i)
    assert.ok(buttonByText('Refresh comments'))
    assert.equal(buttonByText('Retry same command'), undefined)
    assert.equal(buttonWithExactText('Post comment').disabled, true)
    assert.equal(calls.filter(call => call.method === 'POST').length, 1,
      'an unobserved accepted command must not be submitted again')
  } finally { await harness.close() }
})

test('retryable create, edit, resolve, and reopen failures re-arm only their exact durable command', async () => {
  const calls = []
  const commandIds = []
  let serverText = 'Original decision'
  let serverResolved = false
  let serverVersion = 3
  const fetchStub = async (input, options = {}) => {
    const url = String(input)
    const method = String(options.method || 'GET').toUpperCase()
    const call = { url, method, body: options.body, headers: options.headers || {} }
    calls.push(call)
    if (method === 'POST' && /\/retry$/.test(url)) {
      const id = url.match(/commands\/(cmd_[0-9a-f]{32})\/retry$/)?.[1]
      const command = commandIds.find(item => item.id === id)
      if (command?.type === 'comment_edited') {
        serverText = command.text
        serverVersion += 1
      } else if (command?.type === 'comment_resolution_changed') {
        serverResolved = command.resolved
        serverVersion += 1
      }
      return response({ id, status: 'succeeded' })
    }
    if (method === 'POST' && /\/commands$/.test(url)) {
      const payload = JSON.parse(options.body)
      const id = `cmd_${String(commandIds.length + 1).padStart(32, '0')}`
      commandIds.push({
        id, type: payload.type, text: payload.data?.text, resolved: payload.data?.resolved,
      })
      return response({
        id, status: 'failed', event_type: payload.type,
        error: { code: 'event_lock_unavailable', retryable: true },
      })
    }
    return response({
      comments: [row({
        text: serverText, resolved: serverResolved, version: serverVersion,
        updated_at: 12 + serverVersion,
      })],
      next_cursor: null,
      has_more: false,
      run_generation: GEN,
    })
  }
  const harness = await mountHarness({
    url: 'https://looplab.test/', fetchStub,
    load: vite => vite.ssrLoadModule('/src/CommentsThread.jsx'),
  })
  const setTextarea = async (textarea, value) => {
    const setter = Object.getOwnPropertyDescriptor(
      harness.dom.window.HTMLTextAreaElement.prototype, 'value').set
    await React.act(async () => {
      setter.call(textarea, value)
      textarea.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  try {
    await harness.render(React.createElement(harness.component.default, {
      runId: 'demo', nodeId: 7, nodeGeneration: 2, expectedGeneration: GEN,
    }))
    await harness.flush()

    const composer = document.querySelector('.comment-composer textarea')
    await setTextarea(composer, 'Create retry')
    await React.act(async () => { buttonByText('Post comment').click() })
    await harness.flush()
    assert.match(document.querySelector('.comment-composer [role="alert"]').textContent,
      /temporarily unavailable.*draft is preserved/i)
    assert.ok(buttonByText('Retry same command'))
    await React.act(async () => { buttonByText('Retry same command').click() })
    await harness.flush()
    assert.equal(composer.value, '')

    await React.act(async () => { buttonByText('Edit').click() })
    const editor = document.querySelector('.comment-editor textarea')
    await setTextarea(editor, 'First edit')
    await React.act(async () => { buttonByText('Save comment').click() })
    await harness.flush()
    const abandonedEditId = commandIds.at(-1).id
    assert.ok(buttonByText('Retry same command'))
    assert.equal(editor.value, 'First edit', 'strict-lock failure preserves the visible draft')

    await setTextarea(editor, 'Second edit')
    assert.equal(buttonByText('Retry same command'), undefined,
      'changing the visible draft invalidates retry of the old payload')
    assert.ok(buttonByText('Save comment'))
    await React.act(async () => { buttonByText('Save comment').click() })
    await harness.flush()
    const currentEditId = commandIds.at(-1).id
    assert.notEqual(currentEditId, abandonedEditId)
    assert.equal(calls.some(call => call.url.endsWith(`/commands/${abandonedEditId}/retry`)), false)
    await React.act(async () => { buttonByText('Retry same command').click() })
    await harness.flush()
    assert.equal(document.activeElement, buttonWithExactText('Edit'),
      'closing a successful editor returns keyboard focus to its trigger')

    await React.act(async () => { buttonByText('All').click() })
    await React.act(async () => { buttonWithExactText('Resolve').click() })
    await harness.flush()
    assert.ok(buttonWithExactText('Retry resolve'))
    await React.act(async () => { buttonWithExactText('Retry resolve').click() })
    await harness.flush()
    assert.ok(buttonWithExactText('Reopen'))
    await React.act(async () => { buttonWithExactText('Reopen').click() })
    await harness.flush()
    assert.ok(buttonWithExactText('Retry reopen'))
    await React.act(async () => { buttonWithExactText('Retry reopen').click() })
    await harness.flush()

    assert.deepEqual(commandIds.map(item => item.type), [
      'comment_created', 'comment_edited', 'comment_edited',
      'comment_resolution_changed', 'comment_resolution_changed',
    ])
    assert.deepEqual(commandIds.map(item => item.text), [
      'Create retry', 'First edit', 'Second edit', undefined, undefined,
    ])
    assert.deepEqual(commandIds.map(item => item.resolved), [
      undefined, undefined, undefined, true, false,
    ])
    const retryCalls = calls.filter(call => call.method === 'POST' && /\/retry$/.test(call.url))
    assert.deepEqual(retryCalls.map(call => call.url.match(/commands\/(cmd_[0-9a-f]{32})\/retry$/)[1]), [
      commandIds[0].id, currentEditId, commandIds[3].id, commandIds[4].id,
    ])
    assert.equal(retryCalls.every(call => call.body === undefined), true)
    const submitCalls = calls.filter(call => call.method === 'POST' && /\/commands$/.test(call.url))
    const keys = submitCalls.map(call => call.headers['Idempotency-Key'])
    assert.equal(keys.every(Boolean), true)
    assert.equal(new Set(keys).size, keys.length, 'each changed intent gets a fresh key')
  } finally { await harness.close() }
})

test('review collaboration panel never loads owner links, history, or mutations', async () => {
  const calls = []
  const fetchStub = async (input, options = {}) => {
    calls.push({ url: String(input), method: String(options.method || 'GET').toUpperCase() })
    return response({
      comments: [
        row({ text: HOSTILE_TEXT }),
        row({
          comment_id: 'legacy_9', node_generation: null, text: 'Old annotation',
          actor_kind: 'legacy_unknown', version: 1, legacy: true, editable: false,
        }),
      ],
      next_cursor: null,
      has_more: false,
      run_generation: GEN,
    })
  }
  const harness = await mountHarness({
    url: `https://looplab.test/review#/${TOKEN}`,
    fetchStub,
    load: vite => vite.ssrLoadModule('/src/CollabPanel.jsx'),
  })
  try {
    const CollabPanel = harness.component.default
    harness.dom.window.__commentXss = 0
    await harness.render(React.createElement(CollabPanel, {
      state: {}, runId: 'demo', onClose: () => {}, reviewMode: true,
      expectedGeneration: GEN, refreshKey: 9, PanelComponent: StaticPanel,
    }))
    await harness.flush()

    assert.equal(calls.length, 1)
    assert.match(calls[0].url, /\/api\/review\/comments\?include_resolved=true&limit=100/)
    assert.equal(calls.some(call => /\/api\/runs\/demo\/reviews/.test(call.url)), false)
    assert.equal(calls.every(call => call.method === 'GET'), true)
    assert.ok(document.querySelector('.panel-test-shell'))
    assert.doesNotMatch(document.body.textContent, /Create a read-only review link/)
    assert.equal(Boolean(document.querySelector('textarea')), false)
    assert.equal(Boolean(buttonByText('Edit')), false)
    assert.equal(Boolean(buttonWithExactText('Resolve')), false)
    assert.equal(Boolean(buttonByText('History')), false)
    assert.equal(Boolean(buttonByText('Experiment #7 · attempt 2')), true)
    assert.match(document.body.textContent, /Experiment #7 · attempt unknown/)
    assert.equal([...document.querySelectorAll('button')]
      .some(button => button.textContent.includes('attempt unknown')), false,
    'legacy notes must not deep-link into a current attempt')
    assert.match(document.body.textContent, /Deployment owner/)
    assert.doesNotMatch(document.body.textContent, /private identity must not render/)
    const hostile = document.querySelector(`[data-comment-id="${COMMENT_ID}"] .comment-text`)
    assert.equal(hostile.textContent, HOSTILE_TEXT)
    assert.equal(Boolean(hostile.querySelector('img, script, a')), false)
    assert.match(hostile.innerHTML, /&lt;img/)
    assert.equal(harness.dom.window.__commentXss, 0)
  } finally { await harness.close() }
})

test('reviewMode alone fails closed even when a caller omits the redundant readOnly prop', async () => {
  const calls = []
  const fetchStub = async (input, options = {}) => {
    calls.push({ url: String(input), method: String(options.method || 'GET').toUpperCase() })
    return response({
      comments: [row({ text: HOSTILE_TEXT })], next_cursor: null, has_more: false,
      run_generation: GEN,
    })
  }
  const harness = await mountHarness({
    url: `https://looplab.test/review#/${TOKEN}`,
    fetchStub,
    load: vite => vite.ssrLoadModule('/src/CommentsThread.jsx'),
  })
  try {
    await harness.render(React.createElement(harness.component.default, {
      runId: 'demo', nodeId: 7, nodeGeneration: 2, expectedGeneration: GEN,
      reviewMode: true,
    }))
    await harness.flush()
    assert.equal(Boolean(document.querySelector('textarea')), false)
    assert.equal(Boolean(buttonWithExactText('Edit')), false)
    assert.equal(Boolean(buttonWithExactText('Resolve')), false)
    assert.equal(Boolean(buttonByText('History')), false)
    assert.equal(calls.every(call => call.method === 'GET'), true)
    assert.equal(calls.some(call => call.url.includes('/history')), false)
    const hostile = document.querySelector(`[data-comment-id="${COMMENT_ID}"] .comment-text`)
    assert.equal(hostile.textContent, HOSTILE_TEXT)
    assert.equal(Boolean(hostile.querySelector('img, script, a')), false)
    assert.equal(harness.dom.window.__commentXss, undefined)
  } finally { await harness.close() }
})

test('RunView refreshes comment feeds only from comments_revision, never global seq', async () => {
  const [runView, inspector, hook] = await Promise.all([
    readFile(new URL('../src/RunView.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/Inspector.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/useComments.js', import.meta.url), 'utf8'),
  ])
  assert.match(runView, /commentsRevision=\{state\?\.comments_revision\}/)
  assert.match(runView, /refreshKey=\{state\?\.comments_revision\}/)
  assert.doesNotMatch(runView, /(?:commentsRevision|refreshKey)=\{seq\}/)
  assert.match(inspector, /refreshKey=\{commentsRevision\}/)
  assert.match(hook, /refreshKey/)
  assert.match(hook, /commentMatchesSubject/)
  assert.match(inspector, /nodeGeneration=\{n\.attempt\}/)
  assert.match(runView, /currentAttempt !== comment\.nodeGeneration/)
  assert.match(runView, /commentAttemptMatches \? routeState\.commentId : null/)
  assert.match(inspector, /detailResource\.scope === detailScope/)
  assert.match(inspector, /const detail = detailCurrent \? detailResource\.data : null/,
    'a node, attempt, or generation switch must hide stale full detail before passive effects')
  assert.match(inspector, /if \(on && detailMatchesAttempt\(d\)\)/,
    'a reset racing the full-detail response must not relabel another attempt as current')
  assert.match(runView, /const preserveComment = id === current\.nodeId && nextTab === 'Comments'/)
  assert.match(runView, /nodeGeneration: preserveComment \? current\.nodeGeneration : null/,
    'dock/report node transitions must not retarget an old comment to a different node')
  assert.match(hook, /const renderScopeChanged = scopeRef\.current !== scopeKey \|\| !resourceEnabled/)
  assert.match(hook, /comments: renderScopeChanged \? \[\] : comments/,
    'scope changes must hide prior comment pages during render, before passive effects')
  const commentsThread = await readFile(new URL('../src/CommentsThread.jsx', import.meta.url), 'utf8')
  assert.match(commentsThread,
    /key=\{`\$\{runId\}:\$\{expectedGeneration \|\| 'unknown'\}:\$\{comment\.id\}:\$\{comment\.version\}`\}/,
    'a new run/version/generation must invalidate an already-open history cache')
})
