import React, { useCallback, useEffect, useRef, useState } from 'react'
import { deadlineSharedAssistant, fmtDate } from './util.js'
import { Turn } from './AssistantChat.jsx'

const SHARED_REQUEST_TIMEOUT_MS = 15_000
const SHARED_MESSAGE_MAX = 400
const SHARED_TEXT_MAX = 256_000
const SHARED_TOTAL_TEXT_MAX = 2_000_000
const SHARED_ITEMS_MAX = 128
const SHARED_LABEL_MAX = 1_000
// Whole-transcript DOM amplification fence. Newlines can create blocks/list rows/<br>s; Markdown
// delimiters and @run mentions can each create another node. Plain long prose remains cheap, while
// highly structural input fails closed before any Turn is mounted.
const SHARED_STRUCTURE_MAX = 20_000

const record = value => !!value && typeof value === 'object' && !Array.isArray(value)
const boundedText = (value, max = SHARED_TEXT_MAX) => typeof value === 'string' && value.length <= max
const boundedArray = value => Array.isArray(value) && value.length <= SHARED_ITEMS_MAX
const labelItems = value => value == null || (boundedArray(value) && value.every(item => record(item)
  && (item.label == null || boundedText(item.label, SHARED_LABEL_MAX))
  && (item.tool == null || boundedText(item.tool, SHARED_LABEL_MAX))))
const sharedMessageWeight = message => {
  if (!record(message) || !['user', 'assistant'].includes(message.role)
      || !boundedText(message.content) || !labelItems(message.steps) || !labelItems(message.applied)
      || (message.todos != null && (!boundedArray(message.todos) || !message.todos.every(item => record(item)
        && boundedText(item.content, SHARED_LABEL_MAX) && boundedText(item.status, 64))))
      || (message.activity != null && (!boundedArray(message.activity)
        || !message.activity.every(item => record(item)
          && ((item.type === 'text' && boundedText(item.content))
            || (item.type === 'tools' && boundedArray(item.labels)
              && item.labels.every(label => boundedText(label, SHARED_LABEL_MAX)))))))) return null
  let weight = message.content.length
  for (const item of [...(message.steps || []), ...(message.applied || [])]) {
    weight += (item.label || '').length + (item.tool || '').length
  }
  for (const item of message.todos || []) weight += item.content.length + item.status.length
  for (const item of message.activity || []) {
    weight += item.type === 'text' ? item.content.length
      : item.labels.reduce((total, label) => total + label.length, 0)
  }
  return weight
}
const sharedMessageStructure = (message, available) => {
  let remaining = available - 6 // Turn/bubble/speaker wrappers
  const spend = amount => {
    remaining -= amount
    return remaining >= 0
  }
  const spendText = value => {
    if (!spend(1)) return false
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index)
      // A newline can create both a fragment and <br>; every other marker can create one formatted
      // node/chip. Counting markers instead of rendering them is conservative and deterministic.
      const cost = (code === 10 || code === 13) ? 2
        : (code === 42 || code === 64 || code === 91 || code === 95 || code === 96) ? 1 : 0
      if (cost && !spend(cost)) return false
    }
    return true
  }
  if (!spendText(message.content)) return null
  if (!spend((message.steps?.length || 0) + (message.applied?.length || 0)
      + (message.todos?.length || 0) * 2 + (message.activity?.length || 0) * 2)) return null
  for (const item of message.activity || []) {
    if (item.type === 'text' && !spendText(item.content)) return null
    if (item.type === 'tools' && !spend(item.labels.length)) return null
  }
  return remaining
}
const validSharedSession = value => {
  if (!record(value) || !record(value.meta) || !boundedText(value.meta.title, 500)
      || typeof value.meta.live !== 'boolean' || !Number.isFinite(value.meta.expires_at)
      || (value.meta.truncated != null && typeof value.meta.truncated !== 'boolean')
      || value.meta.expires_at <= 0 || !Array.isArray(value.messages)
      || value.messages.length > SHARED_MESSAGE_MAX) return false
  let totalText = value.meta.title.length
  let remainingStructure = SHARED_STRUCTURE_MAX
  for (const message of value.messages) {
    const weight = sharedMessageWeight(message)
    if (weight == null || (totalText += weight) > SHARED_TOTAL_TEXT_MAX) return false
    remainingStructure = sharedMessageStructure(message, remainingStructure)
    if (remainingStructure == null) return false
  }
  return true
}

const sharedLoadFailure = error => {
  if ([404, 410].includes(error?.status)) return {
    terminal: true,
    message: 'This shared chat is unavailable. It may have been removed or made private.',
  }
  return {
    terminal: false,
    message: error?.name === 'TimeoutError'
      ? 'Shared chat loading timed out. Check your connection and retry.'
      : 'Shared chat could not be loaded. Retry when the service is reachable.',
  }
}

// Read-only view of a shared assistant session (opened via a share link). No composer, no tools —
// just the transcript.
export default function SharedAssistant({ sid }) {
  const [resource, setResource] = useState({ status: 'loading', data: null, error: '' })
  const dataRef = useRef(null)
  const mountedRef = useRef(false)
  const requestRef = useRef(null)
  const errorRef = useRef(null)

  const load = useCallback(async ({ preserve = false } = {}) => {
    if (requestRef.current) return
    const retained = preserve ? dataRef.current : null
    const timed = deadlineSharedAssistant(sid, SHARED_REQUEST_TIMEOUT_MS)
    requestRef.current = timed
    setResource({ status: retained ? 'refreshing' : 'loading', data: retained, error: '' })
    try {
      const data = await timed.promise
      if (!mountedRef.current || requestRef.current !== timed) return
      if (!validSharedSession(data)) throw new Error('invalid shared chat response')
      dataRef.current = data
      setResource({ status: 'ready', data, error: '' })
    } catch (error) {
      if (!mountedRef.current || requestRef.current !== timed) return
      const failure = sharedLoadFailure(error)
      if (failure.terminal) {
        // Revocation/removal is authoritative. Do not keep rendering a transcript retained from an
        // earlier refresh and do not offer a Retry that can only repeat the same invalid capability.
        dataRef.current = null
        setResource({ status: 'gone', data: null, error: failure.message })
      } else {
        setResource({ status: retained ? 'stale' : 'error', data: retained, error: failure.message })
      }
    } finally {
      if (requestRef.current === timed) {
        requestRef.current = null
      }
    }
  }, [sid])

  useEffect(() => {
    mountedRef.current = true
    dataRef.current = null
    load()
    return () => {
      mountedRef.current = false
      requestRef.current?.controller.abort()
      requestRef.current = null
    }
  }, [load])
  useEffect(() => {
    if (resource.status === 'error' || resource.status === 'stale' || resource.status === 'gone') {
      errorRef.current?.focus({ preventScroll: true })
    }
  }, [resource.status])

  const sess = resource.data
  const refreshing = resource.status === 'refreshing'
  const retry = () => load({ preserve: !!dataRef.current })
  const messageCount = sess?.messages.length || 0
  const liveShare = sess?.meta.live === true
  const truncated = sess?.meta.truncated === true
  const expiry = sess?.meta.expires_at
  return <main className="asst-view asst-shared" data-route-main tabIndex={-1}
    aria-busy={resource.status === 'loading' || refreshing ? 'true' : undefined}>
    <div className="asst-main">
      <div className="asst-main-h">
        <h1 className="ttl" style={{ flex: 1 }}>{sess?.meta.title || 'Shared chat'}</h1>
        <div className="asst-shared-terms" role="note">
          <span className="pill">{sess ? (liveShare ? 'live · read-only' : 'frozen snapshot · read-only') : 'public · read-only'}</span>
          {expiry && <span className="muted">Expires <time dateTime={new Date(expiry * 1000).toISOString()}>
            {fmtDate(expiry)}</time></span>}
        </div>
        {sess && <button className="btn sm" type="button" disabled={refreshing}
          title={liveShare ? 'Load newly shared messages' : 'Recheck whether this frozen link is still available'}
          onClick={() => load({ preserve: true })}>{refreshing ? 'Refreshing…'
            : liveShare ? 'Refresh messages' : 'Recheck link'}</button>}
      </div>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {resource.status === 'ready' ? `Shared chat loaded. ${messageCount} messages.`
          : refreshing ? 'Refreshing shared chat.' : ''}
      </div>
      <div className="asst-feed" role="log" aria-live="off" aria-label="Shared Assistant transcript"
        aria-busy={resource.status === 'loading' || refreshing ? 'true' : undefined} tabIndex={0}>
        {truncated && <div className="notice" role="status">
          This public transcript reached its safety limit. Some messages or details are not included.
        </div>}
        {resource.status === 'loading' && <div className="notice" role="status">Loading shared chat…</div>}
        {refreshing && <div className="notice" role="status">Refreshing shared chat…</div>}
        {(resource.status === 'error' || resource.status === 'stale' || resource.status === 'gone') && <div ref={errorRef}
          className="notice" role="alert" tabIndex={-1}
          style={{ borderColor: 'var(--fail)', color: 'var(--fg)' }}>
          <p>{resource.status === 'stale'
            ? `${resource.error} Showing the last loaded transcript.` : resource.error}</p>
          {resource.status !== 'gone'
            && <button className="btn sm" type="button" onClick={retry}>Retry</button>}
        </div>}
        {sess && sess.messages.map((message, index) => <Turn key={index} m={message}
          readOnly audience="public" />)}
        {sess && messageCount === 0 && <div className="muted">This shared snapshot has no complete turns.</div>}
      </div>
    </div>
  </main>
}
