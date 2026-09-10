// The two fallback polls that run beside one Assistant turn (doc 25 UI-05). They used to be two bare
// `;(async () => { … })()` blocks a hundred lines apart inside `runLLM`, closing over a `let polling`
// a distant `finally` flipped — so the property that matters, that a LATE result publishes nothing,
// could not be checked at all. These drive them with fake readers and a fake clock.
import test from 'node:test'
import assert from 'node:assert/strict'

import { startTurnFallbackPolls } from '../src/assistantTurnPolls.js'

// A clock the test owns: every `sleep` resolves on the next microtask, so a loop advances as fast as
// the test lets it and never waits on a real timer.
const tick = () => Promise.resolve()

test('both polls run, publish, and end together when the turn stops', async () => {
  const pending = []
  const progress = []
  let streamed = ''
  let permissionReads = 0
  const polls = startTurnFallbackPolls({
    isCurrent: () => true,
    readPermissions: async () => {
      permissionReads += 1
      if (permissionReads > 2) polls.stop()          // two rounds is enough to prove it loops
      return { ok: true, pending: [`req-${permissionReads}`] }
    },
    onPermissions: value => pending.push(value),
    readProgress: async () => ({ active: true, text: 'thinking…' }),
    streamedText: () => streamed,
    onProgress: value => progress.push(value.text),
    sleep: tick,
  })
  await polls.settled()
  assert.deepEqual(pending[0], ['req-1'])
  assert.ok(pending.length >= 2, 'the permission poll loops rather than reading once')
  assert.ok(progress.length >= 1, 'the mirrored answer-so-far reached the bubble')
  assert.equal(streamed, '', 'the helper never writes the stream it reads')
})

test('a poll result that arrives after the turn moved on publishes nothing', async () => {
  // The defect this guards: a late /progress or /permissions response painting the DEPARTED turn's
  // text over the one the operator switched to.
  let current = true
  const seen = []
  const polls = startTurnFallbackPolls({
    isCurrent: () => current,
    readPermissions: async () => { current = false; return { ok: true, pending: ['late'] } },
    onPermissions: value => seen.push(value),
    readProgress: async () => { current = false; return { active: true, text: 'late text' } },
    streamedText: () => '',
    onProgress: value => seen.push(value.text),
    sleep: tick,
  })
  await polls.settled()
  assert.deepEqual(seen, [], 'ownership is re-checked after every await, before anything is published')
  polls.stop()
})

test('stop() is final: an in-flight read that succeeds afterwards still publishes nothing', async () => {
  const seen = []
  let release
  const inFlight = new Promise(resolve => { release = resolve })
  const polls = startTurnFallbackPolls({
    isCurrent: () => true,
    readPermissions: () => inFlight,
    onPermissions: value => seen.push(value),
    readProgress: async () => null,
    onProgress: value => seen.push(value),
    sleep: tick,
  })
  polls.stop()                                   // the turn's `finally`, while the read is open
  release({ ok: true, pending: ['too late'] })
  await polls.settled()
  assert.deepEqual(seen, [])
})

test('a throwing read is transient: the loop keeps going and nothing is published', async () => {
  const seen = []
  let reads = 0
  const polls = startTurnFallbackPolls({
    isCurrent: () => true,
    readPermissions: async () => {
      reads += 1
      if (reads >= 3) polls.stop()
      throw new Error('proxy blip')
    },
    onPermissions: value => seen.push(value),
    readProgress: async () => { throw new Error('proxy blip') },
    onProgress: value => seen.push(value),
    sleep: tick,
  })
  await polls.settled()
  assert.ok(reads >= 2, 'one failed read does not end the poll')
  assert.deepEqual(seen, [])
})

test('the progress mirror stays behind the stream it is filling in for', async () => {
  const painted = []
  let streamed = ''
  let rounds = 0
  const polls = startTurnFallbackPolls({
    isCurrent: () => true,
    readPermissions: async () => ({ ok: false }),
    onPermissions: () => { throw new Error('a not-ok snapshot must not be published') },
    readProgress: async () => {
      rounds += 1
      if (rounds === 2) streamed = 'the real tokens, longer than the mirror'  // the stream overtakes
      if (rounds >= 3) polls.stop()
      return { active: true, text: 'mirror' }
    },
    streamedText: () => streamed,
    onProgress: value => painted.push(value.text),
    sleep: tick,
  })
  await polls.settled()
  assert.deepEqual(painted, ['mirror'],
    'once the stream is longer than the mirror, the authoritative tokens are never overwritten')
})
