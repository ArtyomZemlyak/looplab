import React, { useEffect, useRef, useState } from 'react'
import { Turn } from './AssistantChat.jsx'
import {
  CONTROL, get, assistantCreate, assistantMessageStream, assistantCommands, assistantRevert,
} from './util.js'

// ── Chat-first command bar (always docked at the BOTTOM of every non-assistant view) ──────────────
//
// Behaviour (per product spec):
//  • Unambiguous input is executed ALGORITHMICALLY — a bare "/stop" stops the open run with NO LLM
//    call. Add any free-text ("/stop стопни пож") and it's no longer unambiguous → the assistant (LLM)
//    handles it. parseDirect() is the whole decision: command + at most one id token, nothing else.
//  • While the LLM thinks the bar does NOT expand — it shows a thinking pip in place. When the answer
//    lands, the BEGINNING of the reply appears inline in the bar and the bar is highlighted ("new").
//  • Single-click the reply preview → the chat opens as a right-hand drawer (Cursor-style). A
//    double-click anywhere on the bar opens it immediately.
//  • A micro-button (⤢) jumps to the full-page assistant, carrying this session (or offering a new one).

// Run-control commands that are safe to fire directly, with no model in the loop. Each is a pure
// {type,data} append via CONTROL. `arg:true` means it needs a single node id (e.g. /approve #12);
// without one it's ambiguous and falls through to the assistant.
const DIRECT = {
  stop:    { run: (rid) => CONTROL.abort(rid),   ok: '⏹ run stopped' },
  abort:   { run: (rid) => CONTROL.abort(rid),   ok: '⏹ run aborted' },
  pause:   { run: (rid) => CONTROL.pause(rid),   ok: '⏸ run paused' },
  resume:  { run: (rid) => CONTROL.resume(rid),  ok: '▶ run resumed' },
  ratify:  { run: (rid) => CONTROL.ratify(rid),  ok: '✓ eval spec ratified' },
  approve: { arg: true, run: (rid, id) => CONTROL.approve(rid, id), ok: (id) => `✓ approved #${id}` },
}

// A command is "unambiguous" only when it's a lone /name optionally followed by a single #id token and
// NOTHING else. Any trailing prose makes it a natural-language request → return null → the LLM path.
function parseDirect(t) {
  const m = /^\/([a-z_]+)(?:\s+#?(\d+))?\s*$/i.exec(t)
  if (!m) return null
  const name = m[1].toLowerCase()
  const spec = DIRECT[name]
  if (!spec) return null                          // unknown slash command → let the assistant answer
  const arg = m[2] ? Number(m[2]) : null
  if (spec.arg && arg == null) return null        // needs an id but none given → ambiguous → LLM
  return { name, spec, arg }
}

const firstLine = (s) => (s || '').replace(/[#*`>_-]/g, '').split('\n').map(l => l.trim()).find(Boolean) || ''
const tokText = (tok) => (tok && tok.text != null) ? tok.text : (typeof tok === 'string' ? tok : '')

const MODES = [
  { id: 'plan', label: 'Plan', hint: 'read-only — inspect & propose (safe)' },
  { id: 'default', label: 'Ask', hint: 'confirm every change' },
  { id: 'acceptEdits', label: 'Auto-edit', hint: 'edits apply; commands ask' },
  { id: 'auto', label: 'Auto', hint: 'runs everything without asking' },
]

export default function AssistantBar({ runId }) {
  const [input, setInput] = useState('')
  const [sid, setSid] = useState(null)
  const [msgs, setMsgs] = useState([])
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState('')      // beginning of the latest reply, shown collapsed
  const [hasNew, setHasNew] = useState(false)     // highlight the bar until the drawer is opened
  const [open, setOpen] = useState(false)         // right-hand chat drawer
  const [mode, setMode] = useState('plan')
  const [toast, setToast] = useState(null)
  const [commands, setCommands] = useState([])
  const [runs, setRuns] = useState([])

  const mountedRef = useRef(true)
  const abortRef = useRef(null)
  const inputRef = useRef(null)
  const feedRef = useRef(null)
  useEffect(() => () => { mountedRef.current = false; if (abortRef.current) abortRef.current.abort() }, [])

  useEffect(() => { assistantCommands().then(r => setCommands(r.commands || [])).catch(() => {}) }, [])
  // runs power the @run chips inside the drawer thread (reuses AssistantChat's <Turn/>).
  useEffect(() => {
    let alive = true
    const load = () => get('/api/runs').then(r => alive && setRuns(r || [])).catch(() => {})
    load(); const t = setInterval(load, 6000)
    return () => { alive = false; clearInterval(t) }
  }, [])
  const runsById = React.useMemo(() => Object.fromEntries(runs.map(r => [r.run_id, r])), [runs])

  useEffect(() => { if (open && feedRef.current) requestAnimationFrame(() => { feedRef.current.scrollTop = feedRef.current.scrollHeight }) }, [msgs, open, busy])

  const flash = (m) => { setToast(m); setTimeout(() => mountedRef.current && setToast(null), 2600) }
  const safe = (fn) => (...a) => { if (mountedRef.current) fn(...a) }
  const patchLast = (patch) => setMsgs(m => {
    const c = [...m]; const i = c.length - 1
    if (i >= 0) c[i] = { ...c[i], ...(typeof patch === 'function' ? patch(c[i]) : patch) }
    return c
  })

  // slash-command suggestions when typing a bare "/name" (direct run-control + assistant commands).
  const slashMatch = /^\/(\w*)$/.exec(input)
  const directNames = [
    { name: 'new', desc: 'start a run — open the planner' },
    ...Object.keys(DIRECT).map(n => ({ name: n, desc: 'run control · no LLM' })),
  ]
  const suggestions = slashMatch
    ? [...directNames, ...commands.map(c => ({ name: c.name, desc: c.desc }))]
        .filter(c => c.name.startsWith(slashMatch[1].toLowerCase()))
        .filter((c, i, a) => a.findIndex(x => x.name === c.name) === i).slice(0, 6)
    : []

  const openDrawer = () => { setOpen(true); setHasNew(false) }
  const toFull = () => { location.hash = sid ? `#/assistant/s/${encodeURIComponent(sid)}` : '#/assistant' }

  const runDirect = async (d) => {
    if (!runId) { flash(`/${d.name} needs an open run`); return }
    try { await d.spec.run(runId, d.arg); flash(typeof d.spec.ok === 'function' ? d.spec.ok(d.arg) : d.spec.ok) }
    catch (e) { flash('failed: ' + (e.message || e)) }
  }

  // Open the Genesis run-planner. Works from any view: a flag survives the hop to the run list (which
  // owns the planner modal), and an event covers the case where the list is already mounted.
  const openGenesis = (seed = '') => {
    try { sessionStorage.setItem('ll.openGenesis', seed || '1') } catch { /* private mode */ }
    if (location.hash && location.hash !== '#' && location.hash !== '#/') location.hash = ''
    window.dispatchEvent(new CustomEvent('ll:new-run', { detail: { seed } }))
  }

  const send = async () => {
    const t = input.trim()
    if (!t || busy) return
    // 0) bare "/new" (or "/genesis") → open the run planner directly, no LLM.
    if (t === '/new' || t === '/genesis') { setInput(''); openGenesis(''); return }
    // 1) algorithmic, unambiguous → no LLM
    const direct = parseDirect(t)
    if (direct) { setInput(''); runDirect(direct); return }
    // 2) natural language / ambiguous → the assistant (streamed)
    setInput(''); setPreview(''); setHasNew(false)
    let id = sid
    if (!id) {
      try { const m = await assistantCreate(t.slice(0, 60), mode); id = m.id; if (mountedRef.current) setSid(id) }
      catch { flash('assistant offline'); return }
    }
    setMsgs(m => [...m, { role: 'user', content: t }, { role: 'assistant', content: '', streaming: true }])
    setBusy(true)
    const ctrl = new AbortController(); abortRef.current = ctrl
    let acc = ''
    try {
      const res = await assistantMessageStream(id, t, mode, {
        onToken: safe((tok) => { acc += tokText(tok); patchLast({ content: acc }) }),
        onTodos: safe((items) => patchLast({ todos: items })),
        onError: safe((e) => flash(e)),
      }, ctrl.signal)
      if (!mountedRef.current) return
      const reply = (res && res.reply) || acc || '(no reply)'
      patchLast({ content: reply, streaming: false, steps: res && res.steps, applied: res && res.applied,
                  proposals: res && res.proposals, todos: res && res.todos })
      // the bar stays collapsed — only the BEGINNING of the reply surfaces, with a "new" highlight.
      setPreview(firstLine(reply).slice(0, 120)); setHasNew(!open)
    } catch (e) {
      if (mountedRef.current) { patchLast({ content: acc || 'Could not reach the assistant.', streaming: false }); flash(e.message) }
    } finally { if (mountedRef.current) { setBusy(false) } abortRef.current = null }
  }

  const onRevert = async (absPath) => {
    try { const r = await assistantRevert(absPath); flash(r.result || 'reverted') } catch (e) { flash(e.message) }
  }

  const activeMode = MODES.find(x => x.id === mode) || MODES[0]

  return <>
    <div className={'cmdbar-dock' + (busy ? ' thinking' : '') + (hasNew ? ' fresh' : '') + (open ? ' open' : '')}
         onDoubleClick={openDrawer}>
      <button className="cmdbar-ic" title="open the full-page assistant (carries this chat)" onClick={toFull}>✦</button>

      <div className="cmdbar-field">
        {suggestions.length > 0 && <div className="cmdbar-pop">
          {suggestions.map(c => <button key={c.name} className="cmdbar-pop-item"
            onMouseDown={(e) => { e.preventDefault(); setInput(`/${c.name} `); inputRef.current?.focus() }}>
            <b>/{c.name}</b><span className="muted"> {c.desc}</span></button>)}
        </div>}
        <input className="cmdbar-in" ref={inputRef} value={input} disabled={busy}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && suggestions.length === 0) { e.preventDefault(); send() } }}
          placeholder={runId
            ? 'Command or ask…  /stop · /pause · /approve #12 · or describe what to do'
            : 'Describe a run to start, or ask the assistant…  ( / for commands )'} />
      </div>

      {/* Collapsed status/preview: a thinking pip while streaming; the reply's first line once ready
          (click it — or double-click the bar — to open the chat on the right). */}
      {busy
        ? <span className="cmdbar-status thinking"><span className="cmdbar-pip" /> thinking…</span>
        : preview
          ? <button className="cmdbar-status preview" title="open the conversation" onClick={openDrawer}>
              <span className="cmdbar-who">assistant</span> {preview}<span className="cmdbar-more"> ▸</span></button>
          : msgs.length > 0
            ? <button className="cmdbar-status" title="open the conversation" onClick={openDrawer}>💬 chat</button>
            : null}

      <button className="cmdbar-go" title="send (Enter)" disabled={!input.trim() || busy} onClick={send}>▶</button>
      <button className={'cmdbar-drawer-btn' + (open ? ' on' : '')} title={open ? 'hide chat' : 'open chat on the right'}
        onClick={() => open ? setOpen(false) : openDrawer()}>▧</button>

      {toast && <div className="cmdbar-toast">{toast}</div>}
    </div>

    {/* Right-hand chat drawer (Cursor-style): the conversation thread + extra actions. The bottom bar
        remains the single composer, so there's exactly one input everywhere. */}
    {open && <div className="asst-drawer">
      <div className="asst-drawer-h">
        <b className="asst-drawer-ttl">Assistant</b>
        <div className="asst-modes sm">
          {MODES.map(x => <button key={x.id} className={'asst-mode' + (x.id === mode ? ' on' : '')}
            title={x.hint} onClick={() => setMode(x.id)}>{x.label}</button>)}
        </div>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn xs ghost" title="new chat" onClick={() => { setMsgs([]); setSid(null); setPreview(''); setHasNew(false) }}>＋</button>
        <button className="btn xs ghost" title="open the full-page assistant" onClick={toFull}>⤢</button>
        <button className="btn xs ghost" title="close" onClick={() => setOpen(false)}>✕</button>
      </div>
      <div className="asst-drawer-feed" ref={feedRef}>
        {msgs.length === 0 && <div className="muted" style={{ padding: 14, fontSize: 12 }}>
          Ask anything — inspect the code, read your runs, steer or create runs. Type below.
        </div>}
        {msgs.map((m, i) => <Turn key={i} m={m} runsById={runsById} onRevert={onRevert} />)}
      </div>
      <div className="asst-drawer-foot muted">
        mode: <b>{activeMode.label}</b> — type in the bar below · <span onClick={toFull} className="asst-drawer-link">open full view</span>
      </div>
    </div>}
  </>
}
