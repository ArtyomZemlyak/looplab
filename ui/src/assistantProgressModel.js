import { shouldSurfaceProgress } from './assistantTurnModel.js'

// What a POLLED turn progress (`GET /api/assistant/progress`) means for the live assistant message.
//
// A pure model beside `AssistantBar.jsx`, which applies it at three sites — the send path's
// buffered-proxy fallback, a reattached turn's first frame, and its poll — and they used to spell it
// three times. The defect they shared (2026-10-06, the operator's chat about node #37): the server's
// `text` mixed every tool round's prose with the answer tokens, and all three wrote it into the
// ANSWER bubble, so a turn whose stream dropped showed the model's narration as its reply. The server
// now keeps `text` for the answer and sends the prose in an ordered `activity`; this module is the
// one place the client reads them.

const ACTIVITY_MAX = 40

function cleanActivity(raw) {
  if (!Array.isArray(raw)) return null
  const out = []
  for (const seg of raw.slice(-ACTIVITY_MAX)) {
    if (!seg || typeof seg !== 'object') continue
    if (seg.type === 'text' && typeof seg.content === 'string' && seg.content.trim()) {
      out.push({ type: 'text', content: seg.content })
    } else if (seg.type === 'tools' && Array.isArray(seg.labels)) {
      const labels = seg.labels.filter(label => typeof label === 'string' && label)
      if (labels.length) out.push({ type: 'tools', labels })
    }
  }
  return out
}

/** The activity a progress frame shows: its ordered `activity` when the server sent one, else the
 *  legacy flat `steps` as one tools group, else what the message already had. */
export function progressActivity(progress, previous) {
  const ordered = cleanActivity(progress && progress.activity)
  if (ordered && ordered.length) return ordered
  const steps = progress && Array.isArray(progress.steps) ? progress.steps.filter(Boolean) : []
  if (steps.length) return [{ type: 'tools', labels: steps }]
  return previous
}

/** The patch a progress frame makes to the live assistant message: the ANSWER text only (never the
 *  prose, which is activity), the activity, and when the turn last moved. */
export function progressPatch(progress, previous = {}) {
  const patch = { activity: progressActivity(progress, previous.activity) }
  if (progress && typeof progress.text === 'string' && progress.text) patch.content = progress.text
  const moved = progress && Number(progress.last_event)
  if (Number.isFinite(moved) && moved > 0) patch.lastEventAt = moved * 1000
  return patch
}

/** Does this frame carry anything worth patching? */
export function progressHasNews(progress) {
  if (!progress) return false
  return !!(progress.text || (Array.isArray(progress.steps) && progress.steps.length)
    || (Array.isArray(progress.activity) && progress.activity.length))
}

/** May the fallback poll apply this frame to the live message? (critic 2026-10-08)
 *
 *  The historical rule (`assistantTurnModel.js::shouldSurfaceProgress`) surfaces a frame only when
 *  its `text` is longer than what the stream produced. Since `text` became the ANSWER alone, a turn
 *  behind a buffering proxy has `text === ''` for its whole tool phase, so nothing surfaced: no
 *  steps, no "last activity" line — the opaque-liveness incident on the very path this fallback is
 *  for. So a frame also applies when it carries news and either no SSE event has arrived yet
 *  (`streamLive` false: the stream is buffered) or the poll already owns the activity (`ownsActivity`:
 *  once a frame wrote the server's whole list, the batched SSE events must not append it again). */
export function progressFrameApplies(streamed, progress, { streamLive = false, ownsActivity = false } = {}) {
  if (!progress || progress.active !== true) return false
  if (shouldSurfaceProgress(streamed, progress)) return true
  // ACTIVITY news only — a frame whose answer is not longer than the stream's has nothing for the
  // answer bubble, and the caller keeps the streamed text (`AssistantBar.jsx::runLLM`'s onProgress).
  const activity = (Array.isArray(progress.activity) && progress.activity.length > 0)
    || (Array.isArray(progress.steps) && progress.steps.length > 0)
  return (!streamLive || ownsActivity) && activity
}

/** "HH:MM:SS" of a ms timestamp in the viewer's clock, or '' for none. The live line prints the
 *  time the turn last MOVED rather than a ticking counter: a turn that has stalled shows a time that
 *  stops changing, with no timer re-rendering every message on screen. */
export function lastEventClock(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return ''
  const d = new Date(ms)
  const two = n => String(n).padStart(2, '0')
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`
}
