import React, { useEffect, useRef, useState } from 'react'
import Markdown from './markdown.jsx'
import { fmtAgo } from './util.js'
import {
  assistantSessions, assistantCreate, assistantGet, assistantDelete, assistantFork,
  assistantMessagePost, getJob, assistantPermissions, assistantResolve,
} from './util.js'

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// The general-purpose assistant — the evolution of Genesis into a persistent chat agent (like Claude
// Desktop). P0 is read-only (inspect files + runs); write/shell/git + the permission modes below
// become live in P1, so the mode selector is shown but only `plan` is enforced for now.
const MODES = [
  { id: 'plan', label: 'Plan', hint: 'read-only — inspect & propose (safe)' },
  { id: 'default', label: 'Ask', hint: 'confirm every change' },
  { id: 'acceptEdits', label: 'Auto-edit', hint: 'edits apply; commands ask' },
  { id: 'auto', label: 'Auto', hint: 'runs everything without asking' },
]

// One assistant/user turn in the feed. Tool steps (what the agent read) render as a compact sub-line.
function Turn({ m }) {
  const who = m.role === 'user' ? 'you' : 'assistant'
  return <div className={'feed-msg chat ' + m.role}>
    <div className="fm-body">
      <div className="chat-who">{who}</div>
      {m.role === 'assistant' && Array.isArray(m.steps) && m.steps.length > 0 &&
        <div className="asst-steps">{m.steps.map((s, i) =>
          <span key={i} className="asst-step">{s.label || s.tool}</span>)}</div>}
      {m.role === 'assistant' && Array.isArray(m.applied) && m.applied.length > 0 &&
        <div className="asst-steps">{m.applied.map((a, i) =>
          <span key={i} className="asst-step done">✓ {a.label || a.tool}</span>)}</div>}
      <div className="chat-bubble">
        {m.role === 'assistant'
          ? <Markdown text={m.content || ''} className="chat-text" />
          : <div className="chat-text">{m.content}</div>}
      </div>
    </div>
  </div>
}

// A human-in-the-loop confirm card: the turn paused to ask before a mutating action. Approve/Reject
// resolves it server-side and the turn resumes.
function PermCard({ req, onResolve }) {
  const a = req.action || {}
  const isDiff = a.tool === 'write_file' || a.tool === 'edit_file' || a.tool === 'apply_patch'
  return <div className="asst-perm">
    <div className="asst-perm-h"><span className="asst-perm-badge">approve?</span>
      <b>{a.label || a.tool}</b>{a.cwd && <span className="muted"> · {a.cwd}</span>}</div>
    {a.preview && <pre className={'asst-perm-pre' + (isDiff ? ' diff' : '')}>{a.preview}</pre>}
    <div className="asst-perm-actions">
      <button className="btn xs ghost" onClick={() => onResolve(req.id, 'deny')}>Reject</button>
      <button className="btn xs" onClick={() => onResolve(req.id, 'allow_always')} title="allow this kind for the session">Always</button>
      <button className="btn xs primary" onClick={() => onResolve(req.id, 'allow_once')}>Approve</button>
    </div>
  </div>
}

export default function AssistantChat({ onBack }) {
  const [sessions, setSessions] = useState([])
  const [sid, setSid] = useState(null)
  const [msgs, setMsgs] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState('plan')
  const [err, setErr] = useState(null)
  const [pending, setPending] = useState([])   // live human-in-the-loop confirm requests
  const feedRef = useRef(null)

  const refreshSessions = async () => {
    try { const r = await assistantSessions(); setSessions(r.sessions || []) } catch { /* offline */ }
  }
  useEffect(() => { refreshSessions() }, [])
  useEffect(() => {
    const f = feedRef.current
    if (f) requestAnimationFrame(() => { f.scrollTop = f.scrollHeight })
  }, [msgs, busy])

  const openSession = async (id) => {
    setSid(id); setErr(null)
    try {
      const s = await assistantGet(id)
      setMsgs(s.messages || [])
      if (s.meta && s.meta.mode) setMode(s.meta.mode)
    } catch (e) { setErr(e.message) }
  }
  const newChat = () => { setSid(null); setMsgs([]); setErr(null); setInput('') }
  const del = async (id, e) => {
    e?.stopPropagation()
    try {
      await assistantDelete(id)
      if (id === sid) newChat()
      refreshSessions()
    } catch (e2) { setErr(e2.message) }
  }
  const fork = async () => {
    if (!sid) return
    try { const c = await assistantFork(sid); await refreshSessions(); openSession(c.id) } catch (e) { setErr(e.message) }
  }

  const resolvePerm = async (reqId, decision) => {
    setPending(p => p.filter(x => x.id !== reqId))     // optimistic — the turn resumes server-side
    try { await assistantResolve(reqId, decision) } catch (e) { setErr(e.message) }
  }

  // Drive one turn: POST, then poll the job to completion while ALSO polling for permission requests
  // (a mutating action pauses the turn until the user approves a confirm-card).
  const runTurn = async (id, text) => {
    const resp = await assistantMessagePost(id, text, mode)
    if (!resp || resp.status !== 'running' || !resp.job_id) return resp   // fast inline result
    const deadline = Date.now() + 20 * 60 * 1000
    while (Date.now() < deadline) {
      await sleep(1000)
      try { const p = await assistantPermissions(id); setPending(p.pending || []) } catch { /* transient */ }
      let j
      try { j = await getJob(resp.job_id) } catch { continue }
      if (j.status === 'done') { setPending([]); return j }
      if (j.status === 'unknown') return { ok: false, error: 'the turn expired — try again' }
    }
    return { ok: false, error: 'timed out' }
  }

  const send = async (text) => {
    const t = (text ?? input).trim()
    if (!t || busy) return
    setErr(null); setInput('')
    let id = sid
    if (!id) {
      try { const m = await assistantCreate(t.slice(0, 60), mode); id = m.id; setSid(id); refreshSessions() }
      catch (e) { setErr(e.message); return }
    }
    setMsgs(m => [...m, { role: 'user', content: t }])
    setBusy(true)
    try {
      const r = await runTurn(id, t)
      if (r && r.ok === false && r.error) setErr(r.error)
      setMsgs(m => [...m, { role: 'assistant', content: (r && r.reply) || '(no reply)', steps: r && r.steps, applied: r && r.applied }])
      refreshSessions()
    } catch (e) {
      setErr(e.message)
      setMsgs(m => [...m, { role: 'assistant', content: 'Could not reach the assistant.' }])
    } finally { setBusy(false); setPending([]) }
  }

  const activeMode = MODES.find(x => x.id === mode) || MODES[0]

  return <div className="asst-view">
    <div className="asst-side">
      <div className="asst-side-h">
        <button className="btn sm" onClick={onBack} title="back to runs">←</button>
        <span className="ttl" style={{ flex: 1 }}>Assistant</span>
        <button className="btn sm primary" onClick={newChat}>+ New</button>
      </div>
      <div className="asst-sessions">
        {sessions.length === 0 && <div className="muted" style={{ padding: 12, fontSize: 12 }}>No chats yet.</div>}
        {sessions.map(s => <div key={s.id}
          className={'asst-sess' + (s.id === sid ? ' active' : '')}
          onClick={() => openSession(s.id)}>
          <div className="asst-sess-t">{s.title || 'Chat'}</div>
          <div className="asst-sess-m">{fmtAgo(s.updated)}</div>
          <button className="btn xs ghost asst-sess-x" onClick={(e) => del(s.id, e)} title="delete">✕</button>
        </div>)}
      </div>
    </div>

    <div className="asst-main">
      <div className="asst-main-h">
        <div className="asst-modes">
          {MODES.map(x => <button key={x.id}
            className={'asst-mode' + (x.id === mode ? ' on' : '')}
            title={x.hint} onClick={() => setMode(x.id)}>{x.label}</button>)}
        </div>
        <span className="muted" style={{ flex: 1, fontSize: 11 }}>{activeMode.hint}</span>
        {sid && <button className="btn sm ghost" onClick={fork} title="clone this chat">fork</button>}
      </div>

      <div className="asst-feed" ref={feedRef}>
        {msgs.length === 0 && <div className="gen-empty">
          <div className="muted" style={{ marginBottom: 8 }}>
            Ask me anything — I can inspect the code, read your runs, and (once you switch modes) edit
            files, run tests and fix LoopLab itself.
          </div>
          {['What runs are live right now?', 'Explain how the event log works in this repo',
            'Read the README and summarize LoopLab'].map(s =>
            <button key={s} className="gen-seed" onClick={() => send(s)}>{s}</button>)}
        </div>}
        {msgs.map((m, i) => <Turn key={i} m={m} />)}
        {pending.map(req => <PermCard key={req.id} req={req} onResolve={resolvePerm} />)}
        {busy && pending.length === 0 && <div className="feed-msg chat assistant"><div className="fm-body">
          <div className="chat-who">assistant</div>
          <div className="chat-bubble"><div className="chat-text muted">… thinking</div></div>
        </div></div>}
      </div>

      {err && <div className="notice" style={{ borderColor: 'var(--fail)', color: '#ffd3d3', margin: '0 12px' }}>{err}</div>}

      <div className="chat-in asst-in">
        <textarea className="text" placeholder="Message the assistant…  (Enter to send · Shift+Enter for a newline)"
          value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <div className="toolbar" style={{ marginTop: 6 }}>
          <span className="muted" style={{ flex: 1, fontSize: 11 }}>mode: <b>{activeMode.label}</b></span>
          <button className="btn sm primary" disabled={!input.trim() || busy} onClick={() => send()}>{busy ? '…' : 'Send'}</button>
        </div>
      </div>
    </div>
  </div>
}
