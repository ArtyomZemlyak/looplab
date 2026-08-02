import test from 'node:test'
import assert from 'node:assert/strict'

import {
  clearDamagedLaunchTransport, clearLaunchTransport, listLaunchTransports,
  loadLaunchTransport, saveLaunchTransport,
} from '../src/api.js'

class MemoryStorage {
  constructor() { this.rows = new Map() }
  get length() { return this.rows.size }
  key(index) { return [...this.rows.keys()][index] ?? null }
  getItem(key) { return this.rows.has(key) ? this.rows.get(key) : null }
  setItem(key, value) { this.rows.set(key, String(value)) }
  removeItem(key) { this.rows.delete(key) }
}

test('launch recovery persists only operation identity and survives reload', () => {
  const storage = new MemoryStorage()
  const identity = 'proposal-123'
  assert.equal(saveLaunchTransport(identity, {
    runId: 'new-run', idempotencyKey: '3e525777-9fc9-46ac-9297-15b2414f4ca7',
    task: { secret: 'must-not-persist' }, chat: 'must-not-persist', validationToken: 'must-not-persist',
  }, storage), true)
  const restored = loadLaunchTransport(identity, storage)
  assert.equal(restored.runId, 'new-run')
  assert.equal(restored.idempotencyKey, '3e525777-9fc9-46ac-9297-15b2414f4ca7')
  const raw = [...storage.rows.values()].join('')
  assert.doesNotMatch(raw, /must-not-persist|validationToken|task|chat/)
  assert.deepEqual(listLaunchTransports(storage), [{
    identity, runId: 'new-run', updatedAt: restored.updatedAt, invalid: false,
  }])
  assert.doesNotMatch(JSON.stringify(listLaunchTransports(storage)), /idempotencyKey|3e525777/,
    'the global recovery indicator must never receive the observation key')
  assert.equal(clearLaunchTransport(identity, storage), true)
  assert.equal(loadLaunchTransport(identity, storage), null)
})

test('unavailable or malformed recovery storage fails closed', () => {
  const throwing = {
    getItem() { throw new DOMException('blocked', 'SecurityError') },
    setItem() { throw new DOMException('blocked', 'SecurityError') },
    removeItem() { throw new DOMException('blocked', 'SecurityError') },
  }
  assert.equal(saveLaunchTransport('proposal', {
    runId: 'run', idempotencyKey: 'key',
  }, throwing), false)
  assert.deepEqual(loadLaunchTransport('proposal', throwing), { invalid: true })
  assert.equal(clearLaunchTransport('proposal', throwing), false)

  const malformed = new MemoryStorage()
  malformed.setItem('ll.launch-transport.proposal', '{not-json')
  assert.deepEqual(loadLaunchTransport('proposal', malformed), { invalid: true })
  // A damaged entry now reports the exact storage key it lives under, so it can be cleared by that
  // key instead of by one re-derived from a decode that already failed. The deepEqual read the new
  // field as a defect; it is asserted here, and round-tripped through the clear it exists for.
  const damaged = listLaunchTransports(malformed)
  assert.deepEqual(damaged, [{
    identity: 'proposal', runId: '', updatedAt: 0, invalid: true,
    storageKey: 'll.launch-transport.proposal',
  }])
  assert.equal(clearDamagedLaunchTransport(damaged[0].storageKey, malformed), true)
  assert.deepEqual(listLaunchTransports(malformed), [],
    'a damaged record must be clearable by the key its own listing reports')
  assert.equal(clearDamagedLaunchTransport('ll.something-else.proposal', malformed), false,
    'the clear must refuse a key outside the launch-transport namespace')

  const sticky = new MemoryStorage()
  sticky.setItem('ll.launch-transport.proposal', JSON.stringify({
    identity: 'proposal', runId: 'run', idempotencyKey: 'key', updatedAt: Date.now(),
  }))
  sticky.removeItem = () => {}
  assert.equal(clearLaunchTransport('proposal', sticky), false,
    'a silently ignored removal must not unlock another paid Start')
})
