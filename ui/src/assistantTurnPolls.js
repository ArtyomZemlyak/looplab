import { shouldSurfaceProgress } from './assistantTurnModel.js'

// The two concurrent fallback polls that run BESIDE one Assistant turn's SSE stream (doc 25 UI-05
// names them as the reason `runLLM` is hard to read). They are here, together, because they share
// one lifetime and one ownership fence, and inside `runLLM` that was invisible: two bare
// `;(async () => { … })()` blocks a hundred lines apart, closing over a `let polling` that a
// `finally` far below flipped.
//
//   * PERMISSIONS — the turn may ask for approval mid-flight; the cards come from a poll, not the
//     stream.
//   * PROGRESS — the SSE fallback. Behind a buffering proxy (jupyter-server-proxy / nginx) the
//     token/text/step events arrive batched only at the END, leaving a dead "thinking" bubble the
//     whole time. So the server's mirrored answer-so-far is polled and surfaced while the stream has
//     produced less than it (`assistantTurnModel.js::shouldSurfaceProgress`); once tokens flow, the
//     authoritative SSE content wins. It fills the buffered gap; it never fights a working stream.
//
// Two properties are what this module exists to make checkable, and both are about a LATE result:
// every await is followed by `isCurrent()` before anything is published, so a poll that resolves
// after a session switch or a Stop cannot paint the departed turn's text over the current one; and
// `stop()` is final — a loop that has been stopped publishes nothing, even if a request it already
// issued resolves successfully afterwards.
//
// It is deliberately not a hook: it owns no React state, and `runLLM` already owns the lifetime.
export function startTurnFallbackPolls({
  isCurrent, readPermissions, onPermissions, readProgress, onProgress,
  streamedText = () => '', sleep, permissionIntervalMs = 800, progressIntervalMs = 1000,
}) {
  let polling = true
  const live = () => polling && isCurrent()

  // sid-guarded like every other callback: after a mid-turn session switch, a late poll result must
  // not surface the DEPARTED session's confirm-cards over the one the user switched to.
  const permissions = (async () => {
    while (live()) {
      try {
        const snapshot = await readPermissions()
        if (snapshot?.ok && live()) onPermissions(snapshot.pending)
      } catch { /* transient */ }
      await sleep(permissionIntervalMs)
    }
  })()

  const progress = (async () => {
    while (live()) {
      await sleep(progressIntervalMs)
      if (!live()) break
      try {
        const seen = await readProgress()
        if (!live()) break
        if (!shouldSurfaceProgress(streamedText(), seen)) continue
        onProgress(seen)
      } catch { /* transient */ }
    }
  })()

  // Returns the two loops so a test (and only a test) can await their settlement; the turn itself
  // calls `stop()` in its `finally` and never waits.
  return { stop: () => { polling = false }, settled: () => Promise.all([permissions, progress]) }
}
