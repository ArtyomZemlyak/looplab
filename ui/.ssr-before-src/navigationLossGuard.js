export function installNavigationLossGuard({
  allowRef, guardedHash, message, onAllow = null, shouldBlock = null,
  win = window, guardedState = win.history.state,
}) {
  if (!allowRef || typeof guardedHash !== 'string' || typeof message !== 'function') {
    throw new TypeError('Invalid navigation loss guard')
  }
  if (onAllow != null && typeof onAllow !== 'function') {
    throw new TypeError('Invalid navigation loss callback')
  }
  if (shouldBlock != null && typeof shouldBlock !== 'function') {
    throw new TypeError('Invalid navigation loss predicate')
  }
  const beforeUnload = event => {
    event.preventDefault()
    event.returnValue = ''
  }
  const guardedHref = `${win.location.pathname}${win.location.search}${guardedHash}`
  const restoreGuardedLocation = () => {
    try {
      win.history.pushState(guardedState, '', guardedHref)
      return true
    } catch {}
    try {
      win.location.hash = guardedHash
      if (win.location.hash === guardedHash) {
        try { win.history.replaceState(guardedState, '', guardedHref) } catch {}
        return true
      }
    } catch {}
    try {
      win.history.replaceState(guardedState, '', guardedHref)
      return win.location.hash === guardedHash
    } catch {}
    return false
  }
  const blockClientNavigation = event => {
    if (allowRef.current || win.location.hash === guardedHash) return
    const targetHash = win.location.hash
    if (shouldBlock && !shouldBlock(targetHash)) return
    if (win.confirm(message(targetHash))) {
      allowRef.current = true
      onAllow?.(targetHash)
      return
    }
    // App and route listeners run after this capture listener. Never let them hydrate a cancelled
    // destination, even if a browser throttles or rejects every available history write.
    event?.preventDefault?.()
    event?.stopImmediatePropagation?.()
    restoreGuardedLocation()
  }
  win.addEventListener('beforeunload', beforeUnload)
  win.addEventListener('popstate', blockClientNavigation, true)
  win.addEventListener('hashchange', blockClientNavigation, true)
  return () => {
    win.removeEventListener('beforeunload', beforeUnload)
    win.removeEventListener('popstate', blockClientNavigation, true)
    win.removeEventListener('hashchange', blockClientNavigation, true)
  }
}
