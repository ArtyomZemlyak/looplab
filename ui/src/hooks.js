import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  deadlineGet, fetchEventStream, normalizeRunGeneration, observeRunGeneration, runApiPath,
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
// then every `ms` milliseconds until unmount or a `deps` change. Ticks are serialized: while one
// request is unsettled, any number of timer/visibility ticks collapse into one catch-up read. This
// prevents an older slow response from landing after a newer response and rolling a resource back.
// `fn` receives an `alive()` predicate (true until THIS effect instance is cleaned up) so a request
// from an old dependency scope also cannot commit after the new scope starts.
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
    let running = false
    let queued = false
    let request = null
    const alive = () => on
    const tick = async () => {
      if (!on) return
      if (running) {
        queued = true
        return
      }
      running = true
      try {
        const result = fn(alive)
        request = result?.promise && result?.controller ? result : null
        // Adopt native promises, thenables, and synchronous callbacks. Rejections are a
        // caller-owned resource outcome; consuming them here only keeps the scheduler alive.
        await (request?.promise ?? result)
      } catch { /* caller renders the resource failure */ }
      request = null
      running = false
      if (!on || !queued) return
      queued = false
      if (!pauseHidden || !document.hidden) tick()
    }
    if (immediate) tick()
    const t = (ms != null) ? setInterval(() => { if (!pauseHidden || !document.hidden) tick() }, ms) : null
    const onVis = pauseHidden ? () => { if (!document.hidden) tick() } : null
    if (onVis) document.addEventListener('visibilitychange', onVis)
    return () => {
      on = false
      queued = false
      request?.controller.abort()
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

export function useRunState(runId, {
  pollOnly = false, pollMs = 4000,
} = {}) {
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
    let terminalMode = false
    let terminalDelay = 60000
    let terminalRequest = null
    let reviewTerminal = false
    let reviewPoll = null
    let reviewPollRunning = false
    let reviewEnded = false
    let initialRequest = null
    let reviewRequest = null
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
    const TERMINAL_PROBE_MS = 60000, TERMINAL_PROBE_MAX_MS = 300000
    let backoff = MIN_BACKOFF
    const hidden = () => typeof document !== 'undefined' && document.hidden
    const identity = (payload, probe = false) => {
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('Invalid run snapshot.')
      }
      if (probe && payload?.schema !== 1) throw new Error('Invalid lifecycle probe.')
      if (!Number.isSafeInteger(payload.seq) || payload.seq < -1) {
        throw new Error('Invalid run sequence.')
      }
      if (!probe && (!payload.state || typeof payload.state !== 'object'
        || Array.isArray(payload.state))) {
        throw new Error('Invalid run state.')
      }
      const alive = probe ? payload?.engine_running : payload?.state?.engine_running
      const nextGeneration = normalizeRunGeneration(payload?.generation)
      const nextEventCount = normalizeEventCount(payload?.event_count)
      if (payload?.generation != null && !nextGeneration) {
        throw new Error('Invalid run generation.')
      }
      if (nextEventCount === undefined) {
        throw new Error('Invalid event count.')
      }
      return [payload?.seq, alive, nextGeneration, nextEventCount]
    }
    const identityChanged = next => next[0] !== lastSeq || next[1] !== lastAlive
      || next[2] !== lastGeneration || next[3] !== lastEventCount
    const commitSnapshot = payload => {
      const next = identity(payload)
      const sameGeneration = next[2] != null && next[2] === lastGeneration
      if (sameGeneration && (next[0] < lastSeq
        || (next[3] != null && lastEventCount != null && next[3] < lastEventCount))) {
        throw new Error('Run snapshot moved backwards.')
      }
      if (!identityChanged(next)) return next
      ;[lastSeq, lastAlive, lastGeneration, lastEventCount] = next
      setGenerationState({ runId, value: next[2] })
      setEventCountState({ runId, value: next[3] })
      setLive(withBuilding(payload.state))
      setSeq(next[0])
      return next
    }
    const terminalSnapshot = payload => payload?.state?.finished === true
      && payload.state.engine_running === false && payload.state.phase !== 'finalizing'
    const reconnect = (delay) => { if (stopped) return; clearTimeout(timer); timer = setTimeout(connect, delay) }

    const scheduleTerminalProbe = (delay = terminalDelay) => {
      if (stopped || !terminalMode || hidden()) return
      clearTimeout(timer)
      timer = setTimeout(probeTerminal, delay)
    }
    function probeTerminal() {
      if (stopped || !terminalMode || hidden() || terminalRequest) return
      const request = deadlineGet(runApiPath(runId, '/lifecycle'))
      terminalRequest = request
      const failed = () => {
        if (stopped || !terminalMode || terminalRequest !== request) return
        terminalRequest = null
        setConnected(false)
        terminalDelay = Math.min(terminalDelay * 2, TERMINAL_PROBE_MAX_MS)
        scheduleTerminalProbe()
      }
      request.promise.then(payload => {
        if (stopped || !terminalMode || terminalRequest !== request) return
        let next
        try { next = identity(payload, true) } catch { failed(); return }
        terminalRequest = null
        setConnected(true)
        if (identityChanged(next)) {
          terminalMode = false
          connect()
          return
        }
        terminalDelay = TERMINAL_PROBE_MS
        scheduleTerminalProbe()
      }, failed)
    }
    const enterTerminalMode = () => {
      terminalMode = true
      terminalDelay = TERMINAL_PROBE_MS
      setConnected(true)
      scheduleTerminalProbe()
    }

    function connect() {
      terminalMode = false
      terminalRequest?.controller.abort()
      terminalRequest = null
      clearTimeout(timer)
      streamRef.current?.abort()
      const controller = new AbortController()
      streamRef.current = controller
      let terminal = false
      let acceptedTerminal = false
      const rejectLiveStream = () => {
        if (stopped || controller.signal.aborted) return
        const delay = backoff
        acceptedTerminal = false
        lastStreamEventId = ''
        setConnected(false)
        controller.abort()
        reconnect(delay)
        backoff = Math.min(backoff * 2, MAX_BACKOFF)
      }
      fetchEventStream(runApiPath(runId, '/events'), {
        signal: controller.signal,
        lastEventId: lastStreamEventId,
        onEvent: event => {
          if (stopped || controller.signal.aborted) return
          if (event.type === 'done') {
            if (!acceptedTerminal) {
              rejectLiveStream()
              return
            }
            // A finished run can be reopened later, but immediately reopening this terminal stream
            // would just receive `done` again forever. Switch to the small minute-scale identity probe.
            terminal = true
            controller.abort()
            enterTerminalMode()
            return
          }
          if (event.type !== 'state') return
          let p
          let next
          try {
            p = JSON.parse(event.data)
            if (event.lastEventId !== String(p?.seq)) {
              throw new Error('Run stream cursor does not match its snapshot.')
            }
            next = commitSnapshot(p)
          } catch {
            // A live connection is only healthy after an accepted snapshot. Do not acknowledge a
            // rejected frame's cursor; force a full resync and mark last-good data offline meanwhile.
            rejectLiveStream()
            return
          }
          acceptedTerminal = terminalSnapshot(p)
          // Numeric seq ids are scoped to one durable run generation. Empty/reset startup states have
          // no generation yet, so carrying their `-1` (or the prior generation's id) is ambiguous.
          lastStreamEventId = next[2] != null && event.lastEventId !== '' ? event.lastEventId : ''
          backoff = MIN_BACKOFF
          setConnected(true)
          setStatus('ready')
          setError(null)
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

    const scheduleReviewPoll = () => {
      if (stopped || reviewEnded || !reviewPoll || (reviewTerminal && hidden())) return
      clearTimeout(pollTimer)
      pollTimer = setTimeout(reviewPoll, reviewTerminal ? TERMINAL_PROBE_MS : pollMs)
    }
    const onVisibility = () => {
      if (hidden()) {
        if (terminalMode) {
          clearTimeout(timer)
          terminalRequest?.controller.abort()
        }
        if (reviewTerminal) clearTimeout(pollTimer)
        return
      }
      if (terminalMode) {
        clearTimeout(timer)
        probeTerminal()
      } else if (pollOnly && reviewTerminal && reviewPoll && !reviewPollRunning) {
        clearTimeout(pollTimer)
        reviewPoll()
      }
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }

    // Probe once before opening a self-reconnecting authenticated fetch stream. This turns a mistyped/deleted run
    // URL into an explicit 404 state instead of an endless "Connecting…" loop.
    initialRequest = deadlineGet(runApiPath(runId, '/state'))
    initialRequest.promise
      .then(p => {
        if (stopped) return
        commitSnapshot(p)
        setStatus('ready')
        setError(null)
        reviewRetryRef.current = 1500   // a good probe resets the review re-probe backoff
        if (!pollOnly) {
          if (terminalSnapshot(p)) enterTerminalMode()
          else connect()
        }
        else {
          setConnected(true)
          reviewTerminal = terminalSnapshot(p)
          reviewPoll = () => {
            if (stopped || reviewEnded || reviewPollRunning || (reviewTerminal && hidden())) return
            reviewPollRunning = true
            const request = deadlineGet(runApiPath(runId, '/state'))
            reviewRequest = request
            request.promise
              .then(next => {
                if (stopped) return
                commitSnapshot(next)
                reviewTerminal = terminalSnapshot(next)
                setConnected(true)
                setStatus('ready')
                setError(null)
              })
              .catch(error => {
                if (stopped) return
                const ended = error?.status === 401 || error?.status === 404 || error?.status === 410
                setConnected(false)
                if (ended) {
                  reviewEnded = true
                  setError('This review link expired, was revoked, or is invalid.')
                  setLive(null); setStatus('gone')
                  return
                }
                reviewTerminal = false
                setError(error?.message || 'Review refresh failed')
              })
              .finally(() => {
                if (reviewRequest === request) reviewRequest = null
                reviewPollRunning = false
                scheduleReviewPoll()
              })
          }
          scheduleReviewPoll()
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
      initialRequest?.controller.abort()
      reviewRequest?.controller.abort()
      terminalRequest?.controller.abort()
      streamRef.current?.abort()
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
    }
  }, [runId, retryToken, pollOnly, pollMs])

  // Publish only after React committed the snapshot. Updating this registry in the SSE callback would
  // create a small pre-render window where a click on visible generation A could be rebound to B.
  useLayoutEffect(() => { observeRunGeneration(runId, generation) }, [runId, generation])

  return { live, seq, generation, eventCount, connected, status, error,
    retry: () => setRetryToken(n => n + 1) }
}
