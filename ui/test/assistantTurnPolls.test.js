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

test('a turn in its tool rounds reaches the bubble through a buffering proxy', async () => {
  // The regression (2026-10-07): the server's `text` became the ANSWER only, with the inter-round
  // prose in `activity`, so a buffered turn's frames carry `text: ''` for every tool round — and a
  // gate keyed on "text longer than the stream" published none of them. The bubble sat on a dead
  // "thinking" for the whole turn.
  const frame = (at, prose) => ({
    active: true, text: '', steps: ['Read run r1'],
    activity: [{ type: 'text', content: prose }, { type: 'tools', labels: ['Read run r1'] }],
    last_event: at,
  })
  const frames = [
    frame(1_700_000_001, 'Looking at the run.'),
    frame(1_700_000_001, 'Looking at the run.'),       // polled again, nothing moved
    frame(1_700_000_004, 'Now the champion.'),
    frame(1_700_000_004, 'Now the champion.'),
  ]
  const painted = []
  let rounds = 0
  const polls = startTurnFallbackPolls({
    isCurrent: () => true,
    readPermissions: async () => ({ ok: false }),
    onPermissions: () => {},
    readProgress: async () => {
      const seen = frames[rounds]
      rounds += 1
      if (rounds >= frames.length) polls.stop()
      return seen
    },
    streamedText: () => '',                               // the proxy holds every SSE frame back
    onProgress: value => painted.push([value.last_event, value.activity[0].content]),
    sleep: tick,
  })
  // `stop()` lands inside the last read, so its frame is never published — the turn ended.
  await polls.settled()
  assert.deepEqual(painted, [[1_700_000_001, 'Looking at the run.'], [1_700_000_004, 'Now the champion.']],
    'every frame that moved is surfaced once, and an unchanged one is not re-published')
})

test('an activity-only frame never replaces what a working stream delivered', async () => {
  const painted = []
  let delivered = 0
  let rounds = 0
  const polls = startTurnFallbackPolls({
    isCurrent: () => true,
    readPermissions: async () => ({ ok: false }),
    onPermissions: () => {},
    readProgress: async () => {
      rounds += 1
      if (rounds === 2) delivered = 1                      // the stream delivers its first step
      if (rounds >= 4) polls.stop()
      return { active: true, text: '', steps: ['s'], activity: [{ type: 'tools', labels: ['s'] }],
        last_event: 1_700_000_000 + rounds }
    },
    streamedText: () => '',
    streamEvents: () => delivered,
    onProgress: value => painted.push(value.last_event),
    sleep: tick,
  })
  await polls.settled()
  assert.deepEqual(painted, [1_700_000_001],
    'surfaced while the stream was silent, never once it delivered a step of its own')
})
