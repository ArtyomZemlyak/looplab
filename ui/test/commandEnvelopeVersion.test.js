// The durable command envelope carries its own version, so a DEPLOY cannot kill a live command.
//
// This reader's only answer to a payload it does not fully recognise was `protocolInvalid` +
// `canResubmit: false`. The shape that produces is a deploy: ship a build that adds one envelope
// key, and a tab still running the previous build reads a perfectly valid in-flight command, fails
// `hasOnlyKeys`, and calls it a protocol violation. That is the outcome-unknown state the two-phase
// commit exists to avoid, manufactured by the client's own key set.
//
// A version distinguishes CORRUPT from NEWER-THAN-ME, which need opposite answers: corruption means
// trust nothing, while a newer protocol means the fields this build does understand — above all the
// command id — are still exactly what they claim, so the command can be re-checked rather than
// declared dead.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  COMMAND_ENVELOPE_VERSION, loadRunTransport, saveRunTransport,
} from '../src/commandStorage.js'

const RUN = 'demo'
const GENERATION = 'a'.repeat(64)
const COMMAND_ID = 'cmd_' + 'a'.repeat(32)

const memoryStorage = () => {
  const map = new Map()
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: key => map.delete(key),
    _map: map,
  }
}

const stored = storage => {
  const key = [...storage._map.keys()].find(k => k.includes(RUN))
  return key ? JSON.parse(storage._map.get(key)) : null
}

// The WRITER is exercised through its own API with the shape it accepts; the READER is exercised
// against envelopes placed in storage directly. That split is deliberate: what this change alters is
// how a payload is INTERPRETED, and building the payload by hand is the only way to express "an
// envelope a future build wrote", which no current writer can produce.
const write = (storage, extra = {}) => {
  const ok = saveRunTransport(RUN, {
    action: 'stop', idempotencyKey: 'idem-key-1', expectedGeneration: GENERATION,
    commandId: '', record: { status: 'submitting' },
    statusUnavailable: false, observationKind: null, retrying: false, checking: false,
    ...extra,
  }, storage)
  assert.ok(ok, 'the fixture must actually write an envelope')
  return storage
}

const ENVELOPE_KEY = `ll.command-transport.${RUN}`

// A complete, valid envelope carrying a server command id — the state a live in-flight command is
// in, and the one a deploy must not kill.
const liveEnvelope = (extra = {}) => ({
  v: COMMAND_ENVELOPE_VERSION,
  runId: RUN, action: 'stop', expectedGeneration: GENERATION, idempotencyKey: 'idem-key-1',
  commandId: COMMAND_ID,
  record: { id: COMMAND_ID, status: 'accepted', event_type: 'pause' },
  statusUnavailable: false, observationKind: null, retrying: false, checking: false,
  updatedAt: Date.now(), committed: true, ...extra,
})

const readEnvelope = (envelope) => {
  const storage = memoryStorage()
  storage.setItem(ENVELOPE_KEY, JSON.stringify(envelope))
  return loadRunTransport(RUN, storage)
}

test('the writer stamps the version', () => {
  const storage = write(memoryStorage())

  assert.equal(stored(storage).v, COMMAND_ENVELOPE_VERSION)
})

test('a round trip is unaffected', () => {
  // The regression this could most easily cause: `v` is in the payload and NOT in the allow-list,
  // so every envelope this build writes is refused by the build that wrote it.
  const loaded = loadRunTransport(RUN, write(memoryStorage()))

  assert.ok(loaded, 'a freshly written envelope must load at all')
  assert.ok(!loaded.protocolInvalid, 'and must not read as a protocol violation')
})

test('a complete envelope at the current version reads clean', () => {
  const loaded = readEnvelope(liveEnvelope())

  assert.equal(loaded.commandId, COMMAND_ID)
  assert.ok(!loaded.protocolInvalid)
})

test('an envelope with NO version is read exactly as before', () => {
  // THE MIGRATION, and it is the whole of it: the field is additive, so the shape that shipped
  // before it is version 1. MUTATION: require `v` -> every in-flight command written by the previous
  // build dies on the deploy that adds the field, which is this defect committed once more on the
  // way to fixing it.
  const { v, ...withoutVersion } = liveEnvelope()

  const loaded = readEnvelope(withoutVersion)

  assert.equal(loaded.commandId, COMMAND_ID)
  assert.ok(!loaded.protocolInvalid)
})

test('an envelope from a NEWER build keeps its command id instead of dying', () => {
  // THE DEFECT. A newer deploy adds a key and bumps the version; this build must not read a live
  // command as a protocol violation it can never resubmit.
  //
  // MUTATION: drop the `protocolNewer` branch -> `hasOnlyKeys` rejects the unfamiliar key and the
  // chip goes dead with `canResubmit: false` for a command that is running right now.
  const loaded = readEnvelope(liveEnvelope({
    v: COMMAND_ENVELOPE_VERSION + 1, somethingAddedLater: 'x',
  }))

  assert.equal(loaded.commandId, COMMAND_ID, 'the id is what makes a re-check possible')
  assert.equal(loaded.record.status, 'accepted')
  assert.ok(loaded.statusUnavailable, 'and the surface is told to go and look')
  assert.equal(loaded.protocolNewer, true,
    'a newer protocol is a distinct fact from corruption and must be reported as one')
})

test('genuine corruption is still refused', () => {
  // The version must not become a way to launder junk. MUTATION: trust any `v` -> a truncated or
  // hand-edited payload reads as "newer" and the reader stops checking anything.
  for (const bad of [0, -1, 1.5, 'two', null, {}]) {
    const loaded = readEnvelope(liveEnvelope({ v: bad }))

    assert.ok(loaded.protocolInvalid, `v=${JSON.stringify(bad)} must not be trusted`)
    assert.ok(!loaded.protocolNewer, 'and must not masquerade as a newer protocol')
  }
})

test('an unknown key at the CURRENT version is still a violation', () => {
  // Forward tolerance is granted by the VERSION, never by the key check going soft: at this version
  // the key set is exhaustive and an extra key means the payload was tampered with.
  const loaded = readEnvelope(liveEnvelope({ unexpected: 'x' }))

  assert.ok(loaded.protocolInvalid)
  assert.ok(!loaded.protocolNewer)
})
