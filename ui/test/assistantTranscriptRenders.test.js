// Review 2026-09-22, UI-06: every stream chunk re-rendered the WHOLE Assistant transcript. The
// reply streams into the last message through `setMsgs`, so each token re-runs `AssistantBar`, and
// `Turn` was a plain function handed a fresh callback (and a fresh `launchChat` array) per render —
// every settled turn re-ran, rebuilding its Markdown's inline pass and element tree, once per token
// of someone else's reply.
//
// This is the first Assistant test that MOUNTS the bar with its effects running (the harness it
// was built on is now `_mount.js::mountLive`, hoisted from here — review 2026-09-22, UI-05; the
// static `mountHarness` still runs no effects, on purpose): a real `createRoot` under jsdom
// restores a saved session through the same `assistant_get` a reload performs, opens the side
// view, sends a message through the composer, and then streams a reply chunk by chunk through a
// real SSE body. Renders are COUNTED, not inferred: React's DevTools hook is installed before
// `react-dom` loads (it is the surface the React DevTools Profiler itself reads) and each commit's
// fiber tree is walked for `Turn` fibers that did work. What is asserted is the property, stated
// as a number:
//
//   one chunk re-renders exactly one Turn — the streaming one — however long the transcript is.
//
// Before the fix the same drive measured 20 chunks × 16 messages = 320 `Turn` renders; after it, 20.
import test from 'node:test'
import assert from 'node:assert/strict'

import React from 'react'

import {
  click, fetchStub, holdFrames, jsonResponse, mountLive, settle, sseStream, tick, unanswered, until,
} from './_mount.js'

// ── the render counter ──────────────────────────────────────────────────────────────────────────
// Installed BEFORE `react-dom` is first evaluated (`_mount.js::mountLive` imports it lazily): the
// renderer looks for the hook once, at module load. The walk is the one React DevTools performs:
// descend only where this commit replaced a fiber's children (a bailed-out subtree keeps the
// previous commit's child pointer and did no work), count a mount, and otherwise count a fiber only
// when React's `PerformedWork` flag says its render function actually ran — a `React.memo` bailout
// does not set it.
const PERFORMED_WORK = 0b1
const commitListeners = new Set()
globalThis.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
  supportsFiber: true,
  renderers: new Map(),
  inject(renderer) { this.renderers.set(1, renderer); return 1 },
  onScheduleFiberRoot() {},
  onCommitFiberRoot(_rendererId, root) { for (const listener of commitListeners) listener(root) },
  onPostCommitFiberRoot() {},
  onCommitFiberUnmount() {},
}

function renderedFibers(root, matches) {
  let count = 0
  const visit = (next, previous) => {
    if (matches(next) && (previous == null || (next.flags & PERFORMED_WORK) === PERFORMED_WORK)) {
      count += 1
    }
    if (previous != null && next.child === previous.child) return
    for (let child = next.child; child; child = child.sibling) {
      visit(child, previous == null ? null : child.alternate)
    }
  }
  visit(root.current, root.current.alternate)
  return count
}

const isTurn = fiber => typeof fiber.type === 'function' && fiber.type.name === 'Turn'

// ── the browser and the server ───────────────────────────────────────────────────────────────────
const SID = '0123456789abcdef'
const META = {
  id: SID, title: 'Transcript', mode: 'plan', updated: 1_700_000_000,
  shared: false, share_count: 0, share_ids: [], live_share_ids: [],
  share_expires_at: null, share_live: false,
}

// A Genesis exchange: the proposal renders a LaunchCard, and every message after the `/new` command
// is handed a NON-EMPTY `launchChat` that the bar rebuilds as a fresh array on every render.
const GENESIS = [
  { role: 'user', content: '/new tune the baseline', turn_id: 'g0', mode: 'plan' },
  { role: 'assistant', turn_id: 'g0', content: 'Here is a launch card for the baseline.',
    proposals: [{ proposal_id: 'p1', run_id: 'tune-baseline', rationale: 'Start from the toy task.',
      task: { kind: 'quadratic', goal: 'min (x-3)^2', direction: 'min' },
      settings: { backend: 'toy' } }] },
]

// A settled transcript with the shapes a Turn renders differently: Markdown prose, a run mention,
// persisted tool steps and an applied file change carrying an exact undo receipt.
function settledTranscript(exchanges) {
  const messages = []
  for (let index = 0; index < exchanges; index += 1) {
    messages.push({ role: 'user', content: `Question ${index}: what changed in run r${index}?`,
      turn_id: `t${index}`, mode: 'plan' })
    messages.push({
      role: 'assistant', turn_id: `t${index}`,
      content: `## Answer ${index}\n\nThe **best** node in @run:r${index} improved.\n\n`
        + '- a list item\n- another item with `code`\n',
      steps: [{ label: `Read run r${index}` }],
      ...(index === 1 ? { applied: [{
        tool: 'write_file', label: 'Wrote notes.md', abs_path: '/work/notes.md',
        recovery_id: 'r'.repeat(32), recovery_postimage_exists: true,
        recovery_postimage_digest: 'a'.repeat(64), recovery_postimage_mode: 0o644,
      }] } : {}),
    })
  }
  return messages
}

// The drive's server, on the shared path-keyed stub (`_mount.js::fetchStub`): the saved chat is
// read back, the permission and progress fallback polls are left unanswered (they settle only when
// their own signal aborts, so they can never publish a state change mid-count), every turn opens a
// live SSE body the test writes chunk by chunk, and a revert is held until the test releases it.
function server({ transcript, meta = META }) {
  const streams = []
  const revert = { pending: [] }
  const fetch = fetchStub({
    'GET /api/assistant/commands': { commands: [] },
    'GET /api/assistant/sessions': { sessions: [meta] },
    [`GET /api/assistant/sessions/${SID}`]: { messages: transcript, meta },
    'GET /api/assistant/watches': { watches: [] },
    'GET /api/runs': [{ run_id: 'r0', phase: 'running', engine_running: true, best_metric: 0.5 }],
    'GET /api/assistant/permissions': ({ init }) => unanswered(init),
    'GET /api/assistant/progress': ({ init }) => unanswered(init),
    [`POST /api/assistant/sessions/${SID}/message_stream`]: () => {
      const stream = sseStream()
      streams.push(stream)
      return stream.response()
    },
    'POST /api/assistant/revert': () => new Promise(resolve => revert.pending.push(
      () => resolve(jsonResponse({ ok: true })))),
  })
  return { fetch, calls: fetch.calls, streams, revert }
}

// The live harness (`_mount.js::mountLive`) imports `react-dom/client` itself, lazily — after the
// DevTools hook above is in place, which is what the render counter needs.
let harness
let AssistantBar

test.before(async () => {
  harness = await mountLive()
  ;({ default: AssistantBar } = await harness.load('/src/AssistantBar.jsx'))
})

test.after(async () => {
  await harness?.close()
})

async function mountRestoredChat({ transcript, meta }) {
  const backend = server({ transcript, meta })
  globalThis.fetch = backend.fetch
  localStorage.clear()
  sessionStorage.clear()
  localStorage.setItem('ll.asstSid', SID)
  const mounted = await harness.mount(AssistantBar, { runId: null })
  const { container } = mounted
  await until(() => backend.calls.some(call => call.path === `/api/assistant/sessions/${SID}`),
    'the saved chat to be read back')
  await settle()
  const sideButton = container.querySelector('button.cmdbar-drawer-btn')
  assert.ok(sideButton, 'the bar offers the side view')
  await React.act(async () => {
    sideButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await settle()
  const turns = () => [...container.querySelectorAll('.feed-msg.chat')]
  await until(() => turns().length === transcript.length, 'the restored transcript to render')
  return {
    backend, container, turns,
    // The SAME root re-rendered with new props — an update, never a remount, so React compares
    // this render's hooks with the previous one's.
    rerender: props => mounted.rerender({ runId: null, ...props }),
    unmount: () => mounted.unmount(),
  }
}

async function sendFromComposer(container, text) {
  const textarea = container.querySelector('textarea[aria-label="Assistant message"]')
  assert.ok(textarea, 'the side view has its own composer')
  await React.act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
      .set.call(textarea, text)
    textarea.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
  const send = [...container.querySelectorAll('button')].find(button => button.textContent === 'Send')
  assert.ok(send && !send.disabled,
    'Send is enabled once the restored chat and its share terms are known')
  await click(send)
}

// Send one message and stream its reply through the terminal frame. Returns how many fibers
// `counted` accepts rendered from the first token through that frame.
async function streamReply(chat, text, words, counted) {
  const opened = chat.backend.streams.length
  const before = chat.turns().length
  await sendFromComposer(chat.container, text)
  await until(() => chat.backend.streams.length === opened + 1, 'the turn to open its stream')
  await until(() => chat.turns().length === before + 2, 'the user turn and its reply placeholder')
  await settle()
  const stream = chat.backend.streams[opened]
  let renders = 0
  const listener = root => { renders += renderedFibers(root, counted) }
  commitListeners.add(listener)
  try {
    for (const word of words) {
      await React.act(async () => { stream.send('token', { text: `${word} ` }); await tick() })
    }
    await React.act(async () => {
      stream.send('done', { reply: words.join(' '), steps: [], applied: [], proposals: [], todos: [] })
      await tick()
    })
    await settle()
  } finally {
    commitListeners.delete(listener)
  }
  return renders
}

test('a stream chunk re-renders only the streaming Turn, not the settled transcript', async () => {
  const transcript = [...GENESIS, ...settledTranscript(6)]
  const chat = await mountRestoredChat({ transcript })
  try {
    assert.ok(chat.turns()[1].querySelector('form.asst-launch'), 'the proposal renders its launch card')
    await sendFromComposer(chat.container, 'Summarize the last run')
    await until(() => chat.backend.streams.length === 1, 'the turn to open its stream')
    await until(() => chat.turns().length === transcript.length + 2,
      'the optimistic user turn and the streaming placeholder')
    await settle()
    const [stream] = chat.backend.streams

    let turnRenders = 0
    let commits = 0
    const listener = root => { commits += 1; turnRenders += renderedFibers(root, isTurn) }
    commitListeners.add(listener)
    const CHUNKS = 20
    let streamed = ''
    try {
      for (let chunk = 0; chunk < CHUNKS; chunk += 1) {
        const text = `word${chunk} `
        streamed += text
        await React.act(async () => {
          stream.send('token', { text })
          await tick()
        })
      }
    } finally {
      commitListeners.delete(listener)
    }

    const live = chat.turns().at(-1)
    assert.match(live.textContent, new RegExp(streamed.trim()), 'every chunk reached the live bubble')
    assert.equal(commits, CHUNKS, 'the drive delivers one commit per chunk, and nothing else commits')
    // The measured property. A settled turn's props are identity-stable across a chunk, so a chunk
    // costs ONE Turn render — the streaming one — instead of one per message in the transcript.
    const messages = transcript.length + 2
    assert.equal(turnRenders, CHUNKS,
      `${CHUNKS} chunks into a ${messages}-message transcript rendered ${turnRenders} Turns`)

    // Nothing the settled transcript SHOWS moved while it was not re-rendered.
    const settled = chat.turns().slice(0, transcript.length)
    assert.ok(settled[1].querySelector('form.asst-launch'), 'the launch card is still mounted')
    assert.equal(settled[2].querySelector('.chat-text').textContent, transcript[2].content)
    assert.match(settled[3].querySelector('.chat-bubble').textContent,
      /The best node in @run:r0 improved\./)
    assert.equal(settled[3].querySelector('.asst-runchip b').textContent, 'r0')

    // The terminal frame settles the live turn exactly as before.
    await React.act(async () => {
      stream.send('done', { reply: streamed.trim(), steps: [], applied: [], proposals: [], todos: [] })
      await tick()
    })
    await settle()
    const done = chat.turns().at(-1)
    assert.equal(!!done.querySelector('.asst-cursor'), false, 'the finished reply drops its cursor')
    assert.match(done.querySelector('.chat-bubble').textContent, new RegExp(streamed.trim()))
  } finally {
    await chat.unmount()
  }
})

// The other half of the fix is what a settled Turn now HOLDS: stable faces instead of the closure of
// its last render. A face that reached a stale closure — or none — would leave a button that looks
// right and does nothing, which no render count can see. So a whole reply first streams past the
// settled Turns without re-rendering one of them (counted, through the terminal frame), and then
// every face they hold is pressed and what it did is read off the server or the location.
test('the handlers a settled Turn holds still act, through the latest render', async () => {
  const transcript = settledTranscript(3)
  // The last exchange failed: a rate-limited reply is a retryable error card, and its user turn
  // carries the persisted raw instruction and mode an exact Retry must resend.
  transcript[4] = { ...transcript[4], raw: 'Question 2 [UI context: run "r2" is open.]' }
  transcript[5] = { role: 'assistant', turn_id: 't2', error_kind: 'rate_limit',
    content: 'Assistant error: temporarily rate-limited. Error code: 429.' }
  const chat = await mountRestoredChat({ transcript })
  try {
    const settledTurn = fiber => isTurn(fiber)
      && fiber.memoizedProps.launchMessageIndex < transcript.length
    assert.equal(await streamReply(chat, 'First, a normal question', ['one', 'two', 'three'],
      settledTurn), 0, 'no settled Turn re-rendered from the first token through the terminal frame')
    assert.equal(chat.turns().length, transcript.length + 2)
    assert.match(chat.turns().at(-1).querySelector('.chat-bubble').textContent, /one two three/)

    // Undo (the face over `onRevert`), and `revertState`, which is READ during render: the button
    // must follow the revert through both of its states, so the settled Turn re-renders for them.
    const undo = chat.turns()[3].querySelector('button.asst-undo')
    assert.equal(undo?.textContent, 'undo')
    await click(undo)
    const revert = chat.backend.calls.find(call => call.path === '/api/assistant/revert')
    assert.deepEqual(JSON.parse(revert.body), {
      path: '/work/notes.md', recovery_id: 'r'.repeat(32),
      expected_postimage: { exists: true, digest: 'a'.repeat(64), mode: 0o644 },
    }, 'the exact receipt of the change the button is on')
    await settle()
    assert.equal(chat.turns()[3].querySelector('button.asst-undo').textContent, 'reverting…')
    assert.equal(chat.turns()[3].querySelector('button.asst-undo').disabled, true)
    await React.act(async () => { chat.backend.revert.pending.shift()(); await tick() })
    await settle()
    assert.equal(chat.turns()[3].querySelector('button.asst-undo').textContent, 'reverted')

    // A run link (the face over `openRunFromAssistant`): an ordinary click is routed in-app, which
    // `preventDefault` proves — the browser's own hash navigation would move `location` too.
    const link = chat.turns()[1].querySelector('a.asst-runchip')
    assert.equal(link.getAttribute('href'), '#/run/r0')
    const linkClick = await click(link)
    assert.equal(linkClick.defaultPrevented, true, 'the handler ran and took the navigation')
    assert.equal(location.hash, '#/run/r0')

    // Retry (the per-message face): it must retry THIS message's own user turn, exactly as saved.
    const retry = [...chat.turns()[5].querySelectorAll('button')].find(b => b.textContent === 'Retry')
    assert.ok(retry, 'a rate-limited reply offers Retry')
    await click(retry)
    await until(() => chat.backend.streams.length === 2, 'the retried turn to open its stream')
    const resent = chat.backend.calls.filter(call => call.path.endsWith('/message_stream'))[1]
    assert.deepEqual(JSON.parse(resent.body), {
      instruction: transcript[4].raw, mode: 'plan', acknowledged_live_share_ids: [],
      display: transcript[4].content,
    })
    await React.act(async () => {
      chat.backend.streams[1].send('done', { reply: 'Answer 2, retried.', steps: [], applied: [] })
      await tick()
    })
    await settle()

    // Open Settings (the face over `openAssistantSettings`): the Assistant folds to the bar and
    // hands the route over only once it has.
    const settings = [...chat.turns()[5].querySelectorAll('button')]
      .find(b => b.textContent === 'Open Settings')
    await click(settings)
    await settle()
    // Booleans, never DOM nodes, in a failing assertion: the reporter inspects `actual`, and a
    // jsdom element drags the whole window's object graph into that print.
    assert.equal(!!chat.container.querySelector('.asst-side-panel'), false, 'folded to the bar')
    assert.equal(location.hash, '#/settings')
  } finally {
    await chat.unmount()
  }
})

// The transcript's autoscroll is an animation frame the COMMIT schedules, so it runs after the commit
// that asked for it — by then the feed may be gone: folded to the bar, or the whole bar unmounted. It
// read `feedRef.current.scrollHeight` unguarded, and a null there is a TypeError thrown out of a bare
// frame, where nothing catches it (review 2026-09-22 follow-up to UI-06). Held frames make "the feed
// left before the frame ran" a deterministic step instead of a race with the event loop.
test('an autoscroll frame that outlives its feed does nothing instead of throwing', async () => {
  const chat = await mountRestoredChat({ transcript: settledTranscript(2) })
  const frames = holdFrames()
  try {
    await sendFromComposer(chat.container, 'Keep talking')
    await until(() => chat.backend.streams.length === 1, 'the turn to open its stream')
    await settle()
    for (const frame of frames.take()) frame(0)   // with the feed still there: these scroll it
    const [stream] = chat.backend.streams
    const chunkFrame = async () => {
      await React.act(async () => { stream.send('token', { text: 'more ' }); await tick() })
      const scheduled = frames.take()
      assert.equal(scheduled.length, 1,
        'a chunk schedules exactly one frame: the transcript autoscroll')
      return scheduled[0]
    }

    // 1. Folded to the bar before the frame runs: the side panel, and its feed, are unmounted.
    const beforeFold = await chunkFrame()
    await click(chat.container.querySelector('button[title="collapse to the bar"]'))
    assert.equal(!!chat.container.querySelector('[role="log"]'), false, 'no feed on the bar')
    assert.doesNotThrow(() => beforeFold(0), 'a frame whose feed was folded away does nothing')
    for (const frame of frames.take()) frame(0)   // the fold's own focus frame

    // 2. Unmounted before the frame runs: the same frame, with no component left at all.
    await click(chat.container.querySelector('button.cmdbar-drawer-btn'))
    assert.equal(!!chat.container.querySelector('[role="log"]'), true, 'the feed is back')
    for (const frame of frames.take()) frame(0)
    const beforeUnmount = await chunkFrame()
    await chat.unmount()
    assert.doesNotThrow(() => beforeUnmount(0), 'a frame that outlived the whole bar does nothing')
  } finally {
    frames.release()
    await chat.unmount()
  }
})

// `hidden` returned early ABOVE three effects (the watch poll and the two share-expiry timers), so a
// bar whose `hidden` flipped while mounted called a different number of hooks than the render before
// it: React's hook-order invariant, which takes the whole Assistant down (review 2026-09-22
// follow-up). What a hidden bar RUNS must not change either: one hidden from its first render never
// ran those three, so a bar hidden later stops them, and showing it again restarts them. Both are
// read off the server, because each restart is a request: the watch poll reads at once, and a share
// whose expiry has already passed makes the expiry effect re-read the session list at once.
const EXPIRED_SHARE_META = {
  ...META, shared: true, share_count: 1, share_ids: ['c'.repeat(32)], live_share_ids: [],
  share_expires_at: 1, share_live: false,
}

test('flipping `hidden` keeps the hooks and the state, and a hidden bar runs nothing', async () => {
  const chat = await mountRestoredChat({
    transcript: settledTranscript(2), meta: EXPIRED_SHARE_META,
  })
  const hookErrors = []
  const logError = console.error
  console.error = (...args) => {
    const text = args.map(String).join(' ')
    if (/Rendered (fewer|more) hooks|change in the order of Hooks/i.test(text)) hookErrors.push(text)
    logError(...args)
  }
  const reads = path => chat.backend.calls.filter(call => call.method === 'GET' && call.path === path)
    .length
  try {
    await settle()
    const watches = reads('/api/assistant/watches')
    const sessions = reads('/api/assistant/sessions')
    assert.ok(watches >= 1 && sessions >= 1, 'the visible bar polls its watches and re-reads the list')

    await chat.rerender({ hidden: true })
    await settle()
    assert.equal(chat.container.innerHTML, '', 'hidden renders nothing at all')
    assert.equal(reads('/api/assistant/watches'), watches, 'a hidden bar starts no watch read')
    assert.equal(reads('/api/assistant/sessions'), sessions, 'and arms no share-expiry re-read')

    await chat.rerender({ hidden: false })
    await settle()
    assert.equal(!!chat.container.querySelector('.asst-side-panel'), true,
      'shown again in the view it had, not remounted into the bar')
    assert.equal(chat.turns().length, 4, 'with the transcript it had')
    assert.equal(reads('/api/assistant/watches'), watches + 1, 'its watch poll restarts')
    assert.equal(reads('/api/assistant/sessions'), sessions + 1, 'and its share-expiry effect re-arms')
    assert.deepEqual(hookErrors, [], 'React saw the same hooks on every render')
  } finally {
    console.error = logError
    await chat.unmount()
  }
})
