// The React half of the shared resource machine (doc 25 UI-06); the transitions themselves are in
// resourceModel.js. It is its own module because the review gate in App needs this one small hook,
// while hooks.js also owns the whole run-state/SSE/trace stack. Importing that aggregate from the
// initial shell made every run-only helper eager and erased the route boundary the hook was meant to
// share across.
import { useEffect, useRef, useState } from 'react'

import { DEFAULT_REQUEST_TIMEOUT_MS, deadlineRequest } from './requestDeadline.js'
import {
  resourceBegin, resourceCancel, resourceGated, resourceInitial, resourceSettle, resourceView,
} from './resourceModel.js'

// This owns the parts that need a component: ONE scope-owned in-flight request, the abort on scope
// change and unmount, and the optional poll.
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
