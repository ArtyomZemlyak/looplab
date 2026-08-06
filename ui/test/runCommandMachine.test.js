import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'

import {
  clearRunCommandLock, commandEventForAction, loadAssistantRunTransport, loadRunCommandLock,
  loadRunTransport, saveAssistantRunTransport, saveRunCommandLock, saveRunTransport,
} from '../src/api.js'
import { useCommandStatusPoll } from '../src/hooks.js'
import {
  COMMAND_POLL_MAX_TRANSIENT, COMMAND_POLL_REPEAT_MS, COMMAND_POLL_START_MS,
  commandIntentPreserved, commandLockIdentity, commandLockMismatch, commandPollBackoffMs,
  foreignCommandLock, interruptedCommandRecovery, observeCommandError, protocolCommandRecord,
  restoredCommandRecord, settledCommandFailure,
} from '../src/runCommandMachine.js'

// Doc 25 UI-01. Dock and AssistantBar each own a durable, crash-recoverable run-command protocol:
// an idempotency key is staged BEFORE the request leaves, an unconfirmed submission is recovered
// after a reload, and "the command failed" is kept distinct from "the outcome is unknown". The rule
// layer under both is now one module; these tests drive the three states through the REAL durable
// storage and drive the shared poll through a REAL React render, because a rule that only holds in a
// unit fixture is not the rule the operator meets after a browser reload.

const CMD_A = `cmd_${'a'.repeat(32)}`
const CMD_B = `cmd_${'b'.repeat(32)}`
const GEN_A = 'a'.repeat(64)
const GEN_B = 'b'.repeat(64)

// A real Storage answers null for a key nobody set; anything laxer makes every load look corrupt.
const memoryStorage = () => {
  const values = new Map()
  return {
    values,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
}

// A reload is exactly this: everything in memory is gone and the surface re-reads sessionStorage.
const reload = (source, runId, storage) =>
  source === 'dock' ? loadRunTransport(runId, storage) : loadAssistantRunTransport(runId, storage)
const stage = (source, runId, state, storage) => (source === 'dock'
  ? saveRunTransport(runId, state, storage)
  : saveAssistantRunTransport(runId, state, storage))
// Both surfaces map `stop` onto the same control event, so the same envelope shape exercises both.
const eventFor = source => commandEventForAction('stop', source)

for (const source of ['dock', 'assistant']) {
  test(`${source}: an unknown command outcome survives a reload instead of settling as failed`, () => {
    const storage = memoryStorage()
    // The surface observed the command, then the status read failed in a way that leaves the OUTCOME
    // unknown (transport). The durable envelope records that ambiguity.
    assert.equal(stage(source, 'demo', {
      action: 'stop', idempotencyKey: 'key-unknown', expectedGeneration: GEN_A,
      record: { id: CMD_A, status: 'accepted', event_type: eventFor(source) },
      statusUnavailable: true, observationKind: 'transport',
    }, storage), true)

    const saved = reload(source, 'demo', storage)
    assert.ok(saved, 'the staged envelope must survive the reload')
    const record = restoredCommandRecord(saved)
    assert.equal(record.id, CMD_A, 'the durable command id is what makes re-observation possible')
    assert.equal(settledCommandFailure(saved, record), false,
      'an unknown outcome must never be presented as a settled failure')
    assert.equal(saved.statusUnavailable, true)
    // ...and the lock the two surfaces share still names this exact command, so the OTHER surface
    // cannot start a competing one while this ambiguity is open.
    assert.equal(loadRunCommandLock('demo', storage)?.commandId, CMD_A)

    // The sharpest case: the LAST record this tab saw was terminal, but the read that produced it
    // could not be verified. "failed" and "outcome unknown" are the same bytes apart from this flag.
    const ambiguous = memoryStorage()
    assert.equal(stage(source, 'demo', {
      action: 'stop', idempotencyKey: 'key-unknown-failed', expectedGeneration: GEN_A,
      record: { id: CMD_A, status: 'failed', event_type: eventFor(source),
        error: { code: 'command_failed', retryable: true } },
      statusUnavailable: true, observationKind: 'transport',
    }, ambiguous), true)
    const unresolved = reload(source, 'demo', ambiguous)
    assert.equal(unresolved.statusUnavailable, true)
    assert.equal(settledCommandFailure(unresolved, restoredCommandRecord(unresolved)), false,
      'an unverified terminal record must stay observable, not offer Retry as if it were settled')
  })

  test(`${source}: a reload mid-check/mid-retry is ambiguous, not a settled outcome`, () => {
    for (const interrupted of [{ checking: true }, { retrying: true }]) {
      const storage = memoryStorage()
      // A terminal FAILED record plus an in-flight recovery: the retry/check may already have
      // produced a new outcome that this tab never saw.
      assert.equal(stage(source, 'demo', {
        action: 'stop', idempotencyKey: 'key-interrupted', expectedGeneration: GEN_A,
        record: { id: CMD_A, status: 'failed', event_type: eventFor(source),
          error: { code: 'command_failed', retryable: true } },
        ...interrupted,
      }, storage), true)

      const saved = reload(source, 'demo', storage)
      assert.equal(interruptedCommandRecovery(saved), true, JSON.stringify(interrupted))
      assert.equal(settledCommandFailure(saved, restoredCommandRecord(saved)), false,
        'an interrupted recovery must re-observe, not offer Retry on a stale record')
    }
  })

  test(`${source}: a clean terminal failure DOES settle — the three states stay distinct`, () => {
    const storage = memoryStorage()
    assert.equal(stage(source, 'demo', {
      action: 'stop', idempotencyKey: 'key-failed', expectedGeneration: GEN_A,
      record: { id: CMD_A, status: 'failed', event_type: eventFor(source),
        error: { code: 'command_failed', retryable: true } },
    }, storage), true)

    const saved = reload(source, 'demo', storage)
    assert.equal(interruptedCommandRecovery(saved), false)
    assert.equal(settledCommandFailure(saved, restoredCommandRecord(saved)), true,
      'a failure observed with no ambiguity is actionable, not eternally pending')

    // The third state: accepted, nothing in doubt — pending, and neither failed nor unknown.
    const pendingStorage = memoryStorage()
    stage(source, 'demo', {
      action: 'stop', idempotencyKey: 'key-pending', expectedGeneration: GEN_A,
      record: { id: CMD_A, status: 'accepted', event_type: eventFor(source) },
    }, pendingStorage)
    const pending = reload(source, 'demo', pendingStorage)
    assert.equal(pending.statusUnavailable, false)
    assert.equal(interruptedCommandRecovery(pending), false)
    assert.equal(settledCommandFailure(pending, restoredCommandRecord(pending)), false)
  })
}

test('the two surfaces cannot read each other\'s staged command', () => {
  const storage = memoryStorage()
  assert.equal(saveRunTransport('demo', {
    action: 'stop', idempotencyKey: 'dock-key', expectedGeneration: GEN_A,
    record: { id: CMD_A, status: 'accepted', event_type: 'pause' },
  }, storage), true)

  // Separate namespaces: the Assistant's reload finds nothing of Dock's.
  assert.equal(loadAssistantRunTransport('demo', storage), null)
  assert.equal(loadRunTransport('demo', storage)?.commandId, CMD_A)

  // ...and the shared lock is what tells the Assistant that the silence is not an invitation. The
  // FOREIGN rule catches this, not the identity-mismatch rule — the Assistant has no envelope of its
  // own to compare, so a surface that only checked for a mismatch would sail straight past.
  const lock = loadRunCommandLock('demo', storage)
  assert.equal(lock.source, 'dock')
  assert.equal(foreignCommandLock(lock, 'assistant'), true)
  assert.equal(foreignCommandLock(lock, 'dock'), false)
  assert.equal(commandLockMismatch(lock, 'assistant', { idempotencyKey: 'other', action: 'resume',
    expectedGeneration: GEN_B, commandId: CMD_B }), false,
    'the mismatch rule is scoped to the OWN source; the foreign-lock rule is the cross-surface fence')

  // The reverse direction, through the real storage rather than by symmetry.
  const other = memoryStorage()
  saveAssistantRunTransport('demo', {
    action: 'resume', idempotencyKey: 'assistant-key', expectedGeneration: GEN_A,
    record: { id: CMD_B, status: 'accepted', event_type: 'resume' },
  }, other)
  assert.equal(loadRunTransport('demo', other), null)
  assert.equal(foreignCommandLock(loadRunCommandLock('demo', other), 'dock'), true)
})

test('the lock identity fence refuses an envelope the active lock does not name', () => {
  const identity = { idempotencyKey: 'key-1', action: 'stop', expectedGeneration: GEN_A, commandId: CMD_A }
  const lock = { source: 'dock', ...identity }
  assert.equal(commandLockMismatch(lock, 'dock', identity), false)
  for (const [field, value] of Object.entries({
    idempotencyKey: 'key-2', action: 'resume', expectedGeneration: GEN_B, commandId: CMD_B,
  })) {
    assert.equal(commandLockMismatch({ ...lock, [field]: value }, 'dock', identity), true, field)
  }
  // A lock staged before the POST returned has no id yet; that is not a mismatch, it is the normal
  // pre-submit state, and treating it as one would quarantine every command at its riskiest moment.
  assert.equal(commandLockMismatch({ ...lock, commandId: '' }, 'dock', identity), false)
  assert.equal(commandLockMismatch(lock, 'dock', { ...identity, commandId: '' }), false)
  assert.equal(commandLockMismatch(null, 'dock', identity), false)
  assert.equal(commandLockMismatch(lock, 'dock', null), false, 'no envelope, nothing to disagree with')

  // Driven, not asserted: the exact identity is what api.js requires to release the lock.
  const storage = memoryStorage()
  saveRunCommandLock('demo', { source: 'dock', ...identity, record: { status: 'accepted' } }, storage)
  assert.equal(clearRunCommandLock('demo',
    commandLockIdentity('dock', 'stop', { idempotencyKey: 'key-1', expectedGeneration: GEN_A,
      record: { id: CMD_B } }), storage), false, 'a wrong command id must not release the lock')
  assert.equal(clearRunCommandLock('demo',
    commandLockIdentity('dock', 'stop', { idempotencyKey: 'key-1', expectedGeneration: GEN_A,
      record: { id: CMD_A } }), storage), true)
})

test('observation kinds separate "outcome unknown" from "authoritative failure"', () => {
  assert.equal(observeCommandError({ status: 401 }), 'access')
  assert.equal(observeCommandError({ status: 403 }), 'access')
  assert.equal(observeCommandError({ code: 'COMMAND_PROTOCOL_ERROR' }), 'protocol')
  assert.equal(observeCommandError({ status: 404 }), 'missing')
  assert.equal(observeCommandError({ status: 503 }), 'transport')
  assert.equal(observeCommandError(new TypeError('connection reset')), 'transport')
  assert.equal(observeCommandError({ status: 400 }), 'request')
  // A protocol error is BOTH not-transient (api.js) and intent-preserving: the response could not be
  // verified, which says nothing about whether the server acted.
  for (const kind of ['transport', 'access', 'protocol']) assert.equal(commandIntentPreserved(kind), true, kind)
  for (const kind of ['missing', 'request', null, undefined]) assert.equal(commandIntentPreserved(kind), false, String(kind))
})

test('an unverifiable response keeps only a well-formed command id', () => {
  assert.deepEqual(protocolCommandRecord({ id: CMD_A, status: 'succeeded', event_type: 'pause' }),
    { commandId: CMD_A, record: { id: CMD_A, status: 'accepted' } })
  for (const bad of [null, {}, { id: 'cmd_nope' }, { id: 42 }, { id: `${CMD_A}x` }]) {
    assert.deepEqual(protocolCommandRecord(bad), { commandId: '', record: { status: 'submitting' } },
      JSON.stringify(bad))
  }
})

test('the poll cadence is a fixed contract, not whatever the constants happen to say', () => {
  // Pinned as LITERALS on purpose: every other assertion here is written in terms of these names, so
  // a widened budget or a slower first read would sail through an otherwise complete test file while
  // changing how long an operator stares at a command whose outcome nobody has reported yet.
  assert.equal(COMMAND_POLL_START_MS, 1000)
  assert.equal(COMMAND_POLL_REPEAT_MS, 1500)
  assert.equal(COMMAND_POLL_MAX_TRANSIENT, 3)
})

test('poll backoff never undercuts a server Retry-After and stays capped', () => {
  assert.equal(commandPollBackoffMs({}, 1), 1500)
  assert.equal(commandPollBackoffMs({}, 2), 3000)
  assert.equal(commandPollBackoffMs({}, 4), 6000, 'the exponential is capped')
  assert.equal(commandPollBackoffMs({}, 40), 6000)
  assert.equal(commandPollBackoffMs({ retryAfterMs: 9000 }, 1), 9000,
    'a server-asked wait wins over the client cap')
  assert.equal(commandPollBackoffMs({ retryAfterMs: 'nonsense' }, 1), 1500)
})

// ── the shared poll, driven in a real render ────────────────────────────────────────────────────

const withReactDom = async (fn) => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const timers = []
  const installed = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    IS_REACT_ACT_ENVIRONMENT: true,
    setTimeout: (callback, delay) => {
      timers.push({ callback, delay, cleared: false })
      return timers.length
    },
    clearTimeout: id => { if (timers[id - 1]) timers[id - 1].cleared = true },
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  for (const [key, value] of Object.entries(installed)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  const armed = () => timers.filter(timer => !timer.cleared)
  // Fire the newest armed timer — the poll only ever has one outstanding.
  const fire = async () => {
    const timer = armed().at(-1)
    assert.ok(timer, 'expected an armed poll timer')
    timer.cleared = true
    await React.act(async () => { await timer.callback() })
  }
  let root
  try {
    const { createRoot } = await import('react-dom/client')
    root = createRoot(document.querySelector('#root'))
    await fn({ root, timers, armed, fire })
  } finally {
    if (root) await React.act(async () => { root.unmount() })
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
}

const Harness = props => { useCommandStatusPoll(props); return null }
const accepted = { id: CMD_A, status: 'accepted' }

test('the shared poll observes at 1s, repeats at 1.5s, and stops when the surface says terminal', async () => {
  await withReactDom(async ({ root, timers, fire }) => {
    const seen = []
    let keepPolling = true
    await React.act(async () => {
      root.render(React.createElement(Harness, {
        runId: 'demo', command: accepted, paused: false,
        observe: command => { seen.push(command.id); return Promise.resolve({ ...accepted, status: 'executing' }) },
        onRecord: () => keepPolling,
        onUnobservable: () => assert.fail('no error occurred'),
        onFailed: () => assert.fail('no error occurred'),
      }))
    })
    assert.deepEqual(timers.map(timer => timer.delay), [COMMAND_POLL_START_MS])
    assert.equal(seen.length, 0, 'the first read waits out the start delay')

    await fire()
    assert.deepEqual(seen, [CMD_A])
    assert.deepEqual(timers.map(timer => timer.delay), [COMMAND_POLL_START_MS, COMMAND_POLL_REPEAT_MS])

    keepPolling = false
    await fire()
    assert.equal(seen.length, 2)
    assert.equal(timers.length, 2, 'a terminal record must not arm another tick')
  })
})

test('the shared poll spends a bounded transient budget, then reports the outcome UNKNOWN', async () => {
  await withReactDom(async ({ root, timers, fire }) => {
    const unknown = []
    let reads = 0
    await React.act(async () => {
      root.render(React.createElement(Harness, {
        runId: 'demo', command: accepted, paused: false,
        observe: () => { reads += 1; return Promise.reject(Object.assign(new Error('down'), { status: 503 })) },
        onRecord: () => assert.fail('no record was observed'),
        onUnobservable: (error, kind) => unknown.push(kind),
        onFailed: () => assert.fail('a transient read is not an authoritative failure'),
      }))
    })
    // Literal, not derived from the constants: three reads at 1s / +1.5s / +3s, then the verdict.
    await fire()
    assert.deepEqual(unknown, [])
    assert.equal(timers.at(-1).delay, 1500)
    await fire()
    assert.deepEqual(unknown, [])
    assert.equal(timers.at(-1).delay, 3000)
    await fire()
    assert.equal(reads, 3, 'the transient budget is exactly three reads')
    assert.deepEqual(unknown, ['transport'], 'the budget is exhausted; the outcome is unknown')
    assert.equal(timers.length, 3, 'giving up must not arm another tick')
  })
})

test('access/protocol reads preserve intent immediately; missing/request settle as failures', async () => {
  for (const [error, kind] of [[{ status: 403 }, 'access'], [{ code: 'COMMAND_PROTOCOL_ERROR' }, 'protocol']]) {
    await withReactDom(async ({ root, timers, fire }) => {
      const unknown = []
      await React.act(async () => {
        root.render(React.createElement(Harness, {
          runId: 'demo', command: accepted, paused: false,
          observe: () => Promise.reject(error),
          onRecord: () => assert.fail('no record'), onUnobservable: (_e, k) => unknown.push(k),
          onFailed: () => assert.fail(`${kind} must preserve the durable intent`),
        }))
      })
      await fire()
      assert.deepEqual(unknown, [kind], 'no retry budget is spent on a non-transient read')
      assert.equal(timers.length, 1)
    })
  }
  for (const [error, kind] of [[{ status: 404 }, 'missing'], [{ status: 400 }, 'request']]) {
    await withReactDom(async ({ root, fire }) => {
      const failed = []
      await React.act(async () => {
        root.render(React.createElement(Harness, {
          runId: 'demo', command: accepted, paused: false,
          observe: () => Promise.reject(error),
          onRecord: () => assert.fail('no record'),
          onUnobservable: () => assert.fail(`${kind} is authoritative for this client`),
          onFailed: (_e, k) => failed.push(k),
        }))
      })
      await fire()
      assert.deepEqual(failed, [kind])
    })
  }
})

test('the shared poll stays silent while paused or once the command is no longer observable', async () => {
  for (const props of [
    { command: accepted, paused: true },
    { command: { id: CMD_A, status: 'succeeded' }, paused: false },
    { command: { status: 'submitting' }, paused: false },
    { command: null, paused: false },
  ]) {
    await withReactDom(async ({ root, timers }) => {
      await React.act(async () => {
        root.render(React.createElement(Harness, {
          runId: 'demo', ...props,
          observe: () => assert.fail(`must not observe: ${JSON.stringify(props)}`),
          onRecord: () => true, onUnobservable: () => {}, onFailed: () => {},
        }))
      })
      assert.equal(timers.length, 0, JSON.stringify(props))
    })
  }
})

test('unmount disarms the poll and no late response can commit', async () => {
  await withReactDom(async ({ root, armed }) => {
    let settle
    const commits = []
    await React.act(async () => {
      root.render(React.createElement(Harness, {
        runId: 'demo', command: accepted, paused: false,
        observe: () => new Promise(resolve => { settle = resolve }),
        onRecord: record => { commits.push(record); return true },
        onUnobservable: () => commits.push('unknown'), onFailed: () => commits.push('failed'),
      }))
    })
    const timer = armed().at(-1)
    timer.cleared = true
    // Kick the tick off WITHOUT awaiting it: the read is deliberately left in flight.
    await React.act(async () => { timer.callback(); await Promise.resolve() })
    await React.act(async () => { root.unmount() })
    assert.deepEqual(armed(), [], 'unmount must leave no armed timer behind')
    settle({ ...accepted, status: 'executing' })
    await React.act(async () => { await Promise.resolve() })
    assert.deepEqual(commits, [], 'a response that lands after unmount must not commit')
  })
})

test('a running poll keeps the callbacks from the render that started it', async () => {
  // The hand-written effects froze their closures at effect time and used them for the life of that
  // poll. Reading the latest render's callbacks per tick would be a different machine — one where a
  // re-render mid-flight silently re-targets an in-flight observation.
  await withReactDom(async ({ root, fire }) => {
    const generations = []
    const render = generation => React.createElement(Harness, {
      runId: 'demo', command: accepted, paused: false,
      observe: () => Promise.resolve({ ...accepted, status: 'executing' }),
      onRecord: () => { generations.push(generation); return true },
      onUnobservable: () => {}, onFailed: () => {},
    })
    await React.act(async () => { root.render(render('first')) })
    // A re-render that changes nothing the effect depends on must not restart it...
    await React.act(async () => { root.render(render('second')) })
    await fire()
    assert.deepEqual(generations, ['first'],
      'the in-flight poll must keep the render generation it was started with')
  })
})

// ── neither surface may re-grow its own copy ────────────────────────────────────────────────────

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('Dock and AssistantBar drive the one shared machine and keep no private copy of it', async () => {
  const [dock, assistant, assistantCommand] = await Promise.all([
    source('Dock.jsx'), source('AssistantBar.jsx'), source('assistantCommand.js'),
  ])
  for (const [name, body] of Object.entries({ dock, assistant })) {
    assert.match(body, /import \{[\s\S]*?\} from '\.\/runCommandMachine\.js'/, name)
    assert.match(body, /useCommandStatusPoll\(\{/, `${name} must drive the shared status poll`)
    // NEGATIVE pins: what must not come back is the TEXT. Each of these was a live re-derivation of a
    // rule the other surface also owned; a commented-out copy is the same drift risk as a live one.
    assert.doesNotMatch(body, /750 \* \(2 \*\*/, `${name} re-grew the poll backoff`)
    assert.doesNotMatch(body, /timer = setTimeout\(poll,/, `${name} re-grew the poll loop`)
    assert.doesNotMatch(body, /\['transport', 'access', 'protocol'\]/,
      `${name} re-grew the intent-preserving observation set`)
    assert.doesNotMatch(body, /isTransientCommandReadError\(error\) \? 'transport' : 'request'/,
      `${name} re-grew the observation classifier`)
    assert.doesNotMatch(body, /\/\^cmd_\[0-9a-f\]\{32\}\$\//,
      `${name} re-grew the command-id shape`)
  }
  assert.doesNotMatch(assistantCommand, /isTransientCommandReadError\(error\) \? 'transport' : 'request'/,
    'assistantCommand.js kept a second copy of the observation classifier')
})
