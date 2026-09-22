// The authenticated event-stream transport: an incremental WHATWG event-stream parser plus the
// fetch-based SSE reader that carries the owner/review credential native EventSource cannot. Split out
// of api.js (doc 25 UI-02 — bodies verbatim); api.js re-exports everything, so importers are
// unchanged. The parser stays pure so reconnect/id semantics remain testable without React or a
// browser; the Assistant's message stream reuses it through the barrel.

import { _authHeaders, _throw, apiUrl, reviewReadPath } from './apiClient.js'

// The DEFAULT bound on one frame, for every stream that does not ask for its own. It is a per-call
// option (review 2026-09-22, UI-01): the run stream's first frame is the whole folded state, and
// this one constant bounding every stream is what stopped the owner stream from ever connecting once
// a run's state passed 2 MiB — it asks for a state-sized bound (`runStateModel.js`).
const EVENT_STREAM_MAX_FRAME_CHARS = 2 * 1024 * 1024
// An oversized frame is a property of the STREAM, not a transport blip, so its error is TYPED: a
// caller that reconnected on it would receive the same frame again, forever.
export const EVENT_STREAM_FRAME_TOO_LARGE = 'EVENT_STREAM_FRAME_TOO_LARGE'
const frameTooLarge = maxFrameChars => Object.assign(
  new Error(`Event-stream frame is larger than ${maxFrameChars} characters`),
  { code: EVENT_STREAM_FRAME_TOO_LARGE, maxFrameChars })

// Incremental WHATWG event-stream parser. Fetch chunks can split CRLF, UTF-8 code points and any
// field at arbitrary boundaries, so parsing per network chunk (or only `\n\n`) is not sufficient.
// Keeping this pure also makes reconnect/id semantics testable without React or a browser.
export function createEventStreamParser(onEvent, initialLastEventId = '', {
  maxFrameChars = EVENT_STREAM_MAX_FRAME_CHARS,
} = {}) {
  let buffer = ''
  let eventType = ''
  let dataLines = []
  let dataChars = 0
  let lastEventId = String(initialLastEventId || '')
  let retry = null

  const dispatch = () => {
    if (dataLines.length) {
      onEvent?.({
        type: eventType || 'message',
        data: dataLines.join('\n'),
        lastEventId,
        retry,
      })
    }
    eventType = ''
    dataLines = []
    dataChars = 0
  }
  const line = rawLine => {
    const valueLine = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!valueLine) { dispatch(); return }
    if (valueLine.startsWith(':')) return
    const separator = valueLine.indexOf(':')
    const field = separator < 0 ? valueLine : valueLine.slice(0, separator)
    let value = separator < 0 ? '' : valueLine.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') eventType = value
    else if (field === 'data') {
      dataChars += value.length
      if (dataChars > maxFrameChars) throw frameTooLarge(maxFrameChars)
      dataLines.push(value)
    } else if (field === 'id' && !value.includes('\0')) {
      lastEventId = value
    } else if (field === 'retry' && /^\d+$/.test(value)) {
      retry = Math.min(Number(value), 60_000)
    }
  }

  return {
    // Only the NEW text is searched for line breaks: the partial line is carried as it is, so a frame
    // of N characters costs O(N) however it is chunked. Searching the whole buffer on every chunk
    // was harmless under 2 MiB and quadratic under a state-sized bound. Complete lines are consumed
    // before the remainder is measured, so a chunk carrying many small events is not refused as one
    // oversized frame (the old order appended the whole chunk and measured it first).
    push(text) {
      const chunk = String(text || '')
      let start = 0
      let newline
      while ((newline = chunk.indexOf('\n', start)) >= 0) {
        const next = buffer + chunk.slice(start, newline)
        buffer = ''
        start = newline + 1
        if (next.length > maxFrameChars) throw frameTooLarge(maxFrameChars)
        line(next)
      }
      buffer += chunk.slice(start)
      if (buffer.length > maxFrameChars) throw frameTooLarge(maxFrameChars)
    },
    finish() {
      // EOF without a blank line is an incomplete event and is intentionally discarded, matching
      // EventSource. A reconnect can replay it from the last complete event id.
      buffer = ''
      eventType = ''
      dataLines = []
      dataChars = 0
      return { lastEventId, retry }
    },
    state: () => ({ lastEventId, retry }),
  }
}

// Authenticated GET-SSE transport for owner live state. Native EventSource cannot attach the owner
// or review credential, whereas this path uses the exact auth, review-translation and proxy-prefix
// plumbing as every ordinary API read. The caller owns reconnect timing and abort lifecycle, and
// the frame bound (`maxFrameChars`, the parser's default when omitted).
export async function fetchEventStream(path, {
  signal, lastEventId = '', onEvent, maxFrameChars,
} = {}) {
  const requestPath = reviewReadPath(path)
  const headers = { Accept: 'text/event-stream', 'Cache-Control': 'no-cache' }
  if (lastEventId !== '') headers['Last-Event-ID'] = String(lastEventId).slice(0, 256)
  const response = await fetch(apiUrl(requestPath), {
    method: 'GET',
    headers: _authHeaders(headers),
    signal,
    cache: 'no-store',
  })
  if (!response.ok) await _throw(response, path)
  if (!response.body || typeof response.body.getReader !== 'function') {
    throw new Error('The server returned no readable event stream.')
  }
  const parser = createEventStreamParser(onEvent, lastEventId, { maxFrameChars })
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let completed = false
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      parser.push(decoder.decode(value, { stream: true }))
    }
    parser.push(decoder.decode())
    completed = true
    return parser.finish()
  } finally {
    // A refused frame (or any throw out of the read loop) leaves the RESPONSE open: the body went on
    // streaming into a reader nobody read until the connection died on its own. Hand it back; a
    // cancel that fails (an already-errored stream) changes nothing about the outcome being thrown.
    if (!completed) {
      try { Promise.resolve(reader.cancel()).catch(() => {}) } catch { /* the throw is the outcome */ }
    }
  }
}
