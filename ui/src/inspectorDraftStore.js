import { useCallback, useRef, useSyncExternalStore } from 'react'

// Inspector authoring surfaces are intentionally conditional: collapsing the pane, opening another
// workspace view, or crossing the compact breakpoint unmounts them. Keep their client-only state in
// the owning RunView so the exact run/node/attempt scope can resume without making drafts durable
// across runs, reloads, or signed-in sessions.
export function createInspectorDraftStore() {
  const entries = new Map()
  const listeners = new Map()
  const protectedScopes = new Set()
  const maxDetachedDisposableScopes = 512

  const notify = scope => {
    for (const listener of listeners.get(scope) || []) listener()
  }
  const prune = () => {
    let detachedDisposable = 0
    for (const scope of entries.keys()) {
      if (!protectedScopes.has(scope) && !listeners.get(scope)?.size) detachedDisposable += 1
    }
    if (detachedDisposable <= maxDetachedDisposableScopes) return
    // Authoring/recovery scopes are never evicted: an unmounted editor is exactly where its identity
    // must survive. Bound only explicitly disposable, detached view state such as filters/search.
    for (const scope of entries.keys()) {
      if (protectedScopes.has(scope) || listeners.get(scope)?.size) continue
      entries.delete(scope)
      detachedDisposable -= 1
      if (detachedDisposable <= maxDetachedDisposableScopes) break
    }
  }

  return {
    readField(scope, field, fallback) {
      const current = entries.get(scope)
      return current && Object.hasOwn(current, field) ? current[field] : fallback
    },
    updateField(scope, field, update, fallback, { disposable = false } = {}) {
      const current = entries.get(scope) || {}
      const previous = Object.hasOwn(current, field) ? current[field] : fallback
      const next = typeof update === 'function' ? update(previous) : update
      if (Object.is(previous, next) && Object.hasOwn(current, field)) return next
      if (!disposable) protectedScopes.add(scope)
      // Reinsert to keep Map order as a tiny write-LRU used by `prune`.
      entries.delete(scope)
      entries.set(scope, { ...current, [field]: next })
      prune()
      notify(scope)
      return next
    },
    clear(scope) {
      if (!entries.delete(scope)) return
      protectedScopes.delete(scope)
      notify(scope)
    },
    subscribe(scope, listener) {
      let scoped = listeners.get(scope)
      if (!scoped) {
        scoped = new Set()
        listeners.set(scope, scoped)
      }
      scoped.add(listener)
      return () => {
        scoped.delete(listener)
        if (!scoped.size) {
          listeners.delete(scope)
          prune()
        }
      }
    },
  }
}

const initialValue = value => typeof value === 'function' ? value() : value

export function useInspectorDraftField(store, scope, field, initial, { disposable = false } = {}) {
  const initialRef = useRef(null)
  if (!initialRef.current || initialRef.current.scope !== scope) {
    initialRef.current = { scope, value: initialValue(initial) }
  }
  const fallback = initialRef.current.value

  const subscribe = useCallback(
    listener => store.subscribe(scope, listener),
    [scope, store],
  )
  const snapshot = useCallback(
    () => store.readField(scope, field, fallback),
    [fallback, field, scope, store],
  )
  const value = useSyncExternalStore(subscribe, snapshot, snapshot)
  const setValue = useCallback(
    update => store.updateField(scope, field, update, fallback, { disposable }),
    [disposable, fallback, field, scope, store],
  )
  return [value, setValue]
}
