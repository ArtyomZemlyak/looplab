import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  fetchEventStream, get, normalizeRunGeneration, observeRunGeneration, runApiPath,
} from './api.js'
import { withBuilding } from './buildingModel.js'

// Keep responsive behavior in React aligned with the CSS breakpoints.  The workspace uses this to
// switch persistent desktop panes into temporary drawers on smaller screens; listening to the media
// query also makes resizing/zoom changes take effect without a reload.
export function useMediaQuery(query) {
  // Guard window.matchMedia's EXISTENCE, not just window's: an environment where window exists but
  // matchMedia does not (jsdom without a polyfill, some embedded WebViews) would otherwise throw
  // `window.matchMedia is not a function` in the useState initializer and blow up the whole render.
  // Degrade to `false` (desktop default), matching the defensive optional-API style used elsewhere.
  const hasMM = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  const read = () => hasMM() && window.matchMedia(query).matches
  const [matches, setMatches] = useState(read)
  useEffect(() => {
    if (!hasMM()) return
    const media = window.matchMedia(query)
    const onChange = () => setMatches(media.matches)
    onChange()
    media.addEventListener?.('change', onChange)
    return () => media.removeEventListener?.('change', onChange)
  }, [query])
  return matches
}

// The ONE shared poll hook (mega-refactor P5.2), replacing the hand-rolled setInterval effects that
// were copy-pasted across AssistantBar/Dock/Inspector/RunList/panels. Calls `fn` once immediately and
// then every `ms` milliseconds until unmount or a `deps` change — the exact clearInterval-on-cleanup
// semantics of the effects it replaces. `fn` receives an `alive()` predicate (true until THIS effect
// instance is cleaned up) so async callers can guard their setState exactly like the old
// `let alive = true` closure flag.
//   ms == null        → no interval (the immediate call still fires) — "poll only while working" sites.
//   enabled: false    → do nothing at all (the old `if (!cond) return` early-out; cond goes in deps).
//   immediate: false  → skip the immediate call (interval ticks only).
//   pauseHidden: true → RunList's tab-visibility guard, OPT-IN so the other sites keep polling while
//                       hidden as they always did: skip ticks while document.hidden, and refresh once
//                       immediately when the tab becomes visible again.
export function usePoll(fn, ms, deps = [], { pauseHidden = false, immediate = true, enabled = true } = {}) {
  useEffect(() => {
    if (!enabled) return
    let on = true
    const alive = () => on
    const tick = () => fn(alive)
    if (immediate) tick()
    const t = (ms != null) ? setInterval(() => { if (!pauseHidden || !document.hidden) tick() }, ms) : null
    const onVis = pauseHidden ? () => { if (!document.hidden) tick() } : null
    if (onVis) document.addEventListener('visibilitychange', onVis)
    return () => {
      on = false
      if (t != null) clearInterval(t)
      if (onVis) document.removeEventListener('visibilitychange', onVis)
    }
    // deps come from the caller (they list what their fn reads), mirroring the effects this replaces
  }, deps)
}

// `withBuilding` (the synthetic building-node splice) lives in ./buildingModel.js so it can be unit
// tested without React; imported at the top of this module.

// Subscribe to a run's live folded state over SSE. The server emits `event: state` frames whose
// data is { state, seq, generation, event_count? }. Returns the latest live state + connection
// status; event_count is optional only for compatibility with a legacy server. Auto-reconnects.
const normalizeEventCount = value => {
  if (value == null) return null // additive field: tolerate a legacy server during rolling upgrades
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined
}

export function useRunState(runId, { pollOnly = false, pollMs = 4000 } = {}) {
  const [live, setLive] = useState(null)
  const [seq, setSeq] = useState(-1)
  const [generationState, setGenerationState] = useState({ runId, value: null })
  const generation = generationState.runId === runId ? generationState.value : null
  const [eventCountState, setEventCountState] = useState({ runId, value: null })
  const eventCount = eventCountState.runId === runId ? eventCountState.value : null
  const [connected, setConnected] = useState(false)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)
  const [retryToken, setRetryToken] = useState(0)
  const streamRef = useRef(null)
  // Review-path re-probe backoff. The owner path self-heals inside one effect run via the fetch stream
  // onerror ramp, but the review path re-probes by bumping retryToken (a fresh effect run that resets
  // the local backoff), so its ramp must live in a ref that survives across runs — else a sustained
  // proxy 5xx would re-probe on a fixed 1.5s tick (the GET storm the owner ramp avoids).
  const reviewRetryRef = useRef(1500)

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let timer = null
    let pollTimer = null
    let lastSeq = -2, lastAlive, lastGeneration = null, lastEventCount = null
    let lastStreamEventId = ''
    setLive(null)
    setSeq(-1)
    setGenerationState({ runId, value: null })
    setEventCountState({ runId, value: null })
    setConnected(false)
    setStatus('loading')
    setError(null)
    // Reconnect backoff: behind a proxy a hard drop/504 on the GET (or a keepalive-starved idle drop)
    // would otherwise retry on a fixed 1.5s tick forever — a GET storm that re-folds the run each time.
    // Ramp 1.5s → ×2 → 30s cap; a live `state` frame proves the stream works and resets it.
    const MIN_BACKOFF = 1500, MAX_BACKOFF = 30000
    let backoff = MIN_BACKOFF
    const reconnect = (delay) => { if (stopped) return; clearTimeout(timer); timer = setTimeout(connect, delay) }
    function connect() {
      streamRef.current?.abort()
      const controller = new AbortController()
      streamRef.current = controller
      let terminal = false
      fetchEventStream(runApiPath(runId, '/events'), {
        signal: controller.signal,
        lastEventId: lastStreamEventId,
        onEvent: event => {
          if (stopped || controller.signal.aborted) return
          if (event.lastEventId !== '') lastStreamEventId = event.lastEventId
          if (event.type === 'done') {
            // A terminal run can later be reopened. End this request and reconnect-poll just as the
            // former EventSource path did; seq/generation dedup keeps the idle refresh cheap.
            terminal = true
            controller.abort()
            reconnect(2500)
            return
          }
          if (event.type !== 'state') return
          let p
          try { p = JSON.parse(event.data) } catch { return }
          backoff = MIN_BACKOFF
          setConnected(true)
          // Re-render on a seq change OR an engine_running flip (a zombie's liveness changes with no
          // new event/seq); track lastAlive in the closure (NOT stale React `live`) to avoid churn.
          const alive = p.state && p.state.engine_running
          const nextGeneration = normalizeRunGeneration(p.generation)
          const nextEventCount = normalizeEventCount(p.event_count)
          if (p.generation != null && !nextGeneration) return
          if (nextEventCount === undefined) return
          if (p.seq === lastSeq && alive === lastAlive && nextGeneration === lastGeneration
              && nextEventCount === lastEventCount) return
          lastSeq = p.seq; lastAlive = alive; lastGeneration = nextGeneration; lastEventCount = nextEventCount
          setGenerationState({ runId, value: nextGeneration })
          setEventCountState({ runId, value: nextEventCount })
          setLive(withBuilding(p.state)); setSeq(p.seq); setStatus('ready'); setError(null)
        },
      }).then(({ retry }) => {
        if (stopped || terminal || controller.signal.aborted) return
        setConnected(false)
        reconnect(retry ?? backoff)
        backoff = Math.min(backoff * 2, MAX_BACKOFF)
      }).catch(error => {
        if (stopped || terminal || controller.signal.aborted || error?.name === 'AbortError') return
        setConnected(false)
        reconnect(backoff)
        backoff = Math.min(backoff * 2, MAX_BACKOFF)
      })
    }
    // Probe once before opening a self-reconnecting authenticated fetch stream. This turns a mistyped/deleted run
    // URL into an explicit 404 state instead of an endless "Connecting…" loop.
    get(runApiPath(runId, '/state'))
      .then(p => {
        if (stopped) return
        lastSeq = p.seq
        lastAlive = p.state && p.state.engine_running
        lastGeneration = normalizeRunGeneration(p.generation)
        lastEventCount = normalizeEventCount(p.event_count)
        if (p.generation != null && !lastGeneration) {
          throw new Error('The server returned an invalid run generation.')
        }
        if (lastEventCount === undefined) throw new Error('The server returned an invalid event count.')
        setGenerationState({ runId, value: lastGeneration })
        setEventCountState({ runId, value: lastEventCount })
        setLive(withBuilding(p.state)); setSeq(p.seq); setStatus('ready'); setError(null)
        reviewRetryRef.current = 1500   // a good probe resets the review re-probe backoff
        if (!pollOnly) connect()
        else {
          setConnected(true)
          const poll = () => {
            get(runApiPath(runId, '/state'))
              .then(next => {
                if (stopped) return
                setConnected(true); setStatus('ready'); setError(null)
                const alive = next.state && next.state.engine_running
                const nextGeneration = normalizeRunGeneration(next.generation)
                const nextEventCount = normalizeEventCount(next.event_count)
                if (next.generation != null && !nextGeneration) {
                  throw new Error('The server returned an invalid run generation.')
                }
                if (nextEventCount === undefined) throw new Error('The server returned an invalid event count.')
                if (next.seq !== lastSeq || alive !== lastAlive || nextGeneration !== lastGeneration
                    || nextEventCount !== lastEventCount) {
                  lastSeq = next.seq; lastAlive = alive; lastGeneration = nextGeneration; lastEventCount = nextEventCount
                  setGenerationState({ runId, value: nextGeneration })
                  setEventCountState({ runId, value: nextEventCount })
                  setLive(withBuilding(next.state)); setSeq(next.seq)
                }
                pollTimer = setTimeout(poll, pollMs)
              })
              .catch(error => {
                if (stopped) return
                const ended = error?.status === 401 || error?.status === 404 || error?.status === 410
                setConnected(false)
                if (ended) {
                  setError('This review link expired, was revoked, or is invalid.')
                  setLive(null); setStatus('gone')
                  return
                }
                setError(error?.message || 'Review refresh failed')
                pollTimer = setTimeout(poll, pollMs)
              })
          }
          pollTimer = setTimeout(poll, pollMs)
        }
      })
      .catch(e => {
        if (stopped) return
        const st = e?.status
        const reviewEnded = pollOnly && (st === 401 || st === 404 || st === 410)
        if (reviewEnded) {
          setStatus('gone'); setError('This review link expired, was revoked, or is invalid.'); return
        }
        if (st === 404) {
          setStatus('not_found'); setError('This run does not exist or was removed.'); return
        }
        // Transient probe failure (proxy 504, dropped connection, keepalive-starved idle drop): do NOT
        // strand the workspace on an error screen with nothing scheduled to retry (UI-2). The owner
        // stream self-heals via the fetch-SSE reconnect backoff, so start it; the review poll path
        // reschedules a re-probe. Either way the UI recovers on its own once the blip clears.
        setError(e?.message || 'Could not load this run.')
        if (!pollOnly) { setStatus('loading'); connect() }
        else {
          setStatus('error')
          const delay = reviewRetryRef.current
          reviewRetryRef.current = Math.min(delay * 2, MAX_BACKOFF)   // ramp like the owner path
          timer = setTimeout(() => setRetryToken(n => n + 1), delay)
        }
      })
    return () => {
      stopped = true; clearTimeout(timer); clearTimeout(pollTimer)
      streamRef.current?.abort()
    }
  }, [runId, retryToken, pollOnly, pollMs])

  // Publish only after React committed the snapshot. Updating this registry in the SSE callback would
  // create a small pre-render window where a click on visible generation A could be rebound to B.
  useLayoutEffect(() => { observeRunGeneration(runId, generation) }, [runId, generation])

  return { live, seq, generation, eventCount, connected, status, error,
    retry: () => setRetryToken(n => n + 1) }
}
