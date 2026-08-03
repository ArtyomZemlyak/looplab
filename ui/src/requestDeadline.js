// Four shapes of "fetch with a deadline" coexisted and components picked among them ad hoc
// (doc 25 UI-12). Two are genuinely different and both stay; the other two are conveniences that
// now live here rather than being re-declared per component:
//
// * `deadlineRequest(read, ms)` — THE primitive. Returns the handle `{controller, promise,
//   timedOut()}`, so a caller that must cancel on unmount, or distinguish "timed out" from
//   "aborted", has what it needs. Reach for this when the request outlives the call site.
// * `boundedRequest(read, ms)` — the same thing when the caller only awaits the result. Most
//   component reads are this; spelling `.promise` at every site was what made it look like a
//   different mechanism.
// * `api.js::commandFetch` — deliberately NOT built on this. It bounds a durable COMMAND
//   submission, where the body read is part of the operation and a timeout must surface as a typed
//   `COMMAND_REQUEST_TIMEOUT` the command lifecycle understands. Its own comment explains the
//   body-read lifetime; do not collapse the two.
// * `api.js::deadlineGet` / `deadlineSharedAssistant` — path-level wrappers over the primitive, so
//   a caller naming a URL does not also have to remember `cache: 'no-store'` or which principal
//   the share route takes.
export const DEFAULT_REQUEST_TIMEOUT_MS = 12_000

// One abortable read with a hard liveness bound. It settles exactly once even when a transport
// ignores AbortSignal; callers still fence results by their own request identity.
export function deadlineRequest(read, timeout) {
  const controller = new AbortController()
  let timer
  let timedOut = false
  let settled = false
  let onAbort
  const promise = new Promise((resolve, reject) => {
    const finish = (ok, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      controller.signal.removeEventListener('abort', onAbort)
      ok ? resolve(value) : reject(value)
    }
    onAbort = () => {
      if (timedOut) return
      const error = new Error('request aborted')
      error.name = 'AbortError'
      finish(false, error)
    }
    controller.signal.addEventListener('abort', onAbort, { once: true })
    timer = setTimeout(() => {
      timedOut = true
      controller.abort()
      const error = new Error('request timed out')
      error.name = 'TimeoutError'
      finish(false, error)
    }, timeout)
    Promise.resolve().then(() => read(controller.signal)).then(
      value => finish(true, value), error => finish(false, error),
    )
  })
  return { controller, promise, timedOut: () => timedOut }
}

// The awaited form. Identical bound, no handle — for the ordinary component read that either
// resolves or throws. It was a private one-liner in AssistantBar and had already been re-declared
// (differently) in CollabPanel, which is how a shared default drifts into two spellings.
export const boundedRequest = (read, ms = DEFAULT_REQUEST_TIMEOUT_MS) =>
  deadlineRequest(read, ms).promise
