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

// ── a LIVE message only grows (review 2026-10-08) ─────────────────────────────────────────────────
// The server's copy is BOUNDED: `activity` keeps its newest 40 segments (each prose segment cut at
// 4000 chars) and `text` the LAST 8000 chars of the answer. `progressPatch` writes that copy over the
// message, which is right for a message that has nothing else — and wrong once the client holds more:
// a frame the send path surfaced because its answer was longer than the stream's replaced the
// activity the STREAM had delivered whole with the server's cut window, and past 8000 chars replaced
// the answer's head with the server's tail. A live message therefore only EXTENDS: an answer is taken
// when it continues what is shown (or overlaps its end), an activity window is spliced onto what is
// shown, and anything that would shrink either is left out of the patch.

const sameSegment = (a, b) => !!a && !!b && a.type === b.type && (a.type === 'text'
  ? a.content === b.content
  : JSON.stringify(a.labels) === JSON.stringify(b.labels))

/** `shown` followed by what `polled` adds past it, or null when `polled` adds nothing (or cannot be
 *  placed after `shown`). `polled` is the server's tail of the answer: it either starts with all of
 *  `shown`, or — once the answer is past the server's cap — starts INSIDE it, and the overlap places
 *  it. A tail that starts after `shown` ends cannot be placed: the gap is unknown, nothing is taken. */
export function extendAnswer(shown, polled) {
  const base = typeof shown === 'string' ? shown : ''
  if (typeof polled !== 'string' || !polled) return null
  if (polled.startsWith(base)) return polled.length > base.length ? polled : null
  const probe = polled.slice(0, Math.min(64, polled.length))
  for (let at = base.indexOf(probe); at >= 0; at = base.indexOf(probe, at + 1)) {
    if (polled.startsWith(base.slice(at))) {
      const joined = base.slice(0, at) + polled
      return joined.length > base.length ? joined : null
    }
  }
  return null
}

/** `shown` (the activity the message holds) extended by the server's bounded window `polled`: the
 *  segments of `shown` the window has already dropped, then the window. The window is placed by
 *  overlap — its first segments equal `shown`'s last ones, the last of them allowed to be a tool group
 *  that has grown since — and a window that overlaps nothing replaces an EMPTY `shown` only: a cut
 *  window never stands in for a longer record the client already holds. */
export function extendActivity(shown, polled) {
  const local = Array.isArray(shown) ? shown : []
  const next = Array.isArray(polled) ? polled : []
  if (!next.length) return local
  if (!local.length) return next
  for (let k = Math.min(local.length, next.length); k >= 1; k -= 1) {
    const from = local.length - k
    let fits = true
    for (let i = 0; i < k && fits; i += 1) {
      const a = local[from + i]
      const b = next[i]
      fits = i < k - 1 ? sameSegment(a, b) : (sameSegment(a, b) || (a.type === 'tools' && b.type === 'tools'))
    }
    if (fits) return [...local.slice(0, from), ...next]
  }
  return local
}

/** The patch a progress frame makes to a message that is STILL STREAMING — the send path's fallback
 *  and a reattached turn's poll. `streamed` is what the stream itself delivered of the answer and
 *  `streamEvents` how many step/prose events it delivered: once it has delivered any, the activity is
 *  the stream's and a polled window never touches it. `content` is in the patch only when the frame
 *  extends the answer shown (the longer of the streamed tokens and what the message holds). */
export function streamingProgressPatch(progress, previous = {}, { streamed = '', streamEvents = 0 } = {}) {
  const prev = previous || {}
  const polled = progressActivity(progress, null)
  const patch = { activity: streamEvents > 0 || !polled ? prev.activity : extendActivity(prev.activity, polled) }
  const held = typeof prev.content === 'string' ? prev.content : ''
  const acc = String(streamed || '')
  const shown = held.length >= acc.length ? held : acc
  const longer = progress && extendAnswer(shown, progress.text)
  if (longer) patch.content = longer
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

/** Has this frame MOVED since `previous` — the frame the caller last surfaced, or null? A newer
 *  `last_event`, or activity/steps/answer that differ from it. A frame with no news never has.
 *  The send path's buffered-proxy fallback (`assistantTurnModel.js::shouldSurfaceProgress`) asks it
 *  so a turn deep in tool rounds — `text` empty, the prose in `activity` — still moves its bubble,
 *  and so an unchanged frame polled again does not re-render the live Turn once a second. */
export function progressMovedSince(progress, previous) {
  if (!progressHasNews(progress)) return false
  if (!previous) return true
  const at = frame => {
    const moved = Number(frame && frame.last_event)
    return Number.isFinite(moved) && moved > 0 ? moved : 0
  }
  if (at(progress) > at(previous)) return true
  const shape = frame => JSON.stringify([frame.text || '', frame.steps || null, frame.activity || null])
  return shape(progress) !== shape(previous)
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
