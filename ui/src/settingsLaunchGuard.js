const DEFAULT_BLOCK_REASON = 'Settings are not ready for a new run.'

const INACTIVE_SNAPSHOT = Object.freeze({
  active: false,
  blocked: false,
  status: 'inactive',
  reason: '',
})

let snapshot = INACTIVE_SNAPSHOT
const listeners = new Set()

const cleanText = (value, fallback = '') => {
  const text = typeof value === 'string' ? value.trim() : ''
  return text || fallback
}

// useSyncExternalStore requires getSnapshot() to retain object identity until the store actually
// changes. Canonicalize inactive state and suppress equal publications so consumers never enter a
// render loop merely because Settings rendered again.
export function getSnapshot() {
  return snapshot
}

export function subscribe(listener) {
  if (typeof listener !== 'function') throw new TypeError('Settings launch guard listener must be a function')
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function publish(value = {}) {
  const active = value?.active === true
  if (!active) {
    if (snapshot === INACTIVE_SNAPSHOT) return snapshot
    snapshot = INACTIVE_SNAPSHOT
  } else {
    // An active publisher must opt out of blocking explicitly. Malformed or partial publications
    // therefore fail closed instead of briefly enabling a paid Start.
    const blocked = value?.blocked !== false
    const next = Object.freeze({
      active: true,
      blocked,
      status: cleanText(value?.status, blocked ? 'blocked' : 'ready'),
      reason: cleanText(value?.reason, blocked ? DEFAULT_BLOCK_REASON : 'Settings are ready for a new run.'),
    })
    if (snapshot.active === next.active && snapshot.blocked === next.blocked
        && snapshot.status === next.status && snapshot.reason === next.reason) return snapshot
    snapshot = next
  }

  for (const listener of [...listeners]) listener()
  return snapshot
}
