// The polled turn progress keeps the model's prose OUT of the answer bubble (2026-10-06: a dropped
// stream showed every round's narration as the reply) and carries when the turn last moved.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  lastEventClock, progressActivity, progressHasNews, progressPatch,
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

test('news and the clock', () => {
  assert.equal(progressHasNews({ text: '', steps: [], activity: [] }), false)
  assert.equal(progressHasNews({ activity: [{ type: 'text', content: 'x' }] }), true)
  assert.equal(lastEventClock(undefined), '')
  assert.match(lastEventClock(Date.UTC(2026, 9, 7, 12, 3, 4)), /^\d\d:\d\d:\d\d$/)
})
