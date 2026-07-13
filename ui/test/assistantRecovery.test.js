import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { assistantMessageStream } from '../src/api.js'
import {
  assistantRecoveryFailure, assistantRecoveryPayload, assistantReplyCompletesTurn,
  danglingAssistantTurn,
} from '../src/assistantRecovery.js'

const completedStream = () => ({
  ok: true,
  status: 200,
  body: { getReader: () => ({ read: async () => ({ done: true }) }) },
})

const withFetch = async (fetchImpl, fn) => {
  const previous = {
    fetch: globalThis.fetch, location: globalThis.location, sessionStorage: globalThis.sessionStorage,
  }
  globalThis.location = { pathname: '/proxy/app/', hash: '' }
  globalThis.sessionStorage = { getItem: () => '' }
  globalThis.fetch = fetchImpl
  try { return await fn() }
  finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
}

test('reload recovery preserves hidden run/file context and the exact persisted permission mode', () => {
  const raw = 'Review #12\n\n[UI context: run "paid-run" is open.]\n\n'
    + '[Attached files — use their content as context]\n--- notes.md ---\nprivate evidence\n'
  const turn = {
    role: 'user', content: 'Review #12', raw, mode: 'default', turn_id: 'durable-turn-1',
  }
  assert.equal(danglingAssistantTurn([{ role: 'assistant', content: 'older' }, turn]), turn)
  assert.deepEqual(assistantRecoveryPayload(turn), {
    instruction: raw, display: 'Review #12', mode: 'default',
  })
})

test('recovery falls back to the persisted visible content without substituting the current mode', () => {
  const turn = {
    role: 'user', content: 'Explain the result', mode: 'acceptEdits', turn_id: 'durable-turn-2',
  }
  assert.deepEqual(assistantRecoveryPayload(turn), {
    instruction: 'Explain the result', display: 'Explain the result', mode: 'acceptEdits',
  })
  assert.equal(assistantRecoveryPayload({ ...turn, mode: 'changed-or-corrupt' }), null)
  assert.equal(assistantRecoveryPayload({ ...turn, turn_id: '' }), null)
  assert.equal(danglingAssistantTurn([turn, { role: 'assistant', content: 'done' }]), null)
})

test('exact recovery POST always carries instruction, clean display, and mode on the existing stream endpoint', async () => {
  const calls = []
  const payload = {
    instruction: '[persisted attachment]\nSummarize it', display: 'Summarize it', mode: 'plan',
  }
  await withFetch(async (url, options) => {
    calls.push({ url, options })
    return completedStream()
  }, async () => assistantMessageStream('session one', payload.instruction, payload.mode, {}, undefined,
    payload.display))

  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/proxy/app/api/assistant/sessions/session%20one/message_stream')
  assert.equal(calls[0].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[0].options.body), payload)
})

test('an equal display is still explicit in the exact recovery body', async () => {
  let body
  await withFetch(async (_url, options) => {
    body = JSON.parse(options.body)
    return completedStream()
  }, async () => assistantMessageStream('same', 'same text', 'auto', {}, undefined, 'same text'))
  assert.deepEqual(body, { instruction: 'same text', display: 'same text', mode: 'auto' })
})

test('identity mismatch is blocked while ambiguous transport and an already-running worker keep polling', () => {
  const mismatch = assistantRecoveryFailure({
    status: 409, code: 'assistant_turn_recovery_mismatch',
  })
  assert.equal(mismatch.blocked, true)
  assert.match(mismatch.message, /instruction or permission mode no longer matches/)
  assert.equal(assistantRecoveryFailure({ status: 409 }), null)
  assert.equal(assistantRecoveryFailure({ status: 503 }), null)
  assert.equal(assistantRecoveryFailure(new TypeError('connection reset')), null)
  assert.match(assistantRecoveryFailure({ status: 404 }).message, /session no longer exists/)
})

test('retry distinguishes a late exact reply from an older reply when the POST was never staged', () => {
  const prior = { role: 'user', content: 'new request', retryPayload: {
    historyLength: 2, raw: '[context]\nnew request', mode: 'default',
  } }
  const oldHistory = [
    { role: 'user', content: 'old request', mode: 'plan', turn_id: 'old' },
    { role: 'assistant', content: 'old answer' },
  ]
  assert.equal(assistantReplyCompletesTurn(oldHistory, prior), false)
  const completed = [...oldHistory,
    { role: 'user', content: 'new request', raw: '[context]\nnew request', mode: 'default', turn_id: 'new' },
    { role: 'assistant', content: 'late exact answer' },
  ]
  assert.equal(assistantReplyCompletesTurn(completed, prior), true)
  assert.equal(assistantReplyCompletesTurn(completed, {
    role: 'user', content: 'new request', turn_id: 'new',
  }), true)
  assert.equal(assistantReplyCompletesTurn(completed, {
    ...prior, retryPayload: { ...prior.retryPayload, mode: 'auto' },
  }), false)
})

test('Assistant reload/retry path reuses the dangling turn and never appends a second user bubble', async () => {
  const [source, chat, css] = await Promise.all([
    readFile(new URL('../src/AssistantBar.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/AssistantChat.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/assistant-polish.css', import.meta.url), 'utf8'),
  ])
  const openSession = source.slice(source.indexOf('const openSession ='), source.indexOf('// Restore the last session'))
  assert.match(openSession, /const dangling = danglingAssistantTurn\(arr\)/)
  assert.match(openSession, /const latestTurn = danglingAssistantTurn\(latest\.messages \|\| \[\]\)/)
  assert.match(openSession, /latestTurn\.turn_id !== dangling\.turn_id/)
  assert.match(openSession, /const recovery = assistantRecoveryPayload\(latestTurn\)/)
  assert.match(openSession, /assistantMessageStream\(id, recovery\.instruction, recovery\.mode, \{\},[\s\S]*?recovery\.display\)/)
  assert.doesNotMatch(openSession, /role: 'user'/)
  assert.match(source, /failedTurn\?\.recoveryNeeded[\s\S]*?danglingAssistantTurn\(durableMessages\)[\s\S]*?openSession\(id, \{ recover: true \}\)/)
  assert.match(source, /assistantReplyCompletesTurn\(durableMessages, prior\)/)
  assert.match(source, /if \(prior\.turn_id\)[\s\S]*?assistantRecoveryPayload\(prior\)[\s\S]*?turnMode: persisted\.mode/)
  assert.match(source, /turnMode: payload\.mode \|\| null/)
  assert.match(source, /if \(msgs\[assistantIndex\]\?\.recoveryBlocked\) return null/)
  assert.match(chat, /role=\{m\.recoveryBlocked \? 'alert' : undefined\}/)
  assert.match(css, /\.chat-bubble\.assistant-recovery-blocked/)
})
