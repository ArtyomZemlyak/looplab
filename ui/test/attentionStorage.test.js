import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ATTENTION_MAX_IDS, attentionIds, attentionStorageKey, loadAttentionState, parseAttentionState,
  recordAttentionIds, saveAttentionState,
} from '../src/attentionStorage.js'

const DAY = 24 * 60 * 60 * 1000
const NOW = 2_000_000_000_000
const runId = value => value.toString(16).padStart(64, '0')
const permissionId = value => `perm_${value.toString(16).padStart(16, '0')}`

class MemoryStorage {
  constructor() { this.values = new Map() }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null }
  setItem(key, value) { this.values.set(key, String(value)) }
}

const emptyState = () => parseAttentionState(null, NOW).state

test('persisted attention state is strict, versioned, and contains only opaque id/timestamp pairs', () => {
  const valid = {
    v: 1,
    enabled: true,
    armedAt: NOW - 1_000,
    acknowledged: [[runId(1), NOW - 10]],
    dismissed: [[permissionId(2), NOW - 20]],
    notified: [],
  }
  assert.deepEqual(parseAttentionState(JSON.stringify(valid), NOW), { state: valid, valid: true })

  const invalid = [
    '{broken json',
    '[]',
    JSON.stringify({ ...valid, v: 2 }),
    JSON.stringify({ ...valid, enabled: 'true' }),
    JSON.stringify({ ...valid, enabled: true, armedAt: 0 }),
    JSON.stringify({ ...valid, rawRunId: 'customer-secret-run' }),
    JSON.stringify({ ...valid, acknowledged: [['customer-secret-run', NOW]] }),
    JSON.stringify({ ...valid, dismissed: [[permissionId(1), 'now']] }),
    JSON.stringify({ ...valid, notified: undefined }),
  ]
  for (const raw of invalid) {
    const parsed = parseAttentionState(raw, NOW)
    assert.equal(parsed.valid, false, raw)
    assert.deepEqual(parsed.state, emptyState())
  }
})

test('old and implausibly future records are pruned while duplicate ids keep the newest timestamp', () => {
  const state = {
    v: 1,
    enabled: false,
    armedAt: 0,
    acknowledged: [
      [runId(1), NOW - 91 * DAY],
      [runId(2), NOW - 10],
      [runId(2), NOW - 5],
      [runId(3), NOW + 60_001],
    ],
    dismissed: [],
    notified: [],
  }
  const parsed = parseAttentionState(JSON.stringify(state), NOW)
  assert.equal(parsed.valid, true)
  assert.deepEqual(parsed.state.acknowledged, [[runId(2), NOW - 5]])
})

test('recording is bounded to the owner-workspace envelope and ignores payload-like values', () => {
  const ids = Array.from({ length: ATTENTION_MAX_IDS + 44 }, (_, index) => runId(index))
  const state = recordAttentionIds(emptyState(), 'notified', [
    ...ids,
    'raw-run-name',
    'https://attacker.invalid/secret',
    'perm_not-hex',
  ], NOW)

  assert.equal(state.notified.length, ATTENTION_MAX_IDS)
  assert.equal(attentionIds(state, 'notified').size, ATTENTION_MAX_IDS)
  assert.doesNotMatch(JSON.stringify(state), /raw-run-name|attacker|secret|not-hex/)

  const oversized = {
    ...emptyState(), acknowledged: ids.slice(0, ATTENTION_MAX_IDS + 1).map(id => [id, NOW]),
  }
  assert.equal(parseAttentionState(JSON.stringify(oversized), NOW).valid, false)
})

test('storage is namespaced, verifies read-after-write, and fails closed when blocked', () => {
  assert.notEqual(attentionStorageKey('/alpha'), attentionStorageKey('/beta'))
  assert.match(attentionStorageKey('/owner path'), /%2Fowner%20path/)

  const storage = new MemoryStorage()
  const state = recordAttentionIds(emptyState(), 'dismissed', [permissionId(9)], NOW)
  assert.equal(saveAttentionState(state, storage, '/owner', NOW), true)
  assert.deepEqual(loadAttentionState(storage, '/owner', NOW), {
    state,
    available: true,
    valid: true,
  })
  assert.equal(loadAttentionState(storage, '/different', NOW).state.dismissed.length, 0)

  const liesOnReadback = {
    getItem() { return 'different' },
    setItem() {},
  }
  assert.equal(saveAttentionState(emptyState(), liesOnReadback, '/owner', NOW), false)

  const blocked = {
    getItem() { throw new DOMException('blocked', 'SecurityError') },
    setItem() { throw new DOMException('blocked', 'SecurityError') },
  }
  assert.doesNotThrow(() => loadAttentionState(blocked, '/owner', NOW))
  assert.deepEqual(loadAttentionState(blocked, '/owner', NOW), {
    state: emptyState(), available: false, valid: false,
  })
  assert.equal(saveAttentionState(emptyState(), blocked, '/owner', NOW), false)
})
