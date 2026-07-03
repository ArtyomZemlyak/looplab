import React, { useEffect, useRef, useState } from 'react'
import Markdown from './markdown.jsx'
import { fmtAgo, fmt, get, startRun, ASSISTANT_MODES, tokText } from './util.js'
import {
  assistantSessions, assistantCreate, assistantGet, assistantDelete, assistantFork,
  assistantMessageStream, assistantPermissions, assistantResolve,
  assistantCommands, assistantShare, assistantRevert,
} from './util.js'

const sleep = (ms) => new Promise(r => setTimeout(r, ms))
const RUN_MENTION = /@run:([^\s.,;:!?)]+)/g
const runMentions = (s) => [...new Set([...(s || '').matchAll(RUN_MENTION)].map(m => m[1]))]

// A live inline card for a run referenced with @run:<id> — so a running run shows up right in the chat
// (a direct ask). Links to the run view; the dot pulses while its engine is live.
function RunChip({ id, run }) {
  const phase = run ? (run.phase || (run.finished ? 'finished' : 'running')) : '—'
  return <a className="asst-runchip" href={`#/run/${encodeURIComponent(id)}`}
    title={run ? (run.goal || id) : id}>
    <span className={'asst-run-dot' + (run && run.engine_running ? ' live' : '')} />
    <b>{id}</b>
    {run && <span className="muted"> {phase}{run.best_metric != null ? ' · ' + fmt(run.best_metric) : ''}</span>}
  </a>
}

// The general-purpose assistant — the evolution of Genesis into a persistent chat agent (like Claude
// Desktop). P0 is read-only (inspect files + runs); write/shell/git + the permission modes below
// become live in P1, so the mode selector is shown but only `plan` is enforced for now.
const MODES = ASSISTANT_MODES

// One assistant/user turn in the feed. Tool steps (what the agent read) render as a compact sub-line;
// any @run:<id> mention gets a live inline run card.
// Strip the invisible "[UI context: …]" preamble the bar appends to a user turn for the model, so it
// never shows in the bubble — including messages persisted before the server started storing the clean
// display text. The injected context is still surfaced separately as the faint caption plaque.
const stripCtx = (s) => typeof s === 'string'
  ? s.replace(/\n*\[UI context:[^\]]*\]\s*$/, '').trimEnd() : s

export function Turn({ m, runsById, onRevert, readOnly = false }) {
  const who = m.role === 'user' ? 'you' : 'assistant'
  const content = m.role === 'user' ? stripCtx(m.content) : m.content
  const mentions = runMentions(content)
  return <div className={'feed-msg chat ' + m.role}>
    <div className="fm-body">
      <div className="chat-who">{who}</div>
      {/* Live, interleaved activity (Claude-Desktop style): prose the agent writes BETWEEN tool rounds
          renders as its own line; a run of consecutive tool calls collapses into one status line. */}
      {m.role === 'assistant' && Array.isArray(m.activity) && m.activity.length > 0 &&
        <div className="asst-activity">{m.activity.map((seg, i) => seg.type === 'text'
          ? <Markdown key={i} text={seg.content} className="asst-inter" />
          : <div key={i} className="asst-status"><span className="asst-status-ic">⚙</span>
              {seg.labels.length > 3
                ? <span> {seg.labels.length} steps · {seg.labels[seg.labels.length - 1]}</span>
                : <span> {seg.labels.join(' · ')}</span>}</div>)}</div>}
      {/* Legacy compact step line — only when there's no richer activity timeline to show. */}
      {m.role === 'assistant' && !(m.activity && m.activity.length) && Array.isArray(m.steps) && m.steps.length > 0 &&
        <div className="asst-steps">{m.steps.map((s, i) =>
          <span key={i} className="asst-step">{s.label || s.tool}</span>)}</div>}
      {m.role === 'assistant' && m.streaming && !m.content && !(m.activity && m.activity.length) &&
        <div className="asst-status thinking"><span className="asst-status-ic">…</span><span> thinking</span></div>}
      {m.role === 'assistant' && Array.isArray(m.applied) && m.applied.length > 0 &&
        <div className="asst-steps">{m.applied.map((a, i) =>
          <span key={i} className="asst-step done">✓ {a.label || a.tool}
            {onRevert && a.abs_path && <button className="asst-undo" title="undo this change"
              onClick={() => onRevert(a.abs_path)}>undo</button>}</span>)}</div>}
      {m.role === 'assistant' && <Todos items={m.todos} />}
      {(m.content || !m.streaming) && <div className="chat-bubble">
        {m.role === 'assistant'
          ? <><Markdown text={m.content || ''} className="chat-text" />{m.streaming && <span className="asst-cursor">▍</span>}</>
          : <div className="chat-text">{content}</div>}
      </div>}
      {mentions.length > 0 && <div className="asst-runchips">
        {mentions.map(id => <RunChip key={id} id={id} run={runsById && runsById[id]} />)}
      </div>}
      {!readOnly && Array.isArray(m.proposals) && m.proposals.map((sp, i) => <LaunchCard key={i} spec={sp} />)}
    </div>
  </div>
}

// The assistant's live TODO list for a multi-step task.
function Todos({ items }) {
  if (!items || !items.length) return null
  const mark = (s) => s === 'completed' ? '✓' : (s === 'in_progress' ? '▸' : '○')
  return <div className="asst-todos">{items.map((t, i) =>
    <div key={i} className={'asst-todo ' + (t.status || 'pending')}>
      <span className="asst-todo-box">{mark(t.status)}</span><span>{t.content}</span>
    </div>)}</div>
}

// A human-in-the-loop confirm card: the turn paused to ask before a mutating action. Approve/Reject
// resolves it server-side and the turn resumes.
export function PermCard({ req, onResolve }) {
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

// A launch card for a run the assistant proposed (propose_run). The user reviews and starts it via
// the existing /api/start; the New-run flow becomes one assistant capability.
function LaunchCard({ spec }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [done, setDone] = useState(false)
  const task = spec.task || {}
  const what = spec.task_file ? spec.task_file.split(/[\\/]/).pop() : (task.kind || 'task')
  const launch = async () => {
    setBusy(true); setErr(null)
    try {
      const body = { run_id: spec.run_id, settings: spec.settings || {} }
      if (spec.task_file) body.task_file = spec.task_file; else body.task = task
      await startRun(body)
      setDone(true); location.hash = `#/run/${encodeURIComponent(spec.run_id)}`
    } catch (e) { setErr(e.status === 409 ? `"${spec.run_id}" already exists — rename it` : e.message); setBusy(false) }
  }
  return <div className="asst-launch">
    <div className="asst-launch-h"><span className="asst-perm-badge">new run</span><b>{spec.run_id}</b>
      <span className="muted"> · {what}</span></div>
    {spec.rationale && <div className="muted" style={{ fontSize: 12, margin: '4px 0' }}>{spec.rationale}</div>}
    {spec.settings && Object.keys(spec.settings).length > 0 &&
      <div className="asst-steps">{Object.entries(spec.settings).map(([k, v]) =>
        <span key={k} className="asst-step">{k}={String(v)}</span>)}</div>}
    {err && <div className="notice" style={{ borderColor: 'var(--fail)', color: '#ffd3d3', marginTop: 6 }}>{err}</div>}
    <div className="asst-perm-actions">
      <button className="btn xs primary" disabled={busy || done} onClick={launch}>{done ? 'started ✓' : (busy ? '…' : '▶ Start run')}</button>
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
  const [liveSteps, setLiveSteps] = useState([])   // tool steps streamed while a turn runs
  const [liveTodos, setLiveTodos] = useState([])   // the assistant's TODO list, streamed live
  const [runs, setRuns] = useState([])          // /api/runs, for @run cards + the @-picker
  const [commands, setCommands] = useState([])  // slash commands
  const feedRef = useRef(null)
  const inputRef = useRef(null)
  const mountedRef = useRef(true)
  const abortRef = useRef(null)
  const sidRef = useRef(null)   // the session the stream callbacks belong to; guards against tokens
  //   from session A's in-flight turn bleeding into session B after the user switches sessions
  useEffect(() => () => { mountedRef.current = false; if (abortRef.current) abortRef.current.abort() }, [])

  useEffect(() => { assistantCommands().then(r => setCommands(r.commands || [])).catch(() => {}) }, [])
  // slash-command picker: when the message is a bare "/name" (no space yet), suggest commands.
  const slashMatch = /^\/(\w*)$/.exec(input)
  const slashCmds = slashMatch ? commands.filter(c => c.name.startsWith(slashMatch[1].toLowerCase())) : []
  const pickCmd = (name) => { setInput(`/${name} `); if (inputRef.current) inputRef.current.focus() }

  const runsById = React.useMemo(() => Object.fromEntries(runs.map(r => [r.run_id, r])), [runs])
  useEffect(() => {
    let alive = true
    const load = async () => { try { const r = await get('/api/runs'); if (alive) setRuns(r || []) } catch { /* offline */ } }
    load(); const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  // @-mention picker: when the word under the cursor starts with @, suggest runs to reference.
  const mentionMatch = /(^|\s)@(?:run:)?([\w./-]*)$/.exec(input)
  const mentionQuery = mentionMatch ? mentionMatch[2].toLowerCase() : null
  const mentionRuns = mentionQuery === null ? [] :
    runs.filter(r => r.run_id.toLowerCase().includes(mentionQuery)).slice(0, 6)
  const pickMention = (rid) => {
    setInput(v => v.replace(/(^|\s)@(?:run:)?[\w./-]*$/, (all, pre) => `${pre}@run:${rid} `))
    if (inputRef.current) inputRef.current.focus()
  }

  const refreshSessions = async () => {
    try { const r = await assistantSessions(); setSessions(r.sessions || []) } catch { /* offline */ }
  }
  useEffect(() => { refreshSessions() }, [])
  const atBottomRef = useRef(true)
  const onFeedScroll = (e) => { const f = e.currentTarget; atBottomRef.current = f.scrollHeight - f.scrollTop - f.clientHeight < 80 }
  useEffect(() => {   // don't yank the reader down when they've scrolled up during a streaming reply
    const f = feedRef.current
    if (f && atBottomRef.current) requestAnimationFrame(() => { f.scrollTop = f.scrollHeight })
  }, [msgs, busy])

  const openSession = async (id) => {
    if (abortRef.current) abortRef.current.abort()   // stop an in-flight turn before switching away
    sidRef.current = id; setSid(id); setErr(null)
    try {
      const s = await assistantGet(id)
      setMsgs(s.messages || [])
      if (s.meta && s.meta.mode) setMode(s.meta.mode)
    } catch (e) { setErr(e.message) }
  }
  const newChat = () => { if (abortRef.current) abortRef.current.abort(); sidRef.current = null; setSid(null); setMsgs([]); setErr(null); setInput('') }
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
  const share = async () => {
    if (!sid) return
    try {
      const r = await assistantShare(sid)
      const url = location.origin + location.pathname + r.url
      try { await navigator.clipboard.writeText(url) } catch { /* clipboard blocked */ }
      setErr(null); window.alert('Read-only share link copied:\n' + url)
    } catch (e) { setErr(e.message) }
  }

  const onRevert = async (absPath) => {
    try { const r = await assistantRevert(absPath); setErr(null); window.alert(r.result || 'reverted') }
    catch (e) { setErr(e.message) }
  }

  const resolvePerm = async (reqId, decision) => {
    setPending(p => p.filter(x => x.id !== reqId))     // optimistic — the turn resumes server-side
    try { await assistantResolve(reqId, decision) } catch (e) { setErr(e.message) }
  }

  // Update the last (streaming) assistant message in place as tokens/metadata arrive.
  const patchLast = (patch) => setMsgs(m => {
    const c = [...m]; const i = c.length - 1
    if (i >= 0) c[i] = { ...c[i], ...(typeof patch === 'function' ? patch(c[i]) : patch) }
    return c
  })

  const send = async (text) => {
    const t = (text ?? input).trim()
    if (!t || busy) return
    setErr(null); setInput('')
    let id = sid
    if (!id) {
      try { const m = await assistantCreate(t.slice(0, 60), mode); id = m.id; sidRef.current = id; setSid(id); refreshSessions() }
      catch (e) { setErr(e.message); return }
    }
    atBottomRef.current = true
    setMsgs(m => [...m, { role: 'user', content: t }, { role: 'assistant', content: '', streaming: true }])
    setBusy(true)
    const ctrl = new AbortController(); abortRef.current = ctrl
    // poll permissions concurrently so a mutating action that pauses the turn can be approved. Stops
    // on turn end OR component unmount (mountedRef), so it can't leak or setState after unmount.
    let polling = true
    ;(async () => { while (polling && mountedRef.current) { try { const p = await assistantPermissions(id); if (mountedRef.current) setPending(p.pending || []) } catch { /* transient */ } await sleep(800) } })()
    // Guard on the session too: after the user switches sessions, a late token from this turn must
    // not patch the now-displayed other session's transcript.
    const safe = (fn) => (...a) => { if (mountedRef.current && sidRef.current === id) fn(...a) }
    let acc = ''; let errored = null
    try {
      const res = await assistantMessageStream(id, t, mode, {
        onToken: safe((tok) => { acc += tokText(tok); patchLast({ content: acc }) }),
        onText: safe((txt) => patchLast(prev => ({ activity: [...(prev.activity || []), { type: 'text', content: txt }] }))),
        onStep: safe((s) => { setLiveSteps(x => [...x, s]); patchLast(prev => {
          const a = prev.activity || []; const last = a[a.length - 1]
          return last && last.type === 'tools'
            ? { activity: [...a.slice(0, -1), { ...last, labels: [...last.labels, s] }] }
            : { activity: [...a, { type: 'tools', labels: [s] }] }
        }) }),
        onTodos: safe((items) => { setLiveTodos(items); patchLast({ todos: items }) }),
        onError: safe((e) => { errored = e; setErr(e) }),
      }, ctrl.signal)
      if (!mountedRef.current || sidRef.current !== id) return   // switched away: don't touch the feed
      if (res && res.ok === false && res.error) { errored = res.error; setErr(res.error) }
      patchLast({ content: (res && res.reply) || acc || (errored ? `${errored}` : '(no reply)'), streaming: false,
        steps: res && res.steps, applied: res && res.applied, proposals: res && res.proposals, todos: res && res.todos })
      refreshSessions()
    } catch (e) {
      // AbortError from a session switch is expected; only report/patch if we're still on this session.
      if (mountedRef.current && sidRef.current === id && e.name !== 'AbortError') { setErr(e.message); patchLast({ content: acc || 'Could not reach the assistant.', streaming: false }) }
    } finally {
      // Turn-global UI state (busy/pending/live) is reset regardless of session so a switch mid-turn
      // can't wedge the composer in a busy state; only the per-session message feed is guarded above.
      polling = false; abortRef.current = null
      if (mountedRef.current) { setBusy(false); setPending([]); setLiveSteps([]); setLiveTodos([]) }
    }
  }

  const activeMode = MODES.find(x => x.id === mode) || MODES[0]
  const last = msgs[msgs.length - 1]
  const streamingStarted = !!(last && last.streaming && last.content)

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
        {sid && <button className="btn sm ghost" onClick={share} title="copy a read-only link">share</button>}
      </div>

      <div className="asst-feed" ref={feedRef} onScroll={onFeedScroll}>
        {msgs.length === 0 && <div className="gen-empty">
          <div className="muted" style={{ marginBottom: 8 }}>
            Ask me anything — I can inspect the code, read your runs, and (once you switch modes) edit
            files, run tests and fix LoopLab itself.
          </div>
          {['What runs are live right now?', 'Explain how the event log works in this repo',
            'Read the README and summarize LoopLab'].map(s =>
            <button key={s} className="gen-seed" onClick={() => send(s)}>{s}</button>)}
        </div>}
        {/* The streaming assistant placeholder is itself in `msgs`, so its Turn renders the live
            activity timeline (interstitial text + collapsed tool status) and the thinking indicator. */}
        {msgs.map((m, i) => <Turn key={i} m={m} runsById={runsById} onRevert={onRevert} />)}
        {pending.map(req => <PermCard key={req.id} req={req} onResolve={resolvePerm} />)}
      </div>

      {err && <div className="notice" style={{ borderColor: 'var(--fail)', color: '#ffd3d3', margin: '0 12px' }}>{err}</div>}

      <div className="chat-in asst-in">
        {mentionRuns.length > 0 && <div className="asst-mention-pop">
          {mentionRuns.map(r => <button key={r.run_id} className="asst-mention-item" onClick={() => pickMention(r.run_id)}>
            <span className={'asst-run-dot' + (r.engine_running ? ' live' : '')} />
            <b>{r.run_id}</b><span className="muted"> {r.phase || (r.finished ? 'finished' : 'running')}</span>
          </button>)}
        </div>}
        {slashCmds.length > 0 && <div className="asst-mention-pop">
          {slashCmds.map(c => <button key={c.name} className="asst-mention-item" onClick={() => pickCmd(c.name)}>
            <b>/{c.name}</b><span className="muted"> {c.desc}</span>
          </button>)}
        </div>}
        <textarea className="text" ref={inputRef} placeholder="Message the assistant…  (/ for commands · @ to reference a run · Enter to send)"
          value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && mentionRuns.length === 0 && slashCmds.length === 0) { e.preventDefault(); send() } }} />
        <div className="toolbar" style={{ marginTop: 6 }}>
          <span className="muted" style={{ flex: 1, fontSize: 11 }}>mode: <b>{activeMode.label}</b></span>
          <button className="btn sm primary" disabled={!input.trim() || busy} onClick={() => send()}>{busy ? '…' : 'Send'}</button>
        </div>
      </div>
    </div>
  </div>
}
