import test from 'node:test'
import assert from 'node:assert/strict'

import {
  createAttentionChannel, deliverAttentionNotifications, enableAttentionNotifications,
  mutateAttentionState, notificationCapability,
} from '../src/attentionNotifications.js'
import { attentionIds, loadAttentionState, recordAttentionIds } from '../src/attentionStorage.js'

const NOW = 2_000_000_000_000
const PREFIX = '/owner'
const itemId = value => value.toString(16).padStart(64, '0')

class MemoryStorage {
  constructor() { this.values = new Map() }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null }
  setItem(key, value) { this.values.set(key, String(value)) }
}

function serialLocks() {
  let tail = Promise.resolve()
  const calls = []
  return {
    calls,
    async request(name, options, callback) {
      calls.push({ name, options })
      const before = tail
      let release
      tail = new Promise(resolve => { release = resolve })
      await before
      try { return await callback() } finally { release() }
    },
  }
}

const runItem = (value, overrides = {}) => ({
  id: itemId(value),
  source: 'run',
  href: `#/run/demo?gen=${'a'.repeat(64)}&node=${value}`,
  created: NOW / 1000,
  notifyEligible: true,
  title: 'RAW_SECRET_TITLE',
  detail: 'RAW_SECRET_DETAIL',
  action: { token: 'RAW_SECRET_TOKEN' },
  ...overrides,
})

test('permission is requested only by the explicit enable operation and current items are baselined', async () => {
  let requests = 0
  let presentations = 0
  class NotificationApi {
    static permission = 'default'
    static async requestPermission() { requests += 1; return 'granted' }
    constructor() { presentations += 1 }
  }
  const locks = serialLocks()
  const navigatorApi = { locks }
  const storage = new MemoryStorage()

  assert.equal(notificationCapability(NotificationApi, navigatorApi), 'default')
  assert.equal(requests, 0)
  const passive = await deliverAttentionNotifications([runItem(1)], {
    NotificationApi, navigatorApi, storage, prefix: PREFIX, now: NOW,
  })
  assert.deepEqual(passive, { delivered: 0, status: 'default' })
  assert.equal(requests, 0, 'polling/delivery must never trigger the permission prompt')

  const enabled = await enableAttentionNotifications([
    runItem(1), runItem(2, { notifyEligible: false }),
  ], { NotificationApi, navigatorApi, storage, prefix: PREFIX, now: NOW })
  assert.equal(requests, 1)
  assert.equal(enabled.ok, true)
  assert.equal(presentations, 0, 'enabling baselines the backlog instead of displaying it')
  assert.deepEqual([...attentionIds(enabled.state, 'notified')], [itemId(1)])

  const afterBaseline = await deliverAttentionNotifications([runItem(1)], {
    NotificationApi: class GrantedNotification {
      static permission = 'granted'
      constructor() { presentations += 1 }
    },
    navigatorApi, storage, prefix: PREFIX, now: NOW,
  })
  assert.deepEqual(afterBaseline, { delivered: 0, status: 'idle' })
  assert.equal(presentations, 0)
})

test('two tabs serialize through one Web Lock and display one generic navigation-only alert', async () => {
  const storage = new MemoryStorage()
  const locks = serialLocks()
  const navigatorApi = { locks }
  const broadcasts = []
  const presented = []
  let requests = 0
  class NotificationApi {
    static permission = 'granted'
    static async requestPermission() { requests += 1; return 'granted' }
    constructor(title, options) {
      this.title = title
      this.options = options
      this.closed = false
      presented.push(this)
    }
    close() { this.closed = true }
  }

  const enabled = await enableAttentionNotifications([], {
    NotificationApi, navigatorApi, storage, prefix: PREFIX, now: NOW,
  })
  assert.equal(enabled.ok, true)
  locks.calls.length = 0

  const navigated = []
  let centerOpened = 0
  let focused = 0
  const options = {
    NotificationApi,
    navigatorApi,
    storage,
    prefix: PREFIX,
    now: NOW,
    broadcast: value => broadcasts.push(value),
    onNavigate: href => navigated.push(href),
    onOpenCenter: () => { centerOpened += 1 },
    focusWindow: () => { focused += 1 },
  }
  const fresh = runItem(7)
  const results = await Promise.all([
    deliverAttentionNotifications([fresh], options),
    deliverAttentionNotifications([fresh], options),
  ])

  assert.deepEqual(results.map(value => value.delivered).sort(), [0, 1])
  assert.equal(locks.calls.length, 2)
  assert.ok(locks.calls.every(call => call.options.mode === 'exclusive'))
  assert.equal(presented.length, 1)
  assert.equal(requests, 0)
  assert.deepEqual({ title: presented[0].title, body: presented[0].options.body }, {
    title: 'LoopLab needs attention',
    body: 'One new item is ready to review.',
  })
  assert.doesNotMatch(JSON.stringify({
    title: presented[0].title, options: presented[0].options,
  }), /RAW_SECRET|token/i)

  presented[0].onclick()
  assert.deepEqual(navigated, [fresh.href])
  assert.equal(centerOpened, 0)
  assert.equal(focused, 1)
  assert.equal(presented[0].closed, true)
  assert.ok(broadcasts.every(value => JSON.stringify(value) === '{"type":"invalidate","v":1}'))
  assert.equal(attentionIds(loadAttentionState(storage, PREFIX, NOW).state, 'notified').has(fresh.id), true)
})

test('all cross-tab preference mutations share the delivery lock and preserve concurrent fields', async () => {
  const storage = new MemoryStorage()
  const locks = serialLocks()
  const navigatorApi = { locks }
  const dismissed = itemId(31)
  const acknowledged = itemId(32)

  const results = await Promise.all([
    mutateAttentionState(state => recordAttentionIds(state, 'dismissed', [dismissed], NOW), {
      navigatorApi, storage, prefix: PREFIX, now: NOW,
    }),
    mutateAttentionState(state => recordAttentionIds(state, 'acknowledged', [acknowledged], NOW), {
      navigatorApi, storage, prefix: PREFIX, now: NOW,
    }),
  ])

  assert.ok(results.every(result => result.ok))
  assert.equal(locks.calls.length, 2)
  assert.ok(locks.calls.every(call => call.name === locks.calls[0].name
    && call.options.mode === 'exclusive'))
  const saved = loadAttentionState(storage, PREFIX, NOW).state
  assert.equal(attentionIds(saved, 'dismissed').has(dismissed), true)
  assert.equal(attentionIds(saved, 'acknowledged').has(acknowledged), true)
})

test('a batch click opens the center and blocked storage prevents presentation', async () => {
  const storage = new MemoryStorage()
  const locks = serialLocks()
  const navigatorApi = { locks }
  const presented = []
  class NotificationApi {
    static permission = 'granted'
    constructor(title, options) { this.title = title; this.options = options; presented.push(this) }
  }
  await enableAttentionNotifications([], {
    NotificationApi, navigatorApi, storage, prefix: PREFIX, now: NOW,
  })
  let opened = 0
  let navigated = 0
  const result = await deliverAttentionNotifications([runItem(1), runItem(2)], {
    NotificationApi, navigatorApi, storage, prefix: PREFIX, now: NOW,
    onNavigate: () => { navigated += 1 },
    onOpenCenter: () => { opened += 1 },
  })
  assert.deepEqual(result, { delivered: 2, status: 'delivered' })
  assert.equal(presented[0].options.body, '2 new items are ready to review.')
  presented[0].onclick()
  assert.equal(opened, 1)
  assert.equal(navigated, 0)

  const blocked = {
    getItem() { throw new DOMException('blocked', 'SecurityError') },
    setItem() { throw new DOMException('blocked', 'SecurityError') },
  }
  const denied = await deliverAttentionNotifications([runItem(3)], {
    NotificationApi, navigatorApi, storage: blocked, prefix: PREFIX, now: NOW,
  })
  assert.deepEqual(denied, { delivered: 0, status: 'storage-unavailable' })
  assert.equal(presented.length, 1)
})

test('BroadcastChannel accepts and emits only the minimal invalidation envelope', () => {
  const instances = []
  class Channel {
    constructor(name) { this.name = name; this.posts = []; this.closed = false; instances.push(this) }
    postMessage(value) { this.posts.push(value) }
    close() { this.closed = true }
  }
  let invalidations = 0
  const bridge = createAttentionChannel({
    prefix: PREFIX,
    Channel,
    onInvalidate: () => { invalidations += 1 },
  })
  const channel = instances[0]

  channel.onmessage({ data: { type: 'invalidate', v: 1, payload: 'SECRET' } })
  channel.onmessage({ data: { type: 'other', v: 1 } })
  channel.onmessage({ data: { type: 'invalidate', v: 1 } })
  assert.equal(invalidations, 1)

  bridge.broadcast({ type: 'invalidate', v: 1, payload: 'SECRET' })
  bridge.broadcast({ type: 'invalidate', v: 1 })
  assert.deepEqual(channel.posts, [{ type: 'invalidate', v: 1 }])
  bridge.close()
  assert.equal(channel.closed, true)
})
