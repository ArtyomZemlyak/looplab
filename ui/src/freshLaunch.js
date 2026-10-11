// A run this tab has JUST started, so a 404 from it means "not readable yet", not "does not exist".
//
// doc 74 (found while driving the offline demo end to end, 2026-10-09): `/api/start` proves startup
// as soon as the engine process is spawned, and the launch card then opens the run at once — but
// `/api/runs/<id>/state` answers 404 until the engine has written its first event. RunView took that
// 404 as final and showed "Run not found" with a Retry button, on every launch from the UI, for a run
// that was by then running (and, for the offline demo, already finished).
//
// The fix is deliberately narrow: only a run THIS tab started, only for FRESH_LAUNCH_WINDOW_MS, is
// treated as starting; any other 404 still reads "Run not found" at once, so a mistyped run id is not
// turned into a minute of waiting. Session storage, because the fact belongs to this tab and must
// survive the hash navigation that opens the run, and nothing else. Every storage access is guarded:
// a tab with storage blocked loses only the friendlier wait, never the run view.
export const FRESH_LAUNCH_KEY = 'looplab.freshLaunch'
export const FRESH_LAUNCH_WINDOW_MS = 60_000
export const FRESH_LAUNCH_RETRY_MS = 1_000

const storage = () => {
  try { return globalThis.sessionStorage || null } catch { return null }
}

// One record PER RUN, `{ [runId]: startedAt }`: a single slot was overwritten by a second start
// from the same tab, and the first run, opened while still starting, read "Run not found" again
// (code review of the doc 74 work). Expired entries are dropped on every write, so the map stays
// as small as the runs started in the last minute.
const readAll = store => {
  const raw = JSON.parse(store.getItem(FRESH_LAUNCH_KEY) || '{}')
  return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
}

export function markFreshLaunch(runId, now = Date.now(), store = storage(), windowMs = FRESH_LAUNCH_WINDOW_MS) {
  if (!runId || !store) return
  try {
    let all = {}
    try { all = readAll(store) } catch { all = {} }
    const kept = Object.fromEntries(Object.entries(all).filter(([, at]) =>
      Number.isFinite(at) && now - at >= 0 && now - at <= windowMs))
    kept[String(runId)] = now
    store.setItem(FRESH_LAUNCH_KEY, JSON.stringify(kept))
  } catch { /* best effort */ }
}

export function isFreshLaunch(runId, now = Date.now(), store = storage(), windowMs = FRESH_LAUNCH_WINDOW_MS) {
  if (!runId || !store) return false
  let at
  try { at = readAll(store)[String(runId)] } catch { return false }
  if (!Number.isFinite(at)) return false
  const age = now - at
  return age >= 0 && age <= windowMs
}

export function clearFreshLaunch(runId, store = storage()) {
  if (!store) return
  try {
    if (!runId) { store.removeItem(FRESH_LAUNCH_KEY); return }
    const all = readAll(store)
    delete all[String(runId)]
    if (Object.keys(all).length) store.setItem(FRESH_LAUNCH_KEY, JSON.stringify(all))
    else store.removeItem(FRESH_LAUNCH_KEY)
  } catch { /* best effort */ }
}

// Is the "Starting the run…" screen shown instead of "Run not found"? The ONE rule RunView renders
// by. `latchedFor` is the run id an effect latched across the retry's `loading` flips; `fresh` is
// `isFreshLaunch(runId)` read on THIS render. The render that FIRST sees `not_found` has no latch
// yet — the effect sets it after commit — so without `fresh` it painted "Run not found" for one
// frame and moved focus (code review).
export function freshLaunchShown({ reviewMode, live, runStatus, runId, latchedFor, fresh }) {
  if (reviewMode || live) return false
  if (runStatus === 'not_found' && fresh) return true
  return latchedFor === runId && (runStatus === 'not_found' || runStatus === 'loading')
}
