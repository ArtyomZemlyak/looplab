import { apiPrefix } from './api.js'
import {
  attentionIds, loadAttentionState, notificationsDisabledState,
  recordAttentionIds, saveAttentionState,
} from './attentionStorage.js'

const namespace = prefix => encodeURIComponent(prefix || '/')
export const attentionLockName = (prefix = apiPrefix()) => `looplab-attention:${namespace(prefix)}`
export const attentionChannelName = (prefix = apiPrefix()) => `looplab-attention-events:${namespace(prefix)}`

const eligibleIds = items => (items || []).filter(item => item?.notifyEligible === true)
  .map(item => item.id)

export async function mutateAttentionState(mutator, {
  navigatorApi = globalThis.navigator,
  storage = undefined,
  prefix = apiPrefix(),
  now = Date.now(),
  broadcast = null,
  requireLock = false,
} = {}) {
  const update = async () => {
    const loaded = loadAttentionState(storage, prefix, now)
    if (!loaded.available) {
      return { ok: false, status: 'storage-unavailable', state: loaded.state }
    }
    let state
    try { state = await mutator(loaded.state) }
    catch { return { ok: false, status: 'mutation-failed', state: loaded.state } }
    if (!saveAttentionState(state, storage, prefix, now)) {
      return { ok: false, status: 'storage-unavailable', state: loaded.state }
    }
    const verified = loadAttentionState(storage, prefix, now)
    if (!verified.available || !verified.valid) {
      return { ok: false, status: 'storage-unavailable', state: loaded.state }
    }
    broadcast?.({ type: 'invalidate', v: 1 })
    return { ok: true, status: 'saved', state: verified.state }
  }

  if (typeof navigatorApi?.locks?.request === 'function') {
    try {
      return await navigatorApi.locks.request(
        attentionLockName(prefix), { mode: 'exclusive' }, update,
      )
    } catch { return { ok: false, status: 'locks-unavailable' } }
  }
  if (requireLock) return { ok: false, status: 'locks-unavailable' }
  // In-app read/dismiss remains usable on browsers without Web Locks. Desktop delivery is disabled
  // there, so this best-effort path cannot create duplicate OS notifications.
  return update()
}

export function notificationCapability(NotificationApi = globalThis.Notification,
                                       navigatorApi = globalThis.navigator) {
  if (typeof NotificationApi !== 'function') return 'unsupported'
  if (NotificationApi.permission === 'denied') return 'denied'
  if (NotificationApi.permission === 'granted') {
    return typeof navigatorApi?.locks?.request === 'function' ? 'granted' : 'locks-unavailable'
  }
  return 'default'
}

export async function enableAttentionNotifications(items, {
  NotificationApi = globalThis.Notification,
  navigatorApi = globalThis.navigator,
  storage = undefined,
  prefix = apiPrefix(),
  now = Date.now(),
  broadcast = null,
} = {}) {
  if (typeof NotificationApi !== 'function') return { ok: false, status: 'unsupported' }
  let permission = NotificationApi.permission
  if (permission === 'default') {
    try { permission = await NotificationApi.requestPermission() }
    catch { return { ok: false, status: 'request-failed' } }
  }
  if (permission !== 'granted') return { ok: false, status: permission || 'denied' }
  if (typeof navigatorApi?.locks?.request !== 'function') {
    return { ok: false, status: 'locks-unavailable' }
  }
  const result = await mutateAttentionState(state => {
    // This explicit user gesture may replace a corrupt old preference with a clean bounded envelope.
    let next = { ...state, enabled: true, armedAt: now }
    next = recordAttentionIds(next, 'notified', eligibleIds(items), now)
    return next
  }, { navigatorApi, storage, prefix, now, broadcast, requireLock: true })
  return result.ok ? { ...result, status: 'granted' } : result
}

export async function disableAttentionNotifications({
  navigatorApi = globalThis.navigator,
  storage = undefined, prefix = apiPrefix(), now = Date.now(), broadcast = null,
} = {}) {
  const result = await mutateAttentionState(notificationsDisabledState, {
    navigatorApi, storage, prefix, now, broadcast,
  })
  return result.ok ? { ...result, status: 'disabled' } : result
}

const defaultNavigate = href => {
  if (typeof location === 'undefined' || typeof href !== 'string' || !href.startsWith('#/')) return
  location.hash = href.slice(1)
}

export async function deliverAttentionNotifications(items, {
  NotificationApi = globalThis.Notification,
  navigatorApi = globalThis.navigator,
  storage = undefined,
  prefix = apiPrefix(),
  now = Date.now(),
  broadcast = null,
  onNavigate = defaultNavigate,
  onOpenCenter = () => {},
  focusWindow = () => { try { globalThis.focus?.() } catch { /* unavailable */ } },
} = {}) {
  if (notificationCapability(NotificationApi, navigatorApi) !== 'granted') {
    return { delivered: 0, status: notificationCapability(NotificationApi, navigatorApi) }
  }
  let result = { delivered: 0, status: 'idle' }
  await navigatorApi.locks.request(attentionLockName(prefix), { mode: 'exclusive' }, async () => {
    const loaded = loadAttentionState(storage, prefix, now)
    if (!loaded.available || !loaded.valid || !loaded.state.enabled) {
      result = { delivered: 0, status: loaded.available ? 'disabled' : 'storage-unavailable' }
      return
    }
    const notified = attentionIds(loaded.state, 'notified')
    const fresh = (items || []).filter(item => item?.notifyEligible === true
      && typeof item.id === 'string' && !notified.has(item.id)
      && Number.isFinite(item.created) && item.created * 1000 >= loaded.state.armedAt)
    if (!fresh.length) return

    // Claim before presentation. A constructor failure may lose one visual alert, but can never make
    // two tabs display duplicates or expose payload while attempting to recover.
    const claimed = recordAttentionIds(loaded.state, 'notified', fresh.map(item => item.id), now)
    if (!saveAttentionState(claimed, storage, prefix, now)) {
      result = { delivered: 0, status: 'storage-unavailable' }
      return
    }
    broadcast?.({ type: 'invalidate', v: 1 })
    let notification
    try {
      notification = new NotificationApi('LoopLab needs attention', {
        body: fresh.length === 1 ? 'One new item is ready to review.'
          : `${fresh.length} new items are ready to review.`,
        tag: `looplab-attention-${fresh[0].id}`,
      })
    } catch {
      result = { delivered: 0, status: 'presentation-failed' }
      return
    }
    notification.onclick = () => {
      focusWindow()
      if (fresh.length === 1 && fresh[0].source === 'run' && fresh[0].href) onNavigate(fresh[0].href)
      else onOpenCenter()
      try { notification.close?.() } catch { /* already closed */ }
    }
    result = { delivered: fresh.length, status: 'delivered' }
  })
  return result
}

export function createAttentionChannel({ prefix = apiPrefix(), Channel = globalThis.BroadcastChannel,
  onInvalidate = () => {} } = {}) {
  if (typeof Channel !== 'function') return { broadcast() {}, close() {} }
  let channel
  try { channel = new Channel(attentionChannelName(prefix)) } catch { return { broadcast() {}, close() {} } }
  channel.onmessage = event => {
    const value = event?.data
    if (value && value.type === 'invalidate' && value.v === 1
        && Object.keys(value).length === 2) onInvalidate()
  }
  return {
    broadcast(value = { type: 'invalidate', v: 1 }) {
      if (!value || value.type !== 'invalidate' || value.v !== 1 || Object.keys(value).length !== 2) return
      try { channel.postMessage({ type: 'invalidate', v: 1 }) } catch { /* reload will reconcile */ }
    },
    close() { try { channel.close() } catch { /* already closed */ } },
  }
}
