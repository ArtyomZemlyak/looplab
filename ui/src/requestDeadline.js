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
