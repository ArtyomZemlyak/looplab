const DEFAULT_BLOCK_REASON = 'Settings are not ready for a new run.'
const RETAINED_BLOCK_REASON = 'Settings server state needs review. Open Settings and refresh or resolve the saved configuration before validating or starting a run.'
const PENDING_BLOCK_REASON = 'An earlier Settings operation is still finishing. Wait for it to settle, then refresh Settings before validating or starting a run.'
const RETAINED_KEY_PREFIX = 'looplab.settings-launch-guard.v2'

const INACTIVE_SNAPSHOT = Object.freeze({
  active: false,
  blocked: false,
  status: 'inactive',
  reason: '',
})

const RETAINED_SNAPSHOT = Object.freeze({
  active: true,
  blocked: true,
  status: 'reconcile-required',
  reason: RETAINED_BLOCK_REASON,
})

const PENDING_SNAPSHOT = Object.freeze({
  active: true,
  blocked: true,
  status: 'operation-pending',
  reason: PENDING_BLOCK_REASON,
})

const listeners = new Set()
const operations = new Map()
let publisherSequence = 0
let operationSequence = 0
let operationEpoch = 0
let publisher = null
let reconciledPublisher = null

const cleanText = (value, fallback = '') => {
  const text = typeof value === 'string' ? value.trim() : ''
  return text || fallback
}

const storageScope = () => typeof window === 'undefined'
  ? '' : `${window.location.origin}${window.location.pathname}`
const storageKey = () => `${RETAINED_KEY_PREFIX}:${encodeURIComponent(storageScope())}`

const readRetainedFence = () => {
  if (typeof window === 'undefined') return { retained: false, uncertain: false }
  try {
    const raw = window.sessionStorage.getItem(storageKey())
    if (raw == null) return { retained: false, uncertain: false }
    const value = JSON.parse(raw)
    if (value?.version === 2 && value.scope === storageScope()) {
      return { retained: true, uncertain: false }
    }
    return { retained: true, uncertain: true }
  } catch { return { retained: true, uncertain: true } }
}

const durableState = readRetainedFence()
let retainedFence = durableState.retained
let storageUncertain = durableState.uncertain
let orphanedFence = retainedFence
let reconciliationRequired = retainedFence
let snapshot = retainedFence ? RETAINED_SNAPSHOT : INACTIVE_SNAPSHOT

const writeRetainedFence = () => {
  retainedFence = true
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(storageKey(), JSON.stringify({
      version: 2,
      scope: storageScope(),
      createdAt: Date.now(),
    }))
    storageUncertain = false
  } catch { storageUncertain = true }
}

const clearRetainedFence = () => {
  if (typeof window === 'undefined') {
    retainedFence = false
    storageUncertain = false
    return true
  }
  try {
    window.sessionStorage.removeItem(storageKey())
    retainedFence = false
    storageUncertain = false
    orphanedFence = false
    return true
  } catch {
    retainedFence = true
    storageUncertain = true
    return false
  }
}

const commit = next => {
  if (snapshot.active === next.active && snapshot.blocked === next.blocked
      && snapshot.status === next.status && snapshot.reason === next.reason) return snapshot
  snapshot = next
  for (const listener of [...listeners]) listener()
  return snapshot
}

const recoverySnapshot = () => operations.size > 0 ? PENDING_SNAPSHOT : RETAINED_SNAPSHOT

// useSyncExternalStore requires getSnapshot() to retain object identity until the store actually
// changes. Canonical snapshots and equality suppression keep consumers out of render loops.
export function getSnapshot() {
  return snapshot
}

export function subscribe(listener) {
  if (typeof listener !== 'function') throw new TypeError('Settings launch guard listener must be a function')
  listeners.add(listener)
  return () => listeners.delete(listener)
}

// A generation-scoped publisher prevents a late StrictMode/HMR cleanup from clearing a newer
// Settings instance's fence.
export function claimPublisher() {
  const previousPublisher = publisher
  const token = Object.freeze({ id: ++publisherSequence })
  if (previousPublisher) {
    for (const operation of operations.values()) {
      if (operation.owner === previousPublisher) operation.detached = true
    }
  }
  publisher = token
  reconciledPublisher = null
  if (retainedFence || operations.size > 0) reconciliationRequired = true
  return token
}

// A write lease lives outside React. A new Settings mount can read only after every older lease has
// settled, and a read started before settlement is rejected by the epoch check below.
export function beginOperation(owner, kind = 'settings-write') {
  if (owner !== publisher || reconciliationRequired || reconciledPublisher !== owner
      || operations.size > 0) {
    reconciliationRequired = true
    writeRetainedFence()
    commit(RETAINED_SNAPSHOT)
    return null
  }
  const lease = Object.freeze({ id: ++operationSequence })
  operations.set(lease, { owner, kind: cleanText(kind, 'settings-write'), detached: false })
  operationEpoch += 1
  reconciliationRequired = true
  reconciledPublisher = null
  orphanedFence = false
  writeRetainedFence()
  return lease
}

export function settleOperation(lease, { resolved = false } = {}) {
  const operation = operations.get(lease)
  if (!operation) return false
  operations.delete(lease)
  operationEpoch += 1
  const authoritativeHere = resolved && !operation.detached && operation.owner === publisher
  if (authoritativeHere && operations.size === 0) {
    reconciliationRequired = false
    reconciledPublisher = publisher
    orphanedFence = false
  } else {
    if (!resolved) orphanedFence = true
    reconciliationRequired = true
    reconciledPublisher = null
    writeRetainedFence()
    commit(recoverySnapshot())
  }
  return true
}

export function captureAuthoritativeRead(owner, { explicit = false } = {}) {
  return Object.freeze({ owner, epoch: operationEpoch, explicit: explicit === true })
}

export function confirmAuthoritativeRead(read) {
  if (!read || read.owner !== publisher || operations.size > 0 || read.epoch !== operationEpoch) {
    return false
  }
  // After a hard reload/HMR loses the old write promise, the automatic mount GET is not enough to
  // prove causal ordering. Require a separate, deliberate reconciliation request.
  if (orphanedFence && !read.explicit) return false
  if (retainedFence && !clearRetainedFence()) {
    reconciliationRequired = true
    reconciledPublisher = null
    commit(RETAINED_SNAPSHOT)
    return false
  }
  reconciliationRequired = false
  reconciledPublisher = publisher
  orphanedFence = false
  return true
}

export function publish(tokenOrValue, suppliedValue) {
  const legacyCall = arguments.length < 2
  // The compatibility form is safe only when no scoped publisher exists; it may not impersonate a
  // newer Settings instance during StrictMode/HMR handoff.
  if (legacyCall && publisher !== null) return snapshot
  const token = legacyCall ? null : tokenOrValue
  const value = legacyCall ? (tokenOrValue || {}) : (suppliedValue || {})
  if (token !== publisher) return snapshot
  const active = value?.active === true
  if (!active) return retainedFence ? commit(recoverySnapshot()) : commit(INACTIVE_SNAPSHOT)

  // An active publisher must opt out of blocking explicitly. Malformed or partial publications
  // therefore fail closed instead of briefly enabling a paid Start.
  const blocked = value?.blocked !== false
  const next = Object.freeze({
    active: true,
    blocked,
    status: cleanText(value?.status, blocked ? 'blocked' : 'ready'),
    reason: cleanText(value?.reason, blocked ? DEFAULT_BLOCK_REASON : 'Settings are ready for a new run.'),
  })

  if (blocked && value?.retainable === true) writeRetainedFence()
  if (value?.requiresReconciliation === true) {
    reconciliationRequired = true
    reconciledPublisher = null
  }

  const foreignOperationPending = [...operations.values()].some(operation => (
    operation.owner !== token || operation.detached
  ))
  if (foreignOperationPending) {
    reconciliationRequired = true
    reconciledPublisher = null
    writeRetainedFence()
    return commit(PENDING_SNAPSHOT)
  }

  if (value?.resolved === true) {
    if (storageUncertain) writeRetainedFence()
    if (operations.size > 0 || reconciliationRequired || reconciledPublisher !== token
        || storageUncertain) {
      writeRetainedFence()
      return commit(recoverySnapshot())
    }
    // An authoritative invalid saved baseline remains durable; a purely local draft does not.
    if (value?.retainable !== true && !clearRetainedFence()) return commit(RETAINED_SNAPSHOT)
  }

  return commit(next)
}

export function releasePublisher(token, { retain = false } = {}) {
  if (token !== publisher) return snapshot
  for (const operation of operations.values()) {
    if (operation.owner === token) operation.detached = true
  }
  publisher = null
  reconciledPublisher = null
  if (retain || retainedFence || operations.size > 0) {
    reconciliationRequired = true
    writeRetainedFence()
    return commit(recoverySnapshot())
  }
  if (!clearRetainedFence()) return commit(RETAINED_SNAPSHOT)
  return commit(INACTIVE_SNAPSHOT)
}
