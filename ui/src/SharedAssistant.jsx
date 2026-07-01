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
  return <div className="asst-view">
    <div className="asst-main">
      <div className="asst-main-h">
        <button className="btn sm" onClick={onBack} title="back">←</button>
        <span className="ttl" style={{ flex: 1 }}>{sess ? (sess.meta.title || 'Shared chat') : 'Shared chat'}</span>
        <span className="pill">read-only</span>
      </div>
      <div className="asst-feed">
        {err && <div className="notice" style={{ borderColor: 'var(--fail)', color: '#ffd3d3' }}>{err}</div>}
        {sess && (sess.messages || []).map((m, i) => <Turn key={i} m={m} />)}
        {sess && (sess.messages || []).length === 0 && <div className="muted">Empty chat.</div>}
      </div>
    </div>
  </div>
}
