import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  emptyRunRouteState, hashWithRunRouteState, hrefWithRunRouteState, parseRunRouteState,
  reconcileRunRouteStateUpdate, sameRunRouteState, sanitizeRunRouteState,
} from './runRouteState.js'

const browserLocation = () => (typeof location === 'undefined'
  ? { pathname: '', search: '', hash: '' }
  : location)

function read(reviewMode) {
  const parsed = parseRunRouteState(browserLocation().hash, { reviewMode })
  return { ...parsed, navigationRevision: 0 }
}

export function useRunRouteState({ generation = null, reviewMode = false } = {}) {
  const [resource, setResource] = useState(() => read(reviewMode))
  const stateRef = useRef(resource.state)
  const hydratedHashRef = useRef(null)
  stateRef.current = resource.state

  const write = useCallback((state, mode = 'push', { forceGeneration = false } = {}) => {
    if (typeof window === 'undefined') return
    const nextHash = hashWithRunRouteState(window.location.hash, state, { reviewMode, forceGeneration })
    if (nextHash === window.location.hash) return
    const href = `${window.location.pathname}${window.location.search}${nextHash}`
    window.history[mode === 'replace' ? 'replaceState' : 'pushState'](window.history.state, '', href)
    hydratedHashRef.current = nextHash
  }, [reviewMode])

  const hydrate = useCallback(() => {
    if (hydratedHashRef.current === browserLocation().hash) return
    const parsed = parseRunRouteState(browserLocation().hash, { reviewMode })
    // `gen=A` with otherwise-default state is still an explicit exact-view fence. Preserve it while
    // canonicalizing so refresh/recopy cannot silently turn a generation-bound link into a live alias.
    const canonical = hashWithRunRouteState(browserLocation().hash, parsed.state, {
      reviewMode, forceGeneration: !!parsed.state.generation,
    })
    if (typeof window !== 'undefined' && canonical !== window.location.hash) {
      const href = `${window.location.pathname}${window.location.search}${canonical}`
      window.history.replaceState(window.history.state, '', href)
    }
    hydratedHashRef.current = canonical
    stateRef.current = parsed.state
    setResource(current => ({ ...parsed, navigationRevision: current.navigationRevision + 1 }))
  }, [reviewMode])

  useEffect(() => {
    hydrate()
    window.addEventListener('popstate', hydrate)
    window.addEventListener('hashchange', hydrate)
    return () => {
      window.removeEventListener('popstate', hydrate)
      window.removeEventListener('hashchange', hydrate)
    }
  }, [hydrate])

  const mismatch = !!(resource.state.generation && generation
    && resource.state.generation !== generation)
  const pendingFence = !!(resource.state.generation && !generation)

  const update = useCallback((patch, { mode = 'push', preserveIssues = false } = {}) => {
    const current = stateRef.current
    if (current.generation && generation && current.generation !== generation) return current
    const raw = typeof patch === 'function' ? patch(current) : { ...current, ...patch }
    const candidate = reconcileRunRouteStateUpdate(current, raw, { generation, reviewMode })
    if (candidate === current || sameRunRouteState(current, candidate)) return current
    stateRef.current = candidate
    setResource(value => ({ ...value, state: candidate,
      issues: preserveIssues ? value.issues : [] }))
    write(candidate, mode)
    return candidate
  }, [generation, reviewMode, write])

  const openCurrentGeneration = useCallback(() => {
    const state = emptyRunRouteState()
    stateRef.current = state
    setResource(value => ({ ...value, state, issues: [] }))
    write(state, 'push')
  }, [write])
  const clearIssues = useCallback(() => {
    setResource(value => ({ ...value, issues: [] }))
  }, [])

  const exactHref = useCallback((state = stateRef.current, { forReview = reviewMode } = {}) => {
    const exact = sanitizeRunRouteState({ ...state, generation: generation || state.generation },
      { reviewMode: forReview })
    return hrefWithRunRouteState(browserLocation(), exact,
      { reviewMode: forReview, forceGeneration: true })
  }, [generation, reviewMode])

  return useMemo(() => ({
    state: resource.state,
    issues: resource.issues,
    hadState: resource.hadState,
    navigationRevision: resource.navigationRevision,
    generationMismatch: mismatch,
    generationPending: pendingFence,
    update,
    openCurrentGeneration,
    clearIssues,
    exactHref,
  }), [resource, mismatch, pendingFence, update, openCurrentGeneration, clearIssues, exactHref])
}
