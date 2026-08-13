import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { COMMAND_STALL_NOTICE_MS, pendingCommandRemedy } from '../src/runCommandMachine.js'

// A PENDING COMMAND MUST NEVER READ AS FROZEN.
//
// On 2026-08-11 an operator watched `Stop requested…` for twenty minutes and then, after the command
// timed out, for a day. The server half of that is fixed — a pause now succeeds when the pause folds
// — but the chip was the other half of the report: a bare label, no elapsed time, no bound, no move.
// A surface an operator can neither act on nor see the end of is indistinguishable from a hang, and
// that is how this got debugged as one.
//
// These drive the pure model rather than the component, per the house pattern. What they hold is the
// SHAPE of the account the chip owes: how long it has been waiting, what it is waiting FOR, when the
// wait ends, and a control that is always available.

const secondsAgo = seconds => (Date.now() - seconds * 1000) / 1000
const secondsAhead = seconds => (Date.now() + seconds * 1000) / 1000

test('a command that has only just been submitted says nothing extra', () => {
  const record = { status: 'accepted', created_at: secondsAgo(1),
    absolute_deadline_at: secondsAhead(600) }
  assert.equal(pendingCommandRemedy(record, 'stop'), null,
    'a stop that lands in 300 ms must not flash a stall notice')
})

test('a command still pending after the notice threshold accounts for itself', () => {
  const record = { status: 'executing', created_at: secondsAgo(40),
    absolute_deadline_at: secondsAhead(300) }
  const remedy = pendingCommandRemedy(record, 'stop')
  assert.ok(remedy, 'past the threshold the chip owes the operator an account')
  assert.ok(remedy.elapsedMs >= COMMAND_STALL_NOTICE_MS)
  assert.ok(remedy.boundedMs > 0 && remedy.boundedMs <= 300000)
  // Naming what is being waited for is what turns "frozen" into "working" — and for a stop the
  // honest answer is the one that took a day to establish: the pause is already recorded.
  assert.match(remedy.waitingFor, /pause is recorded/)
  assert.equal(remedy.canCheck, true)
})

test('the answer is per action, because what is being waited for differs', () => {
  const at = seconds => ({ status: 'executing', created_at: secondsAgo(seconds) })
  assert.match(pendingCommandRemedy(at(40), 'finalize').waitingFor, /report, lessons and cost/)
  assert.match(pendingCommandRemedy(at(40), 'resume').waitingFor, /acknowledge the resume/)
  // …including an action this surface does not know: it must still say something true.
  assert.match(pendingCommandRemedy(at(40), 'something-else').waitingFor, /acknowledge this command/)
})

test('the bound comes from the SERVER record and is never guessed', () => {
  // `absolute_deadline_at` is what the command monitor terminalizes against, so it is the real
  // answer to "when does this stop waiting" — and it survives a reload, a second tab, and a page
  // that was closed for an hour, none of which a client-side timer does.
  const bounded = pendingCommandRemedy(
    { status: 'executing', created_at: secondsAgo(40), absolute_deadline_at: secondsAhead(120) },
    'stop')
  assert.ok(Math.abs(bounded.boundedMs - 120000) < 5000)

  // A pre-upgrade server sends no bound. Reporting that honestly is itself actionable; inventing one
  // would tell the operator a deadline nothing will honour.
  const unbounded = pendingCommandRemedy(
    { status: 'executing', created_at: secondsAgo(40) }, 'stop')
  assert.equal(unbounded.boundedMs, null)

  // An expired bound clamps at zero rather than going negative — the record is about to terminalize.
  const expired = pendingCommandRemedy(
    { status: 'executing', created_at: secondsAgo(400), absolute_deadline_at: secondsAgo(10) },
    'stop')
  assert.equal(expired.boundedMs, 0)
})

test('a terminal or unreadable record produces no notice', () => {
  for (const status of ['succeeded', 'noop', 'failed', 'timed_out', 'rejected']) {
    assert.equal(pendingCommandRemedy({ status, created_at: secondsAgo(400) }, 'stop'), null, status)
  }
  assert.equal(pendingCommandRemedy(null, 'stop'), null)
  assert.equal(pendingCommandRemedy({ status: 'executing' }, 'stop'), null,
    'without a creation time there is no elapsed time to report')
  assert.equal(pendingCommandRemedy({ status: 'executing', created_at: 'soon' }, 'stop'), null)
})

test('Dock actually renders it', async () => {
  // The model is worthless if nothing shows it, and this file cannot mount Dock (see CLAUDE.md on
  // AssistantBar's source pins). Tier 3: assert the wiring exists in the TEXT, and keep it specific
  // enough that a comment carrying the name would not satisfy it — the call, the elapsed reading and
  // the always-available control all have to be there.
  const source = await readFile(new URL('../src/Dock.jsx', import.meta.url), 'utf8')
  assert.match(source, /const pendingRemedy = pendingCommandRemedy\(/)
  assert.match(source, /pendingRemedy\.elapsedMs/)
  assert.match(source, /pendingRemedy\.boundedMs == null/)
  assert.match(source, /pendingRemedy\.canCheck && <button/)
})
