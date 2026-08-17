import test from 'node:test'
import assert from 'node:assert/strict'

import {
  WATCH_TERMINAL, budgetText, describeWatch, isTerminal, needsAttention, nextCheckText,
  statusText, stripPollMs, watchStrip,
} from '../src/assistantWatchModel.js'

const NOW = 1_000_000

function watch(over = {}) {
  return {
    id: 'a'.repeat(16), session: 's1', status: 'armed', mode: 'plan',
    instruction: 'tell me the champion', waiting_for: 'run demo to reach finished',
    trigger: { kind: 'run_state', run: 'demo', until: ['finished'] },
    created: NOW - 100, updated: NOW - 10, next_due: NOW + 30,
    wakeups: 0, max_wakeups: 24, last_error: '', ...over,
  }
}

test('the terminal set mirrors the server, and an UNKNOWN status is not terminal', () => {
  assert.deepEqual([...WATCH_TERMINAL].sort(),
    ['blocked', 'cancelled', 'done', 'expired', 'failed', 'interrupted'])
  // The failure mode is hiding a LIVE watch, not showing a dead one: a status from a newer server
  // must keep its row visible.
  assert.equal(isTerminal({ status: 'quiescing' }), false)
  assert.equal(isTerminal({ status: 'done' }), true)
})

test('a waiting watch is never confusable with a stopped one', () => {
  assert.equal(statusText(watch()), 'waiting')
  assert.equal(statusText(watch({ status: 'cancelled' })), 'stopped')
  assert.equal(statusText(watch({ status: 'waking' })), 'running now')
  // An unknown status still prints SOMETHING rather than an empty cell.
  assert.equal(statusText({ status: 'quiescing' }), 'quiescing')
  assert.equal(statusText({}), 'unknown')
})

test('blocked and interrupted watches ask the operator for attention', () => {
  assert.equal(needsAttention(watch({ status: 'interrupted' })), true)
  assert.equal(needsAttention(watch({ status: 'blocked' })), true)
  for (const status of ['armed', 'waking', 'done', 'cancelled', 'expired', 'failed']) {
    assert.equal(needsAttention(watch({ status })), false, status)
  }
})

test('the next check reads as a duration, never a bare timestamp', () => {
  assert.equal(nextCheckText(watch({ next_due: NOW + 0.5 }), NOW), 'now')
  assert.equal(nextCheckText(watch({ next_due: NOW + 45 }), NOW), 'in 45s')
  assert.equal(nextCheckText(watch({ next_due: NOW + 720 }), NOW), 'in 12m')
  assert.equal(nextCheckText(watch({ next_due: NOW + 7200 }), NOW), 'in 2h')
  // A terminal watch has no next check, and an unreadable one must not print "in NaNs".
  assert.equal(nextCheckText(watch({ status: 'done' }), NOW), '')
  assert.equal(nextCheckText(watch({ next_due: null }), NOW), '')
  assert.equal(nextCheckText(null, NOW), '')
})

test('a budget is shown only where it can actually move', () => {
  // A one-shot run-state watch fires once by construction; "0/24" would read as "it will keep
  // waking", which is the opposite of true.
  assert.equal(budgetText(watch()), '')
  assert.equal(budgetText(watch({ trigger: { kind: 'schedule', every_s: 60 }, wakeups: 3 })),
    '3/24 wake-ups')
  assert.equal(budgetText(watch({ trigger: { kind: 'work', every_s: 60 }, wakeups: 3 })),
    '3/24 cycles')
  assert.equal(budgetText(watch({ trigger: { kind: 'schedule' }, max_wakeups: 0 })), '')
})

test('a row carries the sentence the SERVER stamped, never a re-derived one', () => {
  const row = describeWatch(watch({ waiting_for: 'every 10 min' }), NOW)
  assert.equal(row.waitingFor, 'every 10 min')
  // …and it never invents one either.
  assert.equal(describeWatch(watch({ waiting_for: '  ' }), NOW).waitingFor, 'an unstated condition')
})

test('a row says what it will DO when it wakes', () => {
  assert.equal(describeWatch(watch(), NOW).instruction, 'tell me the champion')
  assert.equal(describeWatch(watch({ checkpoint: { summary: 'tests are green' } }), NOW).checkpoint,
    'tests are green')
})

test('the strip hides itself when nothing is standing', () => {
  const strip = watchStrip([], { now: NOW })
  assert.equal(strip.visible, false)
  assert.equal(strip.items.length, 0)
  assert.equal(strip.summary, 'no standing watches')
})

test('active watches come first, in arm order, and do not reshuffle on a poll', () => {
  const rows = [
    watch({ id: 'c'.repeat(16), created: NOW - 10 }),
    watch({ id: 'b'.repeat(16), created: NOW - 500, status: 'done', updated: NOW - 5 }),
    watch({ id: 'a'.repeat(16), created: NOW - 900 }),
  ]
  const first = watchStrip(rows, { now: NOW })
  assert.deepEqual(first.items.map(i => i.id), ['a'.repeat(16), 'c'.repeat(16), 'b'.repeat(16)])
  assert.equal(first.activeCount, 2)
  assert.equal(first.summary, '2 standing watches')
  // The same rows in a different arrival order produce the same list.
  const shuffled = watchStrip([rows[2], rows[0], rows[1]], { now: NOW })
  assert.deepEqual(shuffled.items.map(i => i.id), first.items.map(i => i.id))
})

test('a settled watch ages out of the strip but an attention watch never does', () => {
  const stale = watch({ status: 'done', updated: NOW - 5000 })
  assert.equal(watchStrip([stale], { now: NOW }).items.length, 0,
    'a watch that already reported is history the transcript holds better')

  const interrupted = watch({ status: 'interrupted', updated: NOW - 5_000_000 })
  const strip = watchStrip([interrupted], { now: NOW })
  assert.equal(strip.items.length, 1, 'a request for the operator must not expire on a timer')
  assert.equal(strip.attentionCount, 1)
  assert.equal(strip.activeCount, 0, 'it is not still watching, and must not claim to be')
  assert.equal(strip.items[0].stoppable, false)

  const blocked = watch({ status: 'blocked', updated: NOW - 5_000_000 })
  assert.equal(watchStrip([blocked], { now: NOW }).items.length, 1)
})

test('one watch reads as singular', () => {
  assert.equal(watchStrip([watch()], { now: NOW }).summary, '1 standing watch')
})

test('a malformed row is dropped rather than rendered as a blank', () => {
  const strip = watchStrip([null, {}, { id: '' }, watch()], { now: NOW })
  assert.equal(strip.items.length, 1)
})

test('the strip polls slowly when nothing is armed — the tab is not what watches', () => {
  assert.equal(stripPollMs(watchStrip([watch()], { now: NOW })), 5000)
  assert.equal(stripPollMs(watchStrip([], { now: NOW })), 30000)
  assert.equal(stripPollMs(null), 30000)
})

test('a non-array is handled, because an error envelope is not a list', () => {
  assert.equal(watchStrip(undefined).visible, false)
  assert.equal(watchStrip({ error: 'nope' }).visible, false)
})
