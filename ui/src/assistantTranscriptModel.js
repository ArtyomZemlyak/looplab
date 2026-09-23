// The Assistant transcript's IDENTITY rules (review 2026-09-22, UI-06) — the pure half of keeping a
// settled message from re-rendering while another one streams.
//
// A reply streams into the LAST message: every chunk replaces that one message (`AssistantBar.jsx`'s
// `patchLast` copies the array and swaps one element) and re-renders the bar that owns the list. A
// `Turn` that is not memoized then re-runs for EVERY message on screen — its Markdown's inline pass
// and element tree rebuilt and reconciled — once per token of somebody else's reply: measured at
// 320 `Turn` renders for 20 chunks into a 16-message transcript
// (`test/assistantTranscriptRenders.test.js`), where one chunk now costs one render, the streaming
// message's. `React.memo` stops that only if
// every prop a settled Turn is handed survives the chunk's render with the same identity. The two
// decisions that make that true without changing what any Turn shows live here, where `node --test`
// drives them (`test/assistantTranscriptModel.test.js`); the component keeps only the wiring.

// ── 1. what a Turn is memoized on ──────────────────────────────────────────────────────────────────
// IDENTITY for every prop — a changed message, flag or callback re-renders — with one exception:
// `launchChat`, which `launchProvenance.js::proposalLaunchChat` rebuilds as a fresh array for every
// message on every render of the bar (a message with no Genesis command before it gets a fresh `[]`),
// and which its only reader, `LaunchCard`, uses by CONTENT alone (`launchFingerprint`,
// `buildLaunchBody`). Two chats are the same when they hold the same rows and each row is
// shallow-equal over ALL its fields — not a chosen two — so a field added to a row later can never be
// compared away into a card that launches with a stale conversation.
const sameRow = (previous, next) => {
  if (Object.is(previous, next)) return true
  if (!previous || !next || typeof previous !== 'object' || typeof next !== 'object') return false
  const keys = Object.keys(previous)
  return keys.length === Object.keys(next).length
    && keys.every(key => Object.hasOwn(next, key) && Object.is(previous[key], next[key]))
}

export function sameLaunchChat(previous, next) {
  if (Object.is(previous, next)) return true
  if (!Array.isArray(previous) || !Array.isArray(next) || previous.length !== next.length) return false
  return previous.every((row, index) => sameRow(row, next[index]))
}

export function turnPropsEqual(previous, next) {
  const keys = Object.keys(previous)
  if (keys.length !== Object.keys(next).length) return false
  return keys.every(key => Object.hasOwn(next, key) && (key === 'launchChat'
    ? sameLaunchChat(previous[key], next[key])
    : Object.is(previous[key], next[key])))
}

// ── 2. per-message handlers with a stable face ─────────────────────────────────────────────────────
// A handler that closes over the bar's render (a message's Retry reads the transcript, the share and
// fork gates of THAT render) is a new function every render, so a memoized Turn cannot be handed it
// directly. It is handed a FACE instead: a function that never changes and calls whatever handler the
// LATEST render published for that message — exactly the closure a re-rendered Turn would have held.
//
//   * `publish(handlers)` adopts one render's handlers, an array indexed like the transcript,
//     REPLACING the previous render's wholesale. A face therefore never keeps a departed render's
//     closure (and the transcript it closed over) alive after the list shrinks.
//   * `face(index)` is `null` exactly when the latest render published no handler at `index`. The
//     Retry button renders only when its prop is present, so presence must stay a prop change: a
//     message whose Retry becomes unavailable re-renders and loses the button, as before.
export function latestHandlers() {
  let latest = []
  const faces = []
  return {
    publish(handlers) { latest = Array.isArray(handlers) ? handlers : [] },
    face(index) {
      if (typeof latest[index] !== 'function') return null
      if (!faces[index]) faces[index] = (...args) => latest[index]?.(...args)
      return faces[index]
    },
  }
}
