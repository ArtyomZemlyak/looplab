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

export function markFreshLaunch(runId, now = Date.now(), store = storage()) {
  if (!runId || !store) return
  try { store.setItem(FRESH_LAUNCH_KEY, JSON.stringify({ runId: String(runId), at: now })) } catch { /* best effort */ }
}

export function isFreshLaunch(runId, now = Date.now(), store = storage(), windowMs = FRESH_LAUNCH_WINDOW_MS) {
  if (!runId || !store) return false
  let record = null
  try { record = JSON.parse(store.getItem(FRESH_LAUNCH_KEY) || 'null') } catch { return false }
  if (!record || record.runId !== String(runId) || !Number.isFinite(record.at)) return false
  const age = now - record.at
  return age >= 0 && age <= windowMs
}

export function clearFreshLaunch(runId, store = storage()) {
  if (!store) return
  try {
    const record = JSON.parse(store.getItem(FRESH_LAUNCH_KEY) || 'null')
    if (!runId || record?.runId === String(runId)) store.removeItem(FRESH_LAUNCH_KEY)
  } catch { /* best effort */ }
}
