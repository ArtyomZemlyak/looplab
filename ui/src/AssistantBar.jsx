import React, { useEffect, useRef, useState } from 'react'
import { Turn, PermCard } from './AssistantChat.jsx'
import {
  CONTROL, get, fmtAgo, ASSISTANT_MODES as MODES, tokText, assistantCreate, assistantMessageStream,
  assistantCommands, assistantRevert, assistantSessions, assistantGet, assistantDelete,
  assistantPermissions, assistantResolve,
} from './util.js'

// ── ONE assistant, three flowing views: bar ⇄ drawer(right) ⇄ full ────────────────────────────────
//
// A single component owns the whole conversation (session, messages, streaming) and renders it in one
// of three views. Because it's mounted once in the App shell (outside the router), the assistant is
// NEVER reset by navigation — start a chat in the menu, walk into a run, the history is still there and
// any in-flight turn keeps streaming in the background.
//
//  • bar    — always-docked bottom strip. While the model thinks it does NOT expand (just a pip); when
//             the reply lands its first line surfaces inline and the bar glows ("new").
//  • drawer — right-hand panel (Cursor-style): the thread + per-turn actions. Collapse → folds to the
//             bar (which then highlights the last message).
//  • full   — full-screen overlay: sessions sidebar + thread + composer. Leaving it folds back to the
//             bar; a running turn keeps going in the background.
//
// Intent is resolved ALGORITHMICALLY for unambiguous input (a bare "/stop" stops the open run with no
// LLM call); anything with free text goes to the assistant. In a run, the only context injected is
// "run X is open" — the assistant reads whatever else it needs via its tools.

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// Run-control commands safe to fire directly (no model). `arg:true` needs a node id (e.g. /approve #12).
const DIRECT = {
  stop:    { run: (rid) => CONTROL.abort(rid),   ok: '⏹ run stopped' },
  abort:   { run: (rid) => CONTROL.abort(rid),   ok: '⏹ run aborted' },
  pause:   { run: (rid) => CONTROL.pause(rid),   ok: '⏸ run paused' },
  resume:  { run: (rid) => CONTROL.resume(rid),  ok: '▶ run resumed' },
  ratify:  { run: (rid) => CONTROL.ratify(rid),  ok: '✓ eval spec ratified' },
  approve: { arg: true, run: (rid, id) => CONTROL.approve(rid, id), ok: (id) => `✓ approved #${id}` },
}
// Unambiguous = a lone /name optionally + a single #id token, and NOTHING else. Trailing prose → LLM.
function parseDirect(t) {
  const m = /^\/([a-z_]+)(?:\s+#?(\d+))?\s*$/i.exec(t)
  if (!m) return null
  const name = m[1].toLowerCase()
  const spec = DIRECT[name]
  if (!spec) return null
  const arg = m[2] ? Number(m[2]) : null
  if (spec.arg && arg == null) return null
  return { name, spec, arg }
}

const firstLine = (s) => (s || '').replace(/[#*`>_-]/g, '').split('\n').map(l => l.trim()).find(Boolean) || ''

export default function AssistantBar({ runId, hidden = false }) {
  const [input, setInput] = useState('')
  const [sid, setSid] = useState(null)
  const [msgs, setMsgs] = useState([])
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState('')      // beginning of the latest reply (collapsed bar)
  const [hasNew, setHasNew] = useState(false)     // highlight the bar until a view is opened
  const [view, setView] = useState('bar')         // 'bar' | 'drawer' | 'full'
  const [mode, setMode] = useState('plan')
  const [toast, setToast] = useState(null)
  const [commands, setCommands] = useState([])
  const [runs, setRuns] = useState([])
  const [pending, setPending] = useState([])      // live HITL confirm requests
  const [sessions, setSessions] = useState([])    // full-view session list

  const mountedRef = useRef(true)
  const abortRef = useRef(null)
  const inputRef = useRef(null)
  const feedRef = useRef(null)
  useEffect(() => () => { mountedRef.current = false; if (abortRef.current) abortRef.current.abort() }, [])

  useEffect(() => { assistantCommands().then(r => setCommands(r.commands || [])).catch(() => {}) }, [])
  useEffect(() => {
    let alive = true
    const load = () => get('/api/runs').then(r => alive && setRuns(r || [])).catch(() => {})
    load(); const t = setInterval(load, 6000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  const runsById = React.useMemo(() => Object.fromEntries(runs.map(r => [r.run_id, r])), [runs])
  const refreshSessions = () => assistantSessions().then(r => setSessions(r.sessions || [])).catch(() => {})
  useEffect(() => { if (view === 'full') refreshSessions() }, [view])

  const feedOpen = view === 'drawer' || view === 'full'
  useEffect(() => { if (feedOpen && feedRef.current) requestAnimationFrame(() => { feedRef.current.scrollTop = feedRef.current.scrollHeight }) }, [msgs, view, busy])

  const flash = (m) => { setToast(m); setTimeout(() => mountedRef.current && setToast(null), 2600) }
  const safe = (fn) => (...a) => { if (mountedRef.current) fn(...a) }
  const patchLast = (patch) => setMsgs(m => {
    const c = [...m]; const i = c.length - 1
    if (i >= 0) c[i] = { ...c[i], ...(typeof patch === 'function' ? patch(c[i]) : patch) }
    return c
  })
  const lastAssistant = () => { for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'assistant') return msgs[i]; return null }

  // ── view transitions ──
  const openDrawer = () => { setView('drawer'); setHasNew(false) }
  const openFull = () => { setView('full'); setHasNew(false) }
  const collapseToBar = () => {
    // fold to the bar and surface the last reply there (glow if there's something to see).
    const la = lastAssistant()
    if (la && la.content) { setPreview(firstLine(la.content).slice(0, 120)); setHasNew(true) }
    setView('bar')
  }
  const toggleDrawer = () => (view === 'bar' ? openDrawer() : collapseToBar())

  // ── sessions (full view) ──
  const openSession = async (id) => {
    setSid(id)
    try { const s = await assistantGet(id); if (mountedRef.current) { setMsgs(s.messages || []); if (s.meta?.mode) setMode(s.meta.mode) } }
    catch (e) { flash(e.message) }
  }
  const newChat = () => { setSid(null); setMsgs([]); setPreview(''); setHasNew(false); setInput('') }
  const delSession = async (id, e) => {
    e?.stopPropagation()
    try { await assistantDelete(id); if (id === sid) newChat(); refreshSessions() } catch (e2) { flash(e2.message) }
  }

  const resolvePerm = async (reqId, decision) => {
    setPending(p => p.filter(x => x.id !== reqId))
    try { await assistantResolve(reqId, decision) } catch (e) { flash(e.message) }
  }
  const onRevert = async (absPath) => {
    try { const r = await assistantRevert(absPath); flash(r.result || 'reverted') } catch (e) { flash(e.message) }
  }

  const runDirect = async (d) => {
    if (!runId) { flash(`/${d.name} needs an open run`); return }
    try { await d.spec.run(runId, d.arg); flash(typeof d.spec.ok === 'function' ? d.spec.ok(d.arg) : d.spec.ok) }
    catch (e) { flash('failed: ' + (e.message || e)) }
  }

  // Stream one instruction to the assistant. `userText` = the bubble shown; `instruction` = what the
  // model receives (we append the run context to it, not to the bubble). `ensureVisible` pops the
  // drawer when we're collapsed (used by /new so the planning + launch card are visible as they stream).
  const runLLM = async (instruction, { userText = null, ensureVisible = false } = {}) => {
    if (ensureVisible && view === 'bar') setView('drawer')
    const wasBar = view === 'bar' && !ensureVisible
    setPreview(''); setHasNew(false)
    let id = sid
    if (!id) {
      try { const m = await assistantCreate((userText || instruction).slice(0, 60), mode); id = m.id; if (mountedRef.current) setSid(id) }
      catch { flash('assistant offline'); return }
    }
    setMsgs(m => [...m, { role: 'user', content: userText || instruction }, { role: 'assistant', content: '', streaming: true }])
    setBusy(true)
    const ctrl = new AbortController(); abortRef.current = ctrl
    // poll permissions so a mutating action that pauses the turn can be approved from any view.
    let polling = true
    ;(async () => { while (polling && mountedRef.current) { try { const p = await assistantPermissions(id); if (mountedRef.current) setPending(p.pending || []) } catch { /* transient */ } await sleep(800) } })()
    let acc = ''
    try {
      const res = await assistantMessageStream(id, instruction, mode, {
        onToken: safe((tok) => { acc += tokText(tok); patchLast({ content: acc }) }),
        onTodos: safe((items) => patchLast({ todos: items })),
        onError: safe((e) => flash(e)),
      }, ctrl.signal)
      if (!mountedRef.current) return
      const reply = (res && res.reply) || acc || '(no reply)'
      patchLast({ content: reply, streaming: false, steps: res && res.steps, applied: res && res.applied,
                  proposals: res && res.proposals, todos: res && res.todos })
      setPreview(firstLine(reply).slice(0, 120)); setHasNew(wasBar)   // glow only if it landed while collapsed
    } catch (e) {
      if (mountedRef.current) { patchLast({ content: acc || 'Could not reach the assistant.', streaming: false }); flash(e.message) }
    } finally { polling = false; abortRef.current = null; if (mountedRef.current) { setBusy(false); setPending([]) } }
  }

  const send = () => {
    const t = input.trim()
    if (!t || busy) return
    // /new [goal] · /run [goal] · /genesis → plan + launch a run INSIDE the chat (inline launch card).
    const mNew = /^\/(new|genesis|run)\b\s*([\s\S]*)$/i.exec(t)
    if (mNew) {
      setInput('')
      const goal = mNew[2].trim()
      runLLM(
        goal ? `Plan a new run for this goal and show me a launch card to start it: ${goal}`
             : 'I want to start a new run. Propose a run spec (name, task, key settings) as a launch card I can start; ask me for anything you need first.',
        { userText: goal ? `/new ${goal}` : '/new', ensureVisible: true })
      return
    }
    // unambiguous slash command → run control, no LLM
    const direct = parseDirect(t)
    if (direct) { setInput(''); runDirect(direct); return }
    // everything else → the assistant. In a run, inject ONLY "run X is open"; it reads the rest itself.
    setInput('')
    const instruction = runId
      ? `${t}\n\n[UI context: run "${runId}" is currently open. Use the run tools if this is about it.]`
      : t
    runLLM(instruction, { userText: t })
  }

  const activeMode = MODES.find(x => x.id === mode) || MODES[0]

  // slash suggestions (bar): direct run-control + /new + assistant commands.
  const slashMatch = /^\/(\w*)$/.exec(input)
  const directNames = [
    { name: 'new', desc: 'plan & start a run — in this chat' },
    ...Object.keys(DIRECT).map(n => ({ name: n, desc: 'run control · no LLM' })),
  ]
  const suggestions = slashMatch
    ? [...directNames, ...commands.map(c => ({ name: c.name, desc: c.desc }))]
        .filter(c => c.name.startsWith(slashMatch[1].toLowerCase()))
        .filter((c, i, a) => a.findIndex(x => x.name === c.name) === i).slice(0, 6)
    : []

  const onKey = (e) => { if (e.key === 'Enter' && !e.shiftKey && suggestions.length === 0) { e.preventDefault(); send() } }

  // The thread body, shared by drawer + full. A plain function (not a nested component) so it inlines
  // its elements — rendering it as <Thread/> would remount the whole feed on every streamed token and
  // collapse any expanded turn.
  const renderThread = () => <>
    {msgs.length === 0 && <div className="muted" style={{ padding: 14, fontSize: 12 }}>
      Ask anything — inspect the code, read your runs, steer or create runs{runId ? ` · run “${runId}” is open` : ''}. Type below.
    </div>}
    {msgs.map((m, i) => <Turn key={i} m={m} runsById={runsById} onRevert={onRevert} />)}
    {pending.map(req => <PermCard key={req.id} req={req} onResolve={resolvePerm} />)}
    {busy && pending.length === 0 && (!msgs.length || !msgs[msgs.length - 1].content) &&
      <div className="feed-msg chat assistant"><div className="fm-body">
        <div className="chat-who">assistant</div>
        <div className="chat-bubble"><div className="chat-text muted">… thinking</div></div></div></div>}
  </>

  if (hidden) return null

  return <>
    {/* ── bottom bar (hidden while the full overlay is up) ── */}
    {view !== 'full' && <div className={'cmdbar-dock' + (busy ? ' thinking' : '') + (hasNew ? ' fresh' : '') + (view === 'drawer' ? ' open' : '')}
         onDoubleClick={openDrawer}>
      <button className="cmdbar-ic" title="open the full assistant" onClick={openFull}>✦</button>
      <div className="cmdbar-field">
        {suggestions.length > 0 && <div className="cmdbar-pop">
          {suggestions.map(c => <button key={c.name} className="cmdbar-pop-item"
            onMouseDown={(e) => { e.preventDefault(); setInput(`/${c.name} `); inputRef.current?.focus() }}>
            <b>/{c.name}</b><span className="muted"> {c.desc}</span></button>)}
        </div>}
        <input className="cmdbar-in" ref={inputRef} value={input} disabled={busy}
          onChange={e => setInput(e.target.value)} onKeyDown={onKey}
          placeholder={runId
            ? 'Command or ask…  /stop · /pause · /approve #12 · or describe what to do'
            : 'Describe a run to start, or ask the assistant…  ( / for commands )'} />
      </div>
      {busy
        ? <span className="cmdbar-status thinking"><span className="cmdbar-pip" /> thinking…</span>
        : preview
          ? <button className="cmdbar-status preview" title="open the conversation" onClick={openDrawer}>
              <span className="cmdbar-who">assistant</span> {preview}<span className="cmdbar-more"> ▸</span></button>
          : msgs.length > 0
            ? <button className="cmdbar-status" title="open the conversation" onClick={openDrawer}>💬 chat</button>
            : null}
      <button className="cmdbar-go" title="send (Enter)" disabled={!input.trim() || busy} onClick={send}>▶</button>
      <button className={'cmdbar-drawer-btn' + (view === 'drawer' ? ' on' : '')}
        title={view === 'drawer' ? 'hide chat' : 'open chat on the right'} onClick={toggleDrawer}>▧</button>
      {toast && <div className="cmdbar-toast">{toast}</div>}
    </div>}

    {/* ── right drawer ── */}
    {view === 'drawer' && <div className="asst-drawer">
      <div className="asst-drawer-h">
        <b className="asst-drawer-ttl">Assistant</b>
        <div className="asst-modes sm">
          {MODES.map(x => <button key={x.id} className={'asst-mode' + (x.id === mode ? ' on' : '')}
            title={x.hint} onClick={() => setMode(x.id)}>{x.label}</button>)}
        </div>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn xs ghost" title="new chat" onClick={newChat}>＋</button>
        <button className="btn xs ghost" title="expand to full view" onClick={openFull}>⤢</button>
        <button className="btn xs ghost" title="collapse to the bar" onClick={collapseToBar}>✕</button>
      </div>
      <div className="asst-drawer-feed" ref={feedRef}>{renderThread()}</div>
      <div className="asst-drawer-foot muted">
        mode: <b>{activeMode.label}</b> — type in the bar below · <span onClick={openFull} className="asst-drawer-link">full view</span>
      </div>
    </div>}

    {/* ── full overlay ── */}
    {view === 'full' && <div className="asst-view asst-full">
      <div className="asst-side">
        <div className="asst-side-h">
          <button className="btn sm" title="fold back to the bar" onClick={collapseToBar}>▾ bar</button>
          <span className="ttl" style={{ flex: 1 }}>Assistant</span>
          <button className="btn sm primary" onClick={newChat}>+ New</button>
        </div>
        <div className="asst-sessions">
          {sessions.length === 0 && <div className="muted" style={{ padding: 12, fontSize: 12 }}>No chats yet.</div>}
          {sessions.map(s => <div key={s.id} className={'asst-sess' + (s.id === sid ? ' active' : '')} onClick={() => openSession(s.id)}>
            <div className="asst-sess-t">{s.title || 'Chat'}</div>
            <div className="asst-sess-m">{fmtAgo(s.updated)}</div>
            <button className="btn xs ghost asst-sess-x" onClick={(e) => delSession(s.id, e)} title="delete">✕</button>
          </div>)}
        </div>
      </div>
      <div className="asst-main">
        <div className="asst-main-h">
          <div className="asst-modes">
            {MODES.map(x => <button key={x.id} className={'asst-mode' + (x.id === mode ? ' on' : '')}
              title={x.hint} onClick={() => setMode(x.id)}>{x.label}</button>)}
          </div>
          <span className="muted" style={{ flex: 1, fontSize: 11 }}>{activeMode.hint}</span>
          <button className="btn sm ghost" title="dock to the right" onClick={openDrawer}>▧ drawer</button>
          <button className="btn sm ghost" title="fold to the bar" onClick={collapseToBar}>▾ bar</button>
        </div>
        <div className="asst-feed" ref={feedRef}>{renderThread()}</div>
        <div className="chat-in asst-in">
          <textarea className="text" ref={inputRef} value={input} onChange={e => setInput(e.target.value)}
            onKeyDown={onKey} placeholder="Message the assistant…  (/ for commands · Enter to send)" />
          <div className="toolbar" style={{ marginTop: 6 }}>
            <span className="muted" style={{ flex: 1, fontSize: 11 }}>mode: <b>{activeMode.label}</b>{runId ? ` · run “${runId}” open` : ''}</span>
            <button className="btn sm primary" disabled={!input.trim() || busy} onClick={send}>{busy ? '…' : 'Send'}</button>
          </div>
        </div>
      </div>
    </div>}
  </>
}
