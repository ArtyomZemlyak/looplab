export function installNavigationLossGuard({
  allowRef, guardedHash, message, win = window,
}) {
  if (!allowRef || typeof guardedHash !== 'string' || typeof message !== 'function') {
    throw new TypeError('Invalid navigation loss guard')
  }
  const beforeUnload = event => {
    event.preventDefault()
    event.returnValue = ''
  }
  const blockClientNavigation = () => {
    if (allowRef.current || win.location.hash === guardedHash) return
    if (win.confirm(message())) {
      allowRef.current = true
      return
    }
    win.history.pushState(win.history.state, '',
      `${win.location.pathname}${win.location.search}${guardedHash}`)
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
