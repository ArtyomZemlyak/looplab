import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  DEFAULT_REQUEST_TIMEOUT_MS, boundedRequest, deadlineRequest,
} from '../src/requestDeadline.js'

// doc 25 UI-12. Four "fetch with a deadline" shapes coexisted and components picked among them ad
// hoc — including two per-component one-liners that had already drifted into different spellings of
// the same 12s bound. Two shapes are genuinely different and both stay; the conveniences live in one
// place now, and this file is where the choice between them is enforced rather than remembered.

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('the awaited form and the handle form share one bound', async () => {
  assert.equal(DEFAULT_REQUEST_TIMEOUT_MS, 12_000)
  const value = await boundedRequest(async () => 'ok')
  assert.equal(value, 'ok')
  const handle = deadlineRequest(async () => 'ok', DEFAULT_REQUEST_TIMEOUT_MS)
  assert.equal(await handle.promise, 'ok')
  assert.ok(handle.controller instanceof AbortController, 'the handle form still exposes cancel')
  assert.equal(typeof handle.timedOut, 'function')
})

test('the awaited form still enforces the deadline it inherited', async () => {
  const started = Date.now()
  await assert.rejects(
    boundedRequest(signal => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
    }), 20),
    error => error.name === 'TimeoutError')
  assert.ok(Date.now() - started < 5_000, 'it must not wait for a read that never settles')
})

test('the awaited form rejects rather than resolving undefined on failure', async () => {
  // The reason it is a thin wrapper and not a try/catch: a swallowed failure would look to the
  // caller exactly like a successful empty read.
  await assert.rejects(boundedRequest(async () => { throw new Error('boom') }),
    error => error.message === 'boom')
})

test('no component re-declares its own deadline helper', async () => {
  // The rule is about the BOUND, not the name: a component may wrap `deadlineRequest` under a local
  // name (CollabPanel does, because its call sites need the handle), but the number must come from
  // the shared constant. A literal on that line is a second opinion about how long a component read
  // may take, which is exactly the drift that produced `12000` and `12_000` in two files.
  for (const name of ['AssistantBar.jsx', 'CollabPanel.jsx']) {
    for (const line of (await source(name)).split('\n')) {
      if (!/^const bounded\w*\s*=/.test(line)) continue
      assert.doesNotMatch(line, /\d[\d_]{2,}/,
        `${name}: a local deadline helper carries its own numeric bound — use the shared constant`)
    }
  }
  const collab = await source('CollabPanel.jsx')
  // It keeps a NAMED local, because its three call sites want the handle — but the bound is the
  // shared constant, not a second opinion about how long a component read may take.
  assert.match(collab, /const REVIEW_LINK_TIMEOUT_MS = DEFAULT_REQUEST_TIMEOUT_MS/)
})

test('the assistant reads through the shared awaited form', async () => {
  const text = await source('AssistantBar.jsx')
  // The import list may carry `deadlineRequest` too — the assistant uses the handle form in
  // places. What must hold is that `boundedRequest` comes from the shared module and is not
  // re-declared locally (the previous test covers that half).
  assert.match(text, /import \{[^}]*\bboundedRequest\b[^}]*\} from '\.\/requestDeadline\.js'/)
  assert.ok((text.match(/boundedRequest\(/g) || []).length > 10,
    'the assistant is the heaviest user of the awaited form; it must not have drifted back')
})

test('commandFetch is deliberately NOT built on the primitive', async () => {
  // It bounds a durable COMMAND submission, where the body read is part of the operation and a
  // timeout has to surface as a typed error the command lifecycle understands. Collapsing the two
  // would turn a submission timeout into a generic TimeoutError the retry path cannot classify.
  const api = await source('api.js')
  const fetchBody = api.slice(api.indexOf('async function commandFetch'),
    api.indexOf('const commandJson'))
  assert.doesNotMatch(fetchBody, /deadlineRequest|boundedRequest/)
  assert.match(fetchBody, /commandTimeoutError\(path, timeout\)/)
  assert.match(api, /COMMAND_REQUEST_TIMEOUT/)
})

test('the choice between the shapes is written down where they live', async () => {
  const text = await source('requestDeadline.js')
  for (const named of ['deadlineRequest', 'boundedRequest', 'commandFetch', 'deadlineGet']) {
    assert.ok(text.includes(named), `the guide does not mention ${named}`)
  }
})
