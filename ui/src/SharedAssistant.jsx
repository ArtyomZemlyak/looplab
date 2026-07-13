import React, { useEffect, useState } from 'react'
import { get } from './util.js'
import { Turn } from './AssistantChat.jsx'

// Read-only view of a shared assistant session (opened via a share link). No composer, no tools —
// just the transcript.
export default function SharedAssistant({ sid, onBack }) {
  const [sess, setSess] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    get(`/api/assistant/shared/${encodeURIComponent(sid)}`).then(setSess).catch(e => setErr(e.message))
  }, [sid])
  return <main className="asst-view" data-route-main tabIndex={-1}>
    <div className="asst-main">
      <div className="asst-main-h">
        <button className="btn sm" onClick={onBack} aria-label="Back to runs">←</button>
        <h1 className="ttl" style={{ flex: 1 }}>{sess?.meta?.title || 'Shared chat'}</h1>
        <span className="pill">read-only</span>
      </div>
      <div className="asst-feed" role="log" aria-label="Shared Assistant transcript" tabIndex={0}>
        {err && <div className="notice" role="alert" style={{ borderColor: 'var(--fail)', color: 'var(--fg)' }}>{err}</div>}
        {sess && (sess.messages || []).map((m, i) => <Turn key={i} m={m} readOnly />)}
        {sess && (sess.messages || []).length === 0 && <div className="muted">Empty chat.</div>}
      </div>
    </div>
  </main>
}
