import React, { useCallback, useEffect, useRef, useState } from 'react'
import { get } from './util.js'
import { Turn } from './AssistantChat.jsx'

const SHARED_REQUEST_TIMEOUT_MS = 15_000

// REVIEW(2026-07-16): e8d5249 pasted this `withDeadline` VERBATIM into OwnerAuth.jsx and here (only
// the message strings differ), and RunList.jsx carries `settleWithin`, a third variant of the same
// deadline/abort idea from 998f864 — three hand-rolled timeout helpers in one day. Any fix (e.g. to
// the abort-listener cleanup or timeout semantics) now needs three edits that will drift; extract one
// shared helper (ui/src/requestDeadline.js, parameterized by timeout + error copy) before a fourth
// copy appears. Separately: `validSharedMessage` rejects the ENTIRE shared transcript when one
// activity item has an unrecognized `type` — additive server evolution (a new activity kind) bricks
// every existing share link with "invalid shared chat response". The repo's own forward-compat rule
// (invariant 5: unknown event types are ignored) argues for skipping unknown items, not failing the
// whole session; reserve the hard reject for structurally unsafe shapes (non-string content etc.).
const withDeadline = (request, controller) => {
  let timer
  let timedOut = false
  let onAbort
  const deadline = new Promise((_, reject) => {
    onAbort = () => {
      if (timedOut) return
      const error = new Error('shared chat request was aborted')
      error.name = 'AbortError'
      reject(error)
    }
    controller.signal.addEventListener('abort', onAbort, { once: true })
    timer = setTimeout(() => {
      timedOut = true
      controller.abort()
      const error = new Error('shared chat request timed out')
      error.name = 'TimeoutError'
      reject(error)
    }, SHARED_REQUEST_TIMEOUT_MS)
  })
  return Promise.race([request, deadline]).finally(() => {
    clearTimeout(timer)
    controller.signal.removeEventListener('abort', onAbort)
  })
}

const record = value => !!value && typeof value === 'object' && !Array.isArray(value)
const labelItems = value => value == null || (Array.isArray(value) && value.every(item => record(item)
  && (item.label == null || typeof item.label === 'string')
  && (item.tool == null || typeof item.tool === 'string')))
const validSharedMessage = message => record(message)
  && (message.role === 'user' || message.role === 'assistant')
  && typeof message.content === 'string'
  && labelItems(message.steps) && labelItems(message.applied)
  && (message.todos == null || (Array.isArray(message.todos) && message.todos.every(item => record(item)
    && typeof item.content === 'string' && typeof item.status === 'string')))
  && (message.activity == null || (Array.isArray(message.activity) && message.activity.every(item => record(item)
    && ((item.type === 'text' && typeof item.content === 'string')
      || (item.type === 'tools' && Array.isArray(item.labels)
        && item.labels.every(label => typeof label === 'string'))))))
const validSharedSession = value => record(value) && record(value.meta)
  && typeof value.meta.title === 'string' && Array.isArray(value.messages)
  && value.messages.every(validSharedMessage)

const sharedLoadError = error => error?.status === 404
  ? 'This shared chat is unavailable. It may have been removed or made private.'
  : error?.name === 'TimeoutError'
    ? 'Shared chat loading timed out. Check your connection and retry.'
    : 'Shared chat could not be loaded. Retry when the service is reachable.'

// Read-only view of a shared assistant session (opened via a share link). No composer, no tools —
// just the transcript.
export default function SharedAssistant({ sid, onBack }) {
  const [resource, setResource] = useState({ status: 'loading', data: null, error: '' })
  const dataRef = useRef(null)
  const busyRef = useRef(false)
  const mountedRef = useRef(false)
  const sequenceRef = useRef(0)
  const requestRef = useRef(null)
  const errorRef = useRef(null)

  const load = useCallback(async ({ preserve = false } = {}) => {
    if (busyRef.current) return
    busyRef.current = true
    const retained = preserve ? dataRef.current : null
    const id = ++sequenceRef.current
    const controller = new AbortController()
    requestRef.current = { id, controller }
    setResource({ status: retained ? 'refreshing' : 'loading', data: retained, error: '' })
    try {
      const data = await withDeadline(
        get(`/api/assistant/shared/${encodeURIComponent(sid)}`, { signal: controller.signal }), controller,
      )
      if (!mountedRef.current || requestRef.current?.id !== id) return
      if (!validSharedSession(data)) throw new Error('invalid shared chat response')
      dataRef.current = data
      setResource({ status: 'ready', data, error: '' })
    } catch (error) {
      if (!mountedRef.current || requestRef.current?.id !== id) return
      const errorMessage = sharedLoadError(error)
      setResource({ status: retained ? 'stale' : 'error', data: retained, error: errorMessage })
    } finally {
      if (requestRef.current?.id === id) {
        requestRef.current = null
        busyRef.current = false
      }
    }
  }, [sid])

  useEffect(() => {
    mountedRef.current = true
    dataRef.current = null
    load()
    return () => {
      mountedRef.current = false
      busyRef.current = false
      sequenceRef.current += 1
      requestRef.current?.controller.abort()
      requestRef.current = null
    }
  }, [load])
  useEffect(() => {
    if (resource.status === 'error' || resource.status === 'stale') {
      errorRef.current?.focus({ preventScroll: true })
    }
  }, [resource.status])

  const sess = resource.data
  const refreshing = resource.status === 'refreshing'
  const retry = () => load({ preserve: !!dataRef.current })
  const messageCount = sess?.messages.length || 0
  return <main className="asst-view" data-route-main tabIndex={-1}
    aria-busy={resource.status === 'loading' || refreshing ? 'true' : undefined}>
    <div className="asst-main">
      <div className="asst-main-h">
        <button className="btn sm" type="button" onClick={onBack} aria-label="Back to runs">←</button>
        <h1 className="ttl" style={{ flex: 1 }}>{sess?.meta.title || 'Shared chat'}</h1>
        <span className="pill">read-only</span>
        {sess && <button className="btn sm" type="button" disabled={refreshing}
          onClick={() => load({ preserve: true })}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>}
      </div>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {resource.status === 'ready' ? `Shared chat loaded. ${messageCount} messages.`
          : refreshing ? 'Refreshing shared chat.' : ''}
      </div>
      <div className="asst-feed" role="log" aria-live="off" aria-label="Shared Assistant transcript"
        aria-busy={resource.status === 'loading' || refreshing ? 'true' : undefined} tabIndex={0}>
        {resource.status === 'loading' && <div className="notice" role="status">Loading shared chat…</div>}
        {refreshing && <div className="notice" role="status">Refreshing shared chat…</div>}
        {(resource.status === 'error' || resource.status === 'stale') && <div ref={errorRef}
          className="notice" role="alert" tabIndex={-1}
          style={{ borderColor: 'var(--fail)', color: 'var(--fg)' }}>
          <p>{resource.status === 'stale'
            ? `${resource.error} Showing the last loaded transcript.` : resource.error}</p>
          <button className="btn sm" type="button" onClick={retry}>Retry</button>
        </div>}
        {sess && sess.messages.map((message, index) => <Turn key={index} m={message} readOnly />)}
        {sess && messageCount === 0 && <div className="muted">This shared chat has no messages.</div>}
      </div>
    </div>
  </main>
}
