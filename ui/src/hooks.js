import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { apiUrl, get, normalizeRunGeneration, observeRunGeneration } from './api.js'   // join the served path prefix so SSE works behind a proxy subpath

// Keep responsive behavior in React aligned with the CSS breakpoints.  The workspace uses this to
// switch persistent desktop panes into temporary drawers on smaller screens; listening to the media
// query also makes resizing/zoom changes take effect without a reload.
export function useMediaQuery(query) {
  const read = () => typeof window !== 'undefined' && window.matchMedia(query).matches
  const [matches, setMatches] = useState(read)
  useEffect(() => {
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

// Splice the currently-BUILDING node (server marker `state.building`) into `nodes` as a synthetic
// status:'building' card, so it shows the INSTANT the engine starts on it — before node_created — and
// every node consumer (canvas / list / panels) renders it with no extra wiring. Cleared server-side the
// moment node_created folds. Kept out of the real event-sourced node set on the backend (id allocation).
function withBuilding(state) {
  const b = state && state.building
  // Don't splice a phantom "building…" card once the run is over: a finished run clears the marker
  // server-side, but a STALLED run (engine died mid-build, engine_running===false, not finished) would
  // otherwise leave a breathing card for a node that will never appear.
  if (!b || b.node_id == null || state.finished || state.engine_running === false
      || !state.nodes || state.nodes[b.node_id]) return state
  return { ...state, nodes: { ...state.nodes, [b.node_id]: {
    id: b.node_id, operator: b.operator || 'improve', parent_ids: b.parent_ids || [],
    status: 'building', building: true, idea: { operator: b.operator || 'improve', rationale: 'building…' },
  } } }
}

// Subscribe to a run's live folded state over SSE. The server emits `event: state` frames whose
// data is { state, seq }. Returns the latest live state + connection status. Auto-reconnects.
export function useRunState(runId, { pollOnly = false, pollMs = 4000 } = {}) {
  const [live, setLive] = useState(null)
  const [seq, setSeq] = useState(-1)
  const [generationState, setGenerationState] = useState({ runId, value: null })
  const generation = generationState.runId === runId ? generationState.value : null
  const [connected, setConnected] = useState(false)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState(null)
  const [retryToken, setRetryToken] = useState(0)
  const esRef = useRef(null)

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let timer = null
    let pollTimer = null
    let lastSeq = -2, lastAlive, lastGeneration = null
    setLive(null)
    setSeq(-1)
    setGenerationState({ runId, value: null })
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
      const es = new EventSource(apiUrl(`/api/runs/${runId}/events`))
      esRef.current = es
      es.addEventListener('state', (e) => {
        let p
        try { p = JSON.parse(e.data) } catch { return }  // ignore a torn/partial SSE frame
        backoff = MIN_BACKOFF   // a live frame proves the stream works — reset the error backoff
        setConnected(true)
        // Re-render on a seq change OR an engine_running flip (a zombie's liveness changes with no
        // new event/seq); track lastAlive in the closure (NOT stale React `live`) to avoid churn.
        const alive = p.state && p.state.engine_running
        const nextGeneration = normalizeRunGeneration(p.generation)
        if (p.generation != null && !nextGeneration) return
        if (p.seq === lastSeq && alive === lastAlive && nextGeneration === lastGeneration) return
        lastSeq = p.seq; lastAlive = alive; lastGeneration = nextGeneration
        setGenerationState({ runId, value: nextGeneration })
        setLive(withBuilding(p.state)); setSeq(p.seq); setStatus('ready'); setError(null)
      })
      // `done` = the run reached a terminal state and the server ends the stream. We do NOT treat it
      // as "stop forever": reconnect-poll so a reopen (fork / branch / add-experiment) is picked up
      // within a couple seconds. Closing-and-never-reconnecting is what made those actions invisible
      // until a manual reload (#8). The state handler dedups by seq, so the poll is cheap when idle.
      es.addEventListener('done', () => { es.close(); reconnect(2500) })
      es.onerror = () => {
        setConnected(false); es.close()
        reconnect(backoff)
        backoff = Math.min(backoff * 2, MAX_BACKOFF)   // ramp on repeated failure; reset on a live frame
      }
    }
    // Probe once before opening a self-reconnecting EventSource. This turns a mistyped/deleted run
    // URL into an explicit 404 state instead of an endless "Connecting…" loop.
    get(`/api/runs/${encodeURIComponent(runId)}/state`)
      .then(p => {
        if (stopped) return
        lastSeq = p.seq
        lastAlive = p.state && p.state.engine_running
        lastGeneration = normalizeRunGeneration(p.generation)
        if (p.generation != null && !lastGeneration) {
          throw new Error('The server returned an invalid run generation.')
        }
        setGenerationState({ runId, value: lastGeneration })
        setLive(withBuilding(p.state)); setSeq(p.seq); setStatus('ready'); setError(null)
        if (!pollOnly) connect()
        else {
          setConnected(true)
          const poll = () => {
            get(`/api/runs/${encodeURIComponent(runId)}/state`)
              .then(next => {
                if (stopped) return
                setConnected(true); setStatus('ready'); setError(null)
                const alive = next.state && next.state.engine_running
                const nextGeneration = normalizeRunGeneration(next.generation)
                if (next.generation != null && !nextGeneration) {
                  throw new Error('The server returned an invalid run generation.')
                }
                if (next.seq !== lastSeq || alive !== lastAlive || nextGeneration !== lastGeneration) {
                  lastSeq = next.seq; lastAlive = alive; lastGeneration = nextGeneration
                  setGenerationState({ runId, value: nextGeneration })
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
        const reviewEnded = pollOnly && (e?.status === 401 || e?.status === 404 || e?.status === 410)
        setStatus(reviewEnded ? 'gone' : e?.status === 404 ? 'not_found' : 'error')
        setError(reviewEnded ? 'This review link expired, was revoked, or is invalid.'
          : e?.status === 404 ? 'This run does not exist or was removed.' : (e?.message || 'Could not load this run.'))
      })
    return () => {
      stopped = true; clearTimeout(timer); clearTimeout(pollTimer)
      esRef.current && esRef.current.close()
    }
  }, [runId, retryToken, pollOnly, pollMs])

  // Publish only after React committed the snapshot. Updating this registry in the SSE callback would
  // create a small pre-render window where a click on visible generation A could be rebound to B.
  useLayoutEffect(() => { observeRunGeneration(runId, generation) }, [runId, generation])

  return { live, seq, generation, connected, status, error, retry: () => setRetryToken(n => n + 1) }
}

// Browser notifications for finish / approval / failure-spike.
export function useNotifications(enabled, state) {
  const prev = useRef({ phase: null, fails: 0 })
  useEffect(() => {
    if (!enabled || !state) return
    // `Notification` is absent in insecure/unsupported contexts — referencing it bare throws.
    if (!('Notification' in window)) return
    if (Notification.permission === 'default') Notification.requestPermission()
    const phase = state.phase
    const fails = Object.values(state.nodes || {}).filter(n => n.status === 'failed').length
    const notify = (t, b) => { try { new Notification(t, { body: b }) } catch {} }
    if (prev.current.phase && phase !== prev.current.phase) {
      if (phase === 'finished') notify('LoopLab — run finished', `best=${state.best_node_id ?? '—'}`)
      if (phase === 'approval') notify('LoopLab — approval needed', state.goal || '')
      if (phase === 'spec_approval') notify('LoopLab — eval spec needs ratification', '')
    }
    if (fails - prev.current.fails >= 3) notify('LoopLab — failures spiking', `${fails} failed nodes`)
    prev.current = { phase, fails }
  }, [enabled, state])
}
