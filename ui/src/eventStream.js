// The authenticated event-stream transport: an incremental WHATWG event-stream parser plus the
// fetch-based SSE reader that carries the owner/review credential native EventSource cannot. Split out
// of api.js (doc 25 UI-02 — bodies verbatim); api.js re-exports everything, so importers are
// unchanged. The parser stays pure so reconnect/id semantics remain testable without React or a
// browser; the Assistant's message stream reuses it through the barrel.

import { _authHeaders, _throw, apiUrl, reviewReadPath } from './apiClient.js'

const EVENT_STREAM_MAX_FRAME_CHARS = 2 * 1024 * 1024

// Incremental WHATWG event-stream parser. Fetch chunks can split CRLF, UTF-8 code points and any
// field at arbitrary boundaries, so parsing per network chunk (or only `\n\n`) is not sufficient.
// Keeping this pure also makes reconnect/id semantics testable without React or a browser.
export function createEventStreamParser(onEvent, initialLastEventId = '') {
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
      if (dataChars > EVENT_STREAM_MAX_FRAME_CHARS) throw new Error('Event-stream frame is too large')
      dataLines.push(value)
    } else if (field === 'id' && !value.includes('\0')) {
      lastEventId = value
    } else if (field === 'retry' && /^\d+$/.test(value)) {
      retry = Math.min(Number(value), 60_000)
    }
  }

  return {
    push(text) {
      buffer += String(text || '')
      if (buffer.length > EVENT_STREAM_MAX_FRAME_CHARS) throw new Error('Event-stream buffer is too large')
      let newline
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const next = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        line(next)
      }
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
// plumbing as every ordinary API read. The caller owns reconnect timing and abort lifecycle.
export async function fetchEventStream(path, {
  signal, lastEventId = '', onEvent,
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
  const parser = createEventStreamParser(onEvent, lastEventId)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    parser.push(decoder.decode(value, { stream: true }))
  }
  parser.push(decoder.decode())
  return parser.finish()
}
