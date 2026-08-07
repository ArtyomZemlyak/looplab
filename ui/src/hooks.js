import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  deadlineGet, fetchEventStream, observeRunGeneration, runApiPath,
} from './api.js'
import { withBuilding } from './buildingModel.js'
import { DEFAULT_REQUEST_TIMEOUT_MS, deadlineRequest } from './requestDeadline.js'
import {
  resourceBegin, resourceCancel, resourceGated, resourceInitial, resourceSettle, resourceView,
} from './resourceModel.js'
import {
  COMMAND_POLL_MAX_TRANSIENT, COMMAND_POLL_REPEAT_MS, COMMAND_POLL_START_MS,
  commandIntentPreserved, commandPollBackoffMs, observeCommandError,
} from './runCommandMachine.js'
import {
  MIN_BACKOFF_MS, TERMINAL_PROBE_MAX_MS, TERMINAL_PROBE_MS,
  classifyRunProbeFailure, identityChanged, initialRunIdentity, nextBackoffMs, nextStreamCursor,
  ownerRunEnded, reviewLinkEnded, runSnapshotIdentity, snapshotMovedBackwards,
  streamCursorMatchesSnapshot, terminalSnapshot,
} from './runStateModel.js'
import {
  NODE_TRACE_SPAN_WINDOW, NODE_TRACE_SPAN_WINDOW_MAX, nextNodeSpanWindow,
} from './traceProjection.js'
import { armTraceScroll, shouldWidenOnReach } from './traceScrollModel.js'

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

// The ONE per-node "load more" window, shared by BOTH surfaces that page a node's trace: the chat
// feed's inline waterfall (Dock) and the Inspector's Trace tab (span tree AND conversation, which
// share one window per node — an operator asking for more of a node means the node, not one of its
// two readings). It owns the whole rule, so a consumer cannot hold a correct limit with a wrong
// ceiling, or a correct ceiling it forgets to stop at:
//   * the starting window and the doubling step (traceProjection.js, mirroring the server constants),
//   * `canPage`, which is also what the traceWindow/conversationWindow rules key their ACTIONABLE
//     branch on — so "there is a button" and "the button can do something" are one fact;
//   * `loadMore` going UNDEFINED at the ceiling. Returning a dead callback is how a pager keeps
//     rendering after it stops working, which is the failure this whole change exists to remove.
// The Inspector had none of this and rendered a dead "Trace projection is partial." notice over the
// same routes the Dock was already paging; the constants living in two files is what let that happen.
export function useNodeSpanWindow() {
  const [limit, setLimit] = useState(NODE_TRACE_SPAN_WINDOW)
  const canPage = limit < NODE_TRACE_SPAN_WINDOW_MAX
  return {
    limit,
    canPage,
    loadMore: canPage ? () => setLimit(nextNodeSpanWindow) : undefined,
  }
}

// The CHOREOGRAPHY half of ./traceScrollModel.js — everything that needs a DOM and nothing that
// needs a decision. Attaches an IntersectionObserver to the sentinel a trace surface renders at the
// OLD end of its thread (every cap in these projections keeps the newest tail, so what is missing is
// always the oldest), and calls `onReach` when the operator scrolls it into view.
//
// Three details that are load-bearing and not obvious:
//  * The observer is attached through a CALLBACK REF, not an effect. The sentinel mounts and
//    unmounts as the surface's state changes (and the conversation re-renders on a 4 s poll while a
//    node is live), and an effect with a stable dep list would attach to a node that was not there
//    yet and never see the one that arrives.
//  * `latest` holds the current state/callback so the observer never has to be re-created. Rebuilding
//    it per render would fire IntersectionObserver's initial callback on every poll tick — a widen
//    per tick instead of per gesture.
//  * The re-arm listener is on `window` in the CAPTURE phase because `scroll` does not bubble. That
//    is what lets this hook stay ignorant of which element actually scrolls (`.insp-body` in the
//    Inspector, the dock body in the chat feed) instead of hunting for a scroll parent.
// Deliberately NO scroll anchoring after a widen: the operator reached the sentinel by scrolling to
// the top, so leaving `scrollTop` where it is puts the newly revealed OLDER steps exactly where they
// were looking. Restoring their old offset instead would push what they just asked for off-screen,
// and would fight `StageLog`'s live auto-tail for the same scroll container.
export function useTraceScroll({ state, onReach, failed = false }) {
  const observer = useRef(null)
  const armed = useRef(true)
  const latest = useRef(null)
  latest.current = { state, onReach }
  // A widen that FAILED must spend the budget, and this is not the same as the spend at reach time:
  // the operator goes on scrolling while the read is in flight, which re-arms it. Without this, a
  // route that is failing gets re-asked on the very next intersection, forever, at seconds per call.
  useEffect(() => {
    if (failed) armed.current = armTraceScroll(armed.current, 'fail')
  }, [failed])
  const reach = () => {
    const now = latest.current || {}
    if (!shouldWidenOnReach({ state: now.state, armed: armed.current })) return
    armed.current = armTraceScroll(armed.current, 'reach')
    now.onReach?.()
  }
  const sentinelRef = useCallback(node => {
    observer.current?.disconnect()
    observer.current = null
    if (!node || typeof IntersectionObserver !== 'function') return
    observer.current = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) reach()
    // Start the read a little BEFORE the sentinel is on screen, so the earlier steps are usually
    // already there by the time the operator scrolls to where they belong.
    }, { rootMargin: '200px 0px 0px 0px' })
    observer.current.observe(node)
  }, [])
  useEffect(() => {
    const onScroll = () => { armed.current = armTraceScroll(armed.current, 'scroll') }
    window.addEventListener('scroll', onScroll, true)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      observer.current?.disconnect()
      observer.current = null
    }
  }, [])
  return {
    sentinelRef,
    // The keyboard/AT path. Focus REFILLS the budget before spending it, so tabbing to the
    // affordance always works — a screen-reader user driving a virtual cursor never fires the
    // scroll listener that refills it for everyone else.
    onReachFocus: () => {
      armed.current = armTraceScroll(armed.current, 'focus')
      reach()
    },
  }
}

// The ONE durable-command status poll (doc 25 UI-01), replacing the two byte-identical effects Dock
// and AssistantBar each carried for the run transport / direct commands. It owns exactly what was the
// same on both sides: the accepted-or-executing gate, the per-effect `active` latch, the transient
// failure budget, and the cadence in ./runCommandMachine.js. What it deliberately does NOT own is what
// a tick MEANS — the two surfaces accept a record, preserve an unknown outcome and settle a failure
// differently, so each hands in its own three callbacks.
//
//   command        — the record being observed. Polling runs only while it is accepted/executing.
//   paused         — the surface's own "do not observe" state (an unavailable/retrying/checking entry).
//   observe(cmd)   — the status read. Rejections are classified, never rendered raw.
//   onRecord(rec)  — commit a fresh record; return truthy to keep polling, falsy to stop (terminal).
//   onUnobservable — the outcome is UNKNOWN (transport budget exhausted, or access/protocol). The
//                    durable intent must survive; this is never the same thing as a failure.
//   onFailed       — an authoritative failure for this client ('missing'/'request').
export function useCommandStatusPoll({ runId, command, paused, observe, onRecord,
  onUnobservable, onFailed }) {
  // The callbacks close over the surface's render state, and the effect below must use the ones from
  // the render that STARTED it — exactly like the hand-written effects, whose closures were frozen at
  // effect time and kept for the life of that poll. Reading the ref per tick would silently upgrade
  // that to latest-render semantics, which is a different (and untested) machine.
  const handlers = useRef(null)
  handlers.current = { observe, onRecord, onUnobservable, onFailed }
  useEffect(() => {
    if (paused || !command?.id
        || (command.status !== 'accepted' && command.status !== 'executing')) return
    const bound = handlers.current
    let active = true, timer = null
    let transientFailures = 0
    const schedule = delay => { if (active) timer = setTimeout(poll, delay) }
    const poll = async () => {
      try {
        const record = await bound.observe(command)
        if (!active) return
        transientFailures = 0
        if (!bound.onRecord(record)) return
        schedule(COMMAND_POLL_REPEAT_MS)
      } catch (error) {
        if (!active) return
        const kind = observeCommandError(error)
        if (kind === 'transport') {
          transientFailures += 1
          if (transientFailures < COMMAND_POLL_MAX_TRANSIENT) {
            schedule(commandPollBackoffMs(error, transientFailures))
            return
          }
          bound.onUnobservable(error, kind, command)
          return
        }
        if (commandIntentPreserved(kind)) { bound.onUnobservable(error, kind, command); return }
        bound.onFailed(error, kind, command)
      }
    }
    timer = setTimeout(poll, COMMAND_POLL_START_MS)
    return () => { active = false; clearTimeout(timer) }
    // `paused` stands in for the surface's individual pending flags: whenever the effect is LIVE they
    // are all false, so any change to one of them flips `paused` and restarts the effect exactly as
    // the per-flag dependency lists did. A change between two paused states restarts nothing, and
    // could not have mattered — the body returns immediately while paused.
  }, [runId, command?.id, !!paused])
}

// The React half of the shared resource machine (doc 25 UI-06); the transitions themselves are in
// ./resourceModel.js. This owns the parts that need a component: ONE scope-owned in-flight request,
// the abort on scope change and unmount, and the optional poll.
//
// `read(signal)` performs the request. Everything else is optional:
//   scope            — the identity string. A scope change resets the resource and starts a new
//                      read; a response for a scope that is no longer current never commits.
//   gate             — a status string that means "do not read at all" ('idle', 'waiting',
//                      'restricted'). It doubles as the status a render sees for a scope the state
//                      does not carry yet, so the gate and the fence can never disagree.
//   validate(value)  — returns a message when a transport-successful payload is not the resource
//                      (HTTP 200 proves transport, not truth); null/'' accepts it.
//   classifyFailure  — `({ transport, message, error, intent, lastGood }) => { status, error, data }`,
//                      the caller's failure vocabulary. Omitted, a failure is 'stale' when there is
//                      last-good data and 'error' when there is not, with no message.
//   onSuccess(scope) — a side effect to run before a successful commit (Inspector releases its
//                      trace-clear fence here); `request(...)`'s `onSettled` covers both outcomes.
//                      It is handed the SCOPE the settling read belongs to, never the render's:
//                      a response can land between a re-render and the effect that services it, and
//                      a fence keyed by the wrong scope releases the wrong node's.
//   pollMs           — refresh interval. Ticks skip an active request rather than queueing.
//   deps             — extra effect dependencies, for a read whose URL depends on something the
//                      scope deliberately does not carry. Its LENGTH must be constant across
//                      renders, like any dependency array.
//
// Returns the fenced view plus `retry(options)` and `request(intent, options)`, where options are
// `{ supersede, mapLastGood, onSettled }`. `supersede` aborts an in-flight read instead of being
// refused by it: an explicit user retry owns freshness over an invisible background refresh, which
// guarantees immediate busy feedback and stops the older response from settling the newer intent.
export function useScopedResource(read, {
  scope = '', timeout = DEFAULT_REQUEST_TIMEOUT_MS, gate = null, validate = null,
  classifyFailure = null, onSuccess = null, pollMs = null, deps = [],
} = {}) {
  const [value, setValue] = useState(() => resourceInitial(null, gate || 'loading'))
  const flight = useRef(null)
  const startRef = useRef(null)
  // Every call site re-creates these callbacks each render (they close over props). Reading them
  // through a ref is what keeps the effect keyed on the SCOPE — re-running it on callback identity
  // would restart the request on every parent render — while still letting a poll tick minutes later
  // use the current render's reader instead of one frozen at effect time. They are then frozen for
  // the LIFETIME OF ONE REQUEST, so a read, the validation of its response and the wording of its
  // failure always come from the same render rather than from whichever one happened to be current
  // when the response landed.
  const latest = useRef(null)
  latest.current = { read, validate, classifyFailure, onSuccess }
  useEffect(() => {
    let alive = true
    const owner = {}
    if (gate) {
      setValue(resourceGated(scope, gate))
      startRef.current = null
      return () => { alive = false }
    }
    const start = (intent = 'refresh', { supersede = false, mapLastGood = null, onSettled = null } = {}) => {
      if (supersede && flight.current) {
        const obsolete = flight.current
        flight.current = null
        obsolete.controller.abort()
      }
      if (flight.current) return false
      const bound = latest.current
      const timed = deadlineRequest(signal => bound.read(signal), timeout)
      const request = { owner, controller: timed.controller, promise: timed.promise }
      flight.current = request
      setValue(previous => resourceBegin(previous, { scope, intent, mapLastGood }))
      const settle = (ok, data, failure) => {
        if (flight.current !== request) return
        flight.current = null
        if (!alive) return
        if (ok) bound.onSuccess?.(scope)
        setValue(previous => resourceSettle(previous, {
          scope, ok, data, failure, classify: bound.classifyFailure,
        }))
        onSettled?.(ok)
      }
      timed.promise.then(payload => {
        const invalid = bound.validate ? bound.validate(payload) : null
        if (!invalid) settle(true, payload, null)
        else settle(false, null, { transport: false, message: invalid, error: null, intent })
      }, error => {
        if (error?.name !== 'AbortError') {
          settle(false, null, { transport: true, message: '', error, intent })
          return
        }
        if (flight.current !== request) return
        flight.current = null
        if (!alive) return
        setValue(previous => resourceCancel(previous, scope))
      })
      return request
    }
    startRef.current = { scope, start }
    start('load')
    const timer = pollMs == null ? null : setInterval(start, pollMs)
    return () => {
      alive = false
      if (timer != null) clearInterval(timer)
      if (startRef.current?.start === start) startRef.current = null
      if (flight.current?.owner === owner) {
        flight.current.controller.abort()
        flight.current = null
      }
    }
    // deps come from the caller (they list what their read depends on beyond the scope)
  }, [scope, gate, pollMs, timeout, ...deps])
  const request = (intent = 'refresh', options) => {
    const current = startRef.current
    return current?.scope === scope ? current.start(intent, options) : false
  }
  return {
    ...resourceView(value, scope, gate || 'loading'),
    request,
    retry: (options) => request('retry', options),
  }
}

// `withBuilding` (the synthetic building-node splice) lives in ./buildingModel.js so it can be unit
// tested without React; imported at the top of this module.

// Subscribe to a run's live folded state over SSE. The server emits `event: state` frames whose
// data is { state, seq, generation, event_count? }. Returns the latest live state + connection
// status; event_count is optional only for compatibility with a legacy server. Auto-reconnects.
//
// The DECISIONS this effect makes — snapshot validation, monotonicity, which status ends a run,
// where each backoff ramp stops — live in ./runStateModel.js (doc 25 UI-09) so they can be stated
// and driven without a React root. What stays here is the choreography: the timers, the aborts, the
// ordering between the three connection machines and the setState fan-out.
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
  const reviewRetryRef = useRef(MIN_BACKOFF_MS)

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let timer = null
    let pollTimer = null
    let last = initialRunIdentity()
    let lastStreamEventId = ''
    let terminalMode = false
    let terminalDelay = TERMINAL_PROBE_MS
    let terminalRequest = null
    let ownerEnded = false
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
    let backoff = MIN_BACKOFF_MS
    const hidden = () => typeof document !== 'undefined' && document.hidden
    const commitSnapshot = payload => {
      const next = runSnapshotIdentity(payload)
      if (snapshotMovedBackwards(last, next)) throw new Error('Run snapshot moved backwards.')
      if (!identityChanged(last, next)) return next
      last = next
      setGenerationState({ runId, value: next[2] })
      setEventCountState({ runId, value: next[3] })
      setLive(withBuilding(payload.state))
      setSeq(next[0])
      return next
    }
    const endOwnerRun = () => {
      if (stopped || ownerEnded) return
      ownerEnded = true
      terminalMode = false
      clearTimeout(timer)
      terminalRequest?.controller.abort()
      terminalRequest = null
      streamRef.current?.abort()
      setConnected(false)
      setLive(null)
      setSeq(-1)
      setGenerationState({ runId, value: null })
      setEventCountState({ runId, value: null })
      setStatus('not_found')
      setError('This run does not exist or was removed.')
    }
    const reconnect = (delay) => {
      if (stopped || ownerEnded) return
      clearTimeout(timer); timer = setTimeout(connect, delay)
    }

    const scheduleTerminalProbe = (delay = terminalDelay) => {
      if (stopped || ownerEnded || !terminalMode || hidden()) return
      clearTimeout(timer)
      timer = setTimeout(probeTerminal, delay)
    }
    function probeTerminal() {
      if (stopped || ownerEnded || !terminalMode || hidden() || terminalRequest) return
      const request = deadlineGet(runApiPath(runId, '/lifecycle'))
      terminalRequest = request
      const failed = error => {
        if (stopped || !terminalMode || terminalRequest !== request) return
        if (ownerRunEnded(error)) { endOwnerRun(); return }
        terminalRequest = null
        setConnected(false)
        terminalDelay = nextBackoffMs(terminalDelay, TERMINAL_PROBE_MAX_MS)
        scheduleTerminalProbe()
      }
      request.promise.then(payload => {
        if (stopped || !terminalMode || terminalRequest !== request) return
        let next
        try { next = runSnapshotIdentity(payload, true) } catch { failed(); return }
        terminalRequest = null
        setConnected(true)
        if (identityChanged(last, next)) {
          terminalMode = false
          connect()
          return
        }
        terminalDelay = TERMINAL_PROBE_MS
        scheduleTerminalProbe()
      }, failed)
    }
    const enterTerminalMode = () => {
      if (ownerEnded) return
      terminalMode = true
      terminalDelay = TERMINAL_PROBE_MS
      setConnected(true)
      scheduleTerminalProbe()
    }

    function connect() {
      if (ownerEnded) return
      terminalMode = false
      terminalRequest?.controller.abort()
      terminalRequest = null
      clearTimeout(timer)
      streamRef.current?.abort()
      const controller = new AbortController()
      streamRef.current = controller
      let terminal = false
      let acceptedTerminal = false
      const rejectLiveStream = error => {
        if (stopped || controller.signal.aborted) return
        if (ownerRunEnded(error)) { endOwnerRun(); return }
        const delay = backoff
        acceptedTerminal = false
        lastStreamEventId = ''
        setConnected(false)
        controller.abort()
        reconnect(delay)
        backoff = nextBackoffMs(backoff)
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
            if (!streamCursorMatchesSnapshot(event.lastEventId, p)) {
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
          lastStreamEventId = nextStreamCursor(event.lastEventId, next)
          backoff = MIN_BACKOFF_MS
          setConnected(true)
          setStatus('ready')
          setError(null)
        },
      }).then(({ retry }) => {
        if (stopped || terminal || controller.signal.aborted) return
        setConnected(false)
        reconnect(retry ?? backoff)
        backoff = nextBackoffMs(backoff)
      }).catch(error => {
        if (stopped || terminal || controller.signal.aborted || error?.name === 'AbortError') return
        if (ownerRunEnded(error)) { endOwnerRun(); return }
        setConnected(false)
        reconnect(backoff)
        backoff = nextBackoffMs(backoff)
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
        reviewRetryRef.current = MIN_BACKOFF_MS   // a good probe resets the review re-probe backoff
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
                setConnected(false)
                if (reviewLinkEnded(error)) {
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
        const outcome = classifyRunProbeFailure(e, pollOnly)
        if (outcome === 'gone') {
          setStatus('gone'); setError('This review link expired, was revoked, or is invalid.'); return
        }
        if (outcome === 'not_found') {
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
          reviewRetryRef.current = nextBackoffMs(delay)   // ramp like the owner path
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
