// The polled turn progress keeps the model's prose OUT of the answer bubble (2026-10-06: a dropped
// stream showed every round's narration as the reply) and carries when the turn last moved.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  extendActivity, extendAnswer, lastEventClock, progressActivity, progressHasNews, progressMovedSince,
  progressPatch, streamingProgressPatch,
} from '../src/assistantProgressModel.js'

test('the answer is `text` alone; the prose rides the ordered activity', () => {
  const frame = {
    text: 'Node 37 measured 0.1312',
    activity: [
      { type: 'text', content: 'Let me look at the runs first.' },
      { type: 'tools', labels: ['reading run v11', 'reading run v11'] },
      { type: 'text', content: 'The listing is cut.' },
    ],
    steps: ['reading run v11', 'reading run v11'],
    last_event: 1700000000.5,
  }
  const patch = progressPatch(frame, { activity: [] })
  assert.equal(patch.content, 'Node 37 measured 0.1312')
  assert.deepEqual(patch.activity, frame.activity)
  assert.equal(patch.lastEventAt, 1700000000500)
})

test('an old server (steps only) still shows its tool group, and an empty frame keeps the message', () => {
  assert.deepEqual(progressActivity({ steps: ['a', 'b'] }, []), [{ type: 'tools', labels: ['a', 'b'] }])
  const previous = [{ type: 'text', content: 'kept' }]
  assert.equal(progressActivity({ steps: [], activity: [] }, previous), previous)
  assert.equal(progressPatch({ text: '' }, { activity: previous }).content, undefined,
    'no answer yet: the bubble is not overwritten')
})

test('malformed segments are dropped, never rendered', () => {
  assert.deepEqual(progressActivity({ activity: [
    null, { type: 'text', content: '   ' }, { type: 'tools', labels: [3, ''] },
    { type: 'html', content: '<b>x</b>' }, { type: 'tools', labels: ['ok'] },
  ] }, []), [{ type: 'tools', labels: ['ok'] }])
})

test('a frame moved since the last one surfaced', () => {
  const frame = { text: '', activity: [{ type: 'tools', labels: ['a'] }], last_event: 10 }
  assert.equal(progressMovedSince(frame, null), true)
  assert.equal(progressMovedSince(frame, frame), false)
  assert.equal(progressMovedSince({ ...frame, last_event: 11 }, frame), true)
  assert.equal(progressMovedSince({ ...frame, activity: [{ type: 'tools', labels: ['a', 'b'] }] }, frame),
    true, 'a label appended to the same tool group is new activity')
  assert.equal(progressMovedSince({ text: '', activity: [], last_event: 12 }, frame), false,
    'no news never moved')
})

test('news and the clock', () => {
  assert.equal(progressHasNews({ text: '', steps: [], activity: [] }), false)
  assert.equal(progressHasNews({ activity: [{ type: 'text', content: 'x' }] }), true)
  assert.equal(lastEventClock(undefined), '')
  assert.match(lastEventClock(Date.UTC(2026, 9, 7, 12, 3, 4)), /^\d\d:\d\d:\d\d$/)
})

// ── review 2026-10-08: a live message only grows ────────────────────────────────────────────────

test('a frame surfaced mid-stream never replaces the activity the stream delivered', () => {
  // The send path surfaces a frame whose answer is longer than the stream's; the stream has already
  // delivered 45 segments, the server's window holds only its newest 40 (prose cut at 4000 chars).
  const streamedActivity = Array.from({ length: 45 },
    (_, i) => ({ type: 'text', content: `round ${i} ${'x'.repeat(5000)}` }))
  const serverWindow = streamedActivity.slice(-40).map(seg => ({ ...seg, content: seg.content.slice(0, 4000) }))
  const prev = { content: 'The answer so far', activity: streamedActivity }
  const frame = { active: true, text: 'The answer so far, and more', activity: serverWindow }
  const patch = streamingProgressPatch(frame, prev, { streamed: 'The answer so far', streamEvents: 45 })
  assert.equal(patch.activity, streamedActivity, 'the stream owns its activity once it delivered any')
  assert.equal(patch.content, 'The answer so far, and more', 'an answer that continues the stream is taken')
  // MUTATION (the old patch): the activity became the 40-segment cut window.
  assert.notDeepEqual(progressPatch(frame, prev).activity, streamedActivity)
})

test('past the server cap the answer keeps its head: the tail is spliced on, never written over it', () => {
  const answer = Array.from({ length: 2500 }, (_, i) => `w${i}`).join(' ')     // > 8000 chars
  assert.ok(answer.length > 8000)
  const head = answer.slice(0, 6000)                                             // what is shown
  const tail = answer.slice(-8000)                                               // the server's copy
  assert.ok(!tail.startsWith(head))
  const patch = streamingProgressPatch({ active: true, text: tail }, { content: head }, { streamed: head })
  assert.equal(patch.content, answer, 'the overlap places the tail after the head')
  // A tail that starts PAST what is shown cannot be placed: the gap is unknown, nothing is taken.
  const gap = streamingProgressPatch({ active: true, text: answer.slice(-2000) },
    { content: answer.slice(0, 3000) }, { streamed: answer.slice(0, 3000) })
  assert.equal(gap.content, undefined)
  // A shorter mirror never replaces longer streamed tokens.
  assert.equal(streamingProgressPatch({ active: true, text: 'The ans' }, { content: '' },
    { streamed: 'The answer' }).content, undefined)
  // MUTATION (the old patch): the tail replaced the head.
  assert.equal(progressPatch({ text: tail }, { content: head }).content, tail)
})

test('a silent stream: each polled window extends the activity it already showed', () => {
  const seg = i => ({ type: 'text', content: `step ${i}` })
  const first = Array.from({ length: 40 }, (_, i) => seg(i))
  const prev = { activity: first }
  // The window slid by two (segments 0-1 dropped server-side) and grew a tool group at its end.
  const slid = [...first.slice(2), seg(40), { type: 'tools', labels: ['read_run'] }]
  const once = streamingProgressPatch({ active: true, text: '', activity: slid }, prev)
  assert.deepEqual(once.activity, [...first, seg(40), { type: 'tools', labels: ['read_run'] }])
  // The last tool group grew in place: replaced, not appended beside itself.
  const grown = [...slid.slice(0, -1), { type: 'tools', labels: ['read_run', 'read_run_logs'] }]
  const twice = streamingProgressPatch({ active: true, text: '', activity: grown }, once)
  assert.equal(twice.activity.length, once.activity.length)
  assert.deepEqual(twice.activity.at(-1), { type: 'tools', labels: ['read_run', 'read_run_logs'] })
  assert.deepEqual(twice.activity.slice(0, 2), [seg(0), seg(1)], 'what the window dropped is kept')
  // The first frame over an empty message is taken whole; a window that overlaps nothing never
  // stands in for a longer record.
  assert.deepEqual(extendActivity([], slid), slid)
  assert.equal(extendActivity(first, [seg(99)]), first)
})

test('extendAnswer: only a continuation', () => {
  assert.equal(extendAnswer('', 'abc'), 'abc')
  assert.equal(extendAnswer('ab', 'abc'), 'abc')
  assert.equal(extendAnswer('abc', 'abc'), null)
  assert.equal(extendAnswer('abc', 'ab'), null)
  assert.equal(extendAnswer('abc', 'xyz'), null)
  assert.equal(extendAnswer('abc', ''), null)
})
