// `normalizeRunGeneration` is defined in commandModel.js, but it is imported HERE through the api.js
// barrel on purpose: `apiBarrel.test.js::only the barrel and its own members import the extracted
// modules` refuses a second reachability root into the split members, because that is what lets the
// bundler pull the barrel's chunk apart. Reaching for the leaf directly looks tidier and is the
// change that guard exists to catch.
import { normalizeRunGeneration } from './api.js'

// The RULES of the run-state connection (doc 25 UI-09), lifted out of `hooks.js::useRunState`'s one
// long effect. That effect interleaves three connection machines — the owner SSE stream, the
// minute-scale terminal lifecycle probe, and the review poll — over a bank of mutable closure
// variables, and every decision they make was spelled INSIDE it: what makes a payload a valid run
// snapshot, when a snapshot has moved backwards, which HTTP status ends a run versus merely
// interrupts it, how far each backoff ramp may climb. None of those could be reached by a test
// without standing up a React root, a jsdom document, a fake `fetch` and a fake clock first, so in
// practice none of them was covered: `runStateTerminalPolling.test.js` drives ONE of the three
// machines and touches exactly one of these rules in passing.
//
// This is the PURE half, the same split `resourceModel.js` / `useScopedResource` already uses:
// transitions with no React and no I/O, so the rules can be stated and driven directly. `useRunState`
// stays the React half and remains the only thing that decides WHEN a rule is consulted — the effect
// keeps its timers, its aborts and its ordering, because those are choreography, not rules.
//
// Deliberately NOT moved (the boundary is "a decision, not a step"): the reconnect/abort choreography,
// the visibility wiring, and the setState fan-out. A machine object with injected timers and fetchers
// — what the finding proposed — would have to re-derive React's own effect lifetime, and the
// re-derivation is where the ordering bugs this code has already been hardened against would come
// back (the cursor reset on a rejected frame, the abort-before-reconnect, the terminal-probe fence).

// Reconnect backoff: behind a proxy a hard drop/504 on the GET (or a keepalive-starved idle drop)
// would otherwise retry on a fixed 1.5s tick forever — a GET storm that re-folds the run each time.
// Ramp 1.5s → ×2 → 30s cap; a live `state` frame proves the stream works and resets it.
export const MIN_BACKOFF_MS = 1500
export const MAX_BACKOFF_MS = 30000
// A finished run is probed on a minute scale instead of holding a stream open, ramping to five
// minutes while the probe keeps failing. Both ceilings are here so the review path's own re-probe
// ramp cannot drift from the owner path's by being spelled twice.
export const TERMINAL_PROBE_MS = 60000
export const TERMINAL_PROBE_MAX_MS = 300000

// One ramp for every backoff in this subsystem: double, then stop at the cap.
export const nextBackoffMs = (delay, cap = MAX_BACKOFF_MS) => Math.min(delay * 2, cap)

export const normalizeEventCount = value => {
  if (value == null) return null // additive field: tolerate a legacy server during rolling upgrades
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined
}

// The identity a snapshot (or a compact `/lifecycle` probe) is judged by: `[seq, alive, generation,
// eventCount]`. Anything that is not a well-formed payload throws rather than returning a sentinel —
// the callers treat a throw as "reject this frame and resync", and a silent partial identity would be
// indistinguishable from a run that genuinely did not move.
export function runSnapshotIdentity(payload, probe = false) {
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

export const identityChanged = (previous, next) => next[0] !== previous[0] || next[1] !== previous[1]
  || next[2] !== previous[2] || next[3] !== previous[3]

// Monotonicity is only meaningful WITHIN one durable generation: a reset re-spawns the run with a
// fresh generation whose seq legitimately restarts at -1, and rejecting that would strand the
// workspace on the archived generation's last frame.
export const snapshotMovedBackwards = (previous, next) => {
  const sameGeneration = next[2] != null && next[2] === previous[2]
  return sameGeneration && (next[0] < previous[0]
    || (next[3] != null && previous[3] != null && next[3] < previous[3]))
}

// The initial identity: a seq no real frame can carry, so the first accepted snapshot always commits.
export const initialRunIdentity = () => [-2, undefined, null, null]

export const terminalSnapshot = payload => payload?.state?.finished === true
  && payload.state.engine_running === false && payload.state.phase !== 'finalizing'

// A run that no longer exists for the OWNER. 401 is deliberately absent: an owner whose token blipped
// must reconnect, not be told the run was removed.
export const ownerRunEnded = error => error?.status === 404 || error?.status === 410

// A review link that no longer grants the run. 401 IS terminal here — a reviewer's grant is the only
// thing that authorized the read, so its withdrawal is the end of the link, not a transient blip.
export const reviewLinkEnded = error => error?.status === 401 || error?.status === 404
  || error?.status === 410

// The one-shot probe that precedes any stream or poll. Three outcomes, and the difference between
// them is what the operator is told AND whether anything is left scheduled to retry:
//   'gone'        — a review link expired, was revoked, or is invalid.
//   'not_found'   — this run does not exist or was removed. 410 is terminal for the owner but NOT for
//                   a review link that has already been classified 'gone' above.
//   'transient'   — a proxy 504, a dropped connection, a keepalive-starved idle drop: do NOT strand
//                   the workspace on an error screen with nothing scheduled to retry (UI-2).
export function classifyRunProbeFailure(error, pollOnly = false) {
  const status = error?.status
  if (pollOnly && reviewLinkEnded(error)) return 'gone'
  if (status === 404 || (!pollOnly && status === 410)) return 'not_found'
  return 'transient'
}

// A frame's cursor must name the snapshot it arrived with, or the stream and the state have diverged.
export const streamCursorMatchesSnapshot = (lastEventId, payload) =>
  lastEventId === String(payload?.seq)

// Numeric seq ids are scoped to one durable run generation. Empty/reset startup states have
// no generation yet, so carrying their `-1` (or the prior generation's id) is ambiguous.
export const nextStreamCursor = (lastEventId, identity) =>
  identity[2] != null && lastEventId !== '' ? lastEventId : ''
