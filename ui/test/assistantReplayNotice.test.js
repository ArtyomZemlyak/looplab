// The "Re-running the interrupted turn" line (2026-10-06) said a turn was being REPLAYED whenever the
// reopened transcript ended in a staged user turn (`recoveryNeeded`). That is also the transcript of
// an ordinary reload while the server's ORIGINAL worker is still answering — the bar reattaches to
// that worker and posts nothing — so the operator was told a reply had been lost while it was still
// on its way. The line is now `assistantRecovery.js::replayingTurnNotice`, true only once the exact
// recovery was actually POSTED for the placeholder.
//
// Both halves are driven here: the rule's truth table, and a LIVE mount (`_mount.js::mountLive`)
// that reopens a saved chat ending in a staged turn through the same `assistant_get` a reload
// performs, once with the server's worker still running and once with it gone.
import test from 'node:test'
import assert from 'node:assert/strict'

import React from 'react'

import { replayingTurnNotice } from '../src/assistantRecovery.js'
import { fetchStub, mountLive, settle, sseStream, until } from './_mount.js'

test('the replay line is drawn only for a placeholder whose replay was posted', () => {
  const live = { role: 'assistant', content: '', streaming: true }
  assert.equal(replayingTurnNotice({ ...live, replaying: true }), true)
  assert.equal(replayingTurnNotice({ ...live, recoveryNeeded: true }), false,
    'a staged turn being REATTACHED to is not a replay')
  assert.equal(replayingTurnNotice({ ...live, replaying: true, streaming: false }), false,
    'a settled turn is not being re-run')
  assert.equal(replayingTurnNotice({ role: 'user', content: 'q', streaming: true, replaying: true }), false)
  assert.equal(replayingTurnNotice(null), false)
})

const SID = 'fedcba9876543210'
const META = {
  id: SID, title: 'Replay', mode: 'plan', updated: 1_700_000_000,
  shared: false, share_count: 0, share_ids: [], live_share_ids: [],
  share_expires_at: null, share_live: false,
}
// The reopened transcript: one settled exchange, then a user turn the server staged and never
// answered — the shape both an in-flight turn and a lost one leave behind.
const TRANSCRIPT = [
  { role: 'user', content: 'First question', turn_id: 't0', mode: 'plan' },
  { role: 'assistant', turn_id: 't0', content: 'First answer.' },
  { role: 'user', content: 'Second question', turn_id: 't1', mode: 'plan' },
]

let harness
let AssistantBar

test.before(async () => {
  harness = await mountLive()
  ;({ default: AssistantBar } = await harness.load('/src/AssistantBar.jsx'))
})

test.after(async () => {
  await harness?.close()
})

async function reopenChat(progress) {
  const streams = []
  const fetch = fetchStub({
    'GET /api/assistant/commands': { commands: [] },
    'GET /api/assistant/sessions': { sessions: [META] },
    [`GET /api/assistant/sessions/${SID}`]: { messages: TRANSCRIPT, meta: META },
    'GET /api/assistant/watches': { watches: [] },
    'GET /api/runs': [],
    'GET /api/assistant/permissions': { ok: true, pending: [] },
    'GET /api/assistant/progress': progress,
    [`POST /api/assistant/sessions/${SID}/message_stream`]: () => {
      const stream = sseStream()
      streams.push(stream)
      return stream.response()
    },
  })
  globalThis.fetch = fetch
  localStorage.clear()
  sessionStorage.clear()
  localStorage.setItem('ll.asstSid', SID)
  const mounted = await harness.mount(AssistantBar, { runId: null })
  const { container } = mounted
  await until(() => fetch.calls.some(call => call.path === `/api/assistant/sessions/${SID}`),
    'the saved chat to be read back')
  await settle()
  const sideButton = container.querySelector('button.cmdbar-drawer-btn')
  assert.ok(sideButton, 'the bar offers the side view')
  await React.act(async () => {
    sideButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await settle()
  const turns = () => [...container.querySelectorAll('.feed-msg.chat')]
  return { container, calls: fetch.calls, streams, turns, unmount: () => mounted.unmount() }
}

const posted = calls => calls.filter(call => call.method === 'POST'
  && call.path === `/api/assistant/sessions/${SID}/message_stream`)

test('a reload that reattaches to a running turn does not say it is re-running it', async () => {
  const chat = await reopenChat({ active: true, text: '', steps: ['Read run r1'],
    activity: [{ type: 'text', content: 'Still reading the run.' }], last_event: 1_700_000_100 })
  try {
    await until(() => chat.turns().length === TRANSCRIPT.length + 1, 'the live placeholder')
    const live = chat.turns().at(-1)
    await until(() => live.textContent.includes('Still reading the run.'),
      "the running worker's activity on the placeholder")
    await settle()
    // A boolean, not the element: a failing assertion over a jsdom node inspects its whole graph.
    assert.equal(!!live.querySelector('.asst-status.recovering'), false,
      'the turn is being attached to, not replayed')
    assert.doesNotMatch(chat.container.textContent, /Re-running the interrupted turn/)
    assert.equal(posted(chat.calls).length, 0, 'and nothing was re-posted')
  } finally {
    await chat.unmount()
  }
})

test('a turn whose worker is gone is replayed, and says so', async () => {
  const chat = await reopenChat({ active: false, text: '', steps: [] })
  try {
    await until(() => posted(chat.calls).length === 1, 'the one exact recovery POST')
    const body = JSON.parse(posted(chat.calls)[0].body)
    assert.equal(body.display, 'Second question', 'the staged turn itself is what is re-run')
    await until(() => chat.turns().at(-1)?.querySelector('.asst-status.recovering'),
      'the replay line once the recovery is posted')
    assert.match(chat.turns().at(-1).textContent, /Re-running the interrupted turn/)
  } finally {
    chat.streams.forEach(stream => stream.close())
    await chat.unmount()
  }
})
