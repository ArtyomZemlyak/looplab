import test from 'node:test'
import assert from 'node:assert/strict'

import {
  clearLaunchTransport, listLaunchTransports, loadLaunchTransport, saveLaunchTransport,
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
  assert.deepEqual(listLaunchTransports(malformed), [{
    identity: 'proposal', runId: '', updatedAt: 0, invalid: true,
  }])

  const sticky = new MemoryStorage()
  sticky.setItem('ll.launch-transport.proposal', JSON.stringify({
    identity: 'proposal', runId: 'run', idempotencyKey: 'key', updatedAt: Date.now(),
  }))
  sticky.removeItem = () => {}
  assert.equal(clearLaunchTransport('proposal', sticky), false,
    'a silently ignored removal must not unlock another paid Start')
})
