import React, { useEffect, useRef, useState } from 'react'
import { Turn, PermCard } from './AssistantChat.jsx'
import { OpIcon } from './icons.jsx'
import {
  CONTROL, get, fmtAgo, ASSISTANT_MODES as MODES, tokText, assistantCreate, assistantMessageStream,
  assistantCommands, assistantRevert, assistantSessions, assistantGet, assistantDelete,
  assistantPermissions, assistantResolve, assistantCancel, assistantProgress,
  assistantFork, assistantShare,
} from './util.js'

// ── ONE assistant, three flowing views: bar ⇄ side(right) ⇄ full ───────────────────────────────
//
// A single component owns the whole conversation (session, messages, streaming) and renders it in one
// of three views. Because it's mounted once in the App shell (outside the router), the assistant is
// NEVER reset by navigation — start a chat in the menu, walk into a run, the history is still there and
// any in-flight turn keeps streaming in the background.
//
//  • bar   — a centered, docked bottom strip (capped to the run-list width). The composer lives here.
//  • side  — a RESIZABLE right-hand panel: the thread + a composer INSIDE it (the bottom bar is gone
//            while the side view is open — input moves into the panel). Only openable once a chat
//            exists. Drag its left edge to resize.
//  • full  — a dedicated opaque page: sessions sidebar + thread + composer. Command hints + delete.
//
// Intent is resolved ALGORITHMICALLY for unambiguous input (a bare "/stop" stops the open run with no
// LLM call); anything with free text goes to the assistant. Attached text files + #experiment refs are
// injected as context. A dropped SSE stream (e.g. a buffering proxy) is RECOVERED by polling the
// session — the background worker persists the reply — so "could not reach" no longer strands a turn.

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// Run-control commands safe to fire directly (no model). `arg:true` needs a node id (e.g. /approve #12).
const DIRECT = {
  stop:     { run: (rid) => CONTROL.stop(rid),     ok: '⏸ run stopped (frozen — not finalized)' },
  finalize: { run: (rid) => CONTROL.finalize(rid), ok: '⏹ run finalizing (wrapping up)' },
  resume:   { run: (rid) => CONTROL.resume(rid),   ok: '▶ run resumed' },
  pause:    { run: (rid) => CONTROL.stop(rid),     ok: '⏸ run stopped (frozen)' },     // alias of /stop
  abort:    { run: (rid) => CONTROL.finalize(rid), ok: '⏹ run finalizing' },           // alias of /finalize
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

// U5 · cheap pre-router: catch a few natural-language control phrases WITHOUT paying for an LLM
// round-trip. Fires ONLY when the phrase names the run ("stop the run", "finalize run") — a bare
// "stop" or "continue" is everyday chat directed at the assistant. `stop` is now a reversible FREEZE
// (safe); the terminal wrap-up is `finalize` (maps everyday "abort/halt/wrap up" onto it).
const _NL_CONTROL = { stop: 'stop', freeze: 'stop', pause: 'stop',
  finalize: 'finalize', abort: 'finalize', halt: 'finalize', wrapup: 'finalize',
  resume: 'resume', continue: 'resume', unpause: 'resume' }
function preRoute(t) {
  const cleaned = t.toLowerCase().replace(/^(please\s+|can you\s+)/, '').replace(/[.!]+$/, '').trim()
  if (!/\brun\b/.test(cleaned)) return null
  const norm = cleaned.replace(/\b(the\s+|this\s+|current\s+)?run\b/g, '').trim()
  const name = _NL_CONTROL[norm]
  return name && DIRECT[name] ? { name, spec: DIRECT[name], arg: null } : null
}
// `#N` must start a token (not follow a word/# char) and end at a boundary — so `#3498db` (hex color),
// URL fragments (`page#12`), and `x#5` don't fabricate an experiment reference.
const refNodes = (t) => [...new Set([...(t || '').matchAll(/(?<![\w#])#(?:node-)?(\d+)\b/gi)].map(m => Number(m[1])))]

// Popular one-tap prompts surfaced in the full view (and side view when empty). Keep short + generic.
const HINTS = [
  { label: 'Summarize my runs', text: 'Summarize the state of my runs — best results, what’s running, what failed.' },
  { label: 'Start a new run', text: '/new ' },
  { label: 'Explain the best result', text: 'Explain the current best experiment and why it works.' },
  { label: "What's next?", text: 'Given my runs so far, propose the next experiment worth trying and why.' },
]

// Attach only text-ish files we can read as plain text (no special parsing). Cap each file so a huge
// paste doesn't blow the context; the backend receives the content inline in the instruction.
const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|log|py|js|jsx|ts|tsx|sh|c|cpp|h|hpp|java|go|rs|rb|sql|html|css|xml|env)$/i
const FILE_CHAR_CAP = 20000
const MAX_FILE_BYTES = 2 * 1024 * 1024   // never readAsText a giant log/csv into the tab (OOM)
const SECRET_RE = /(^|\/)\.env(\.|$)|\.pem$|\.key$|(^|\/)(id_rsa|id_ed25519)$|secret|credential/i
const readFileText = (file) => new Promise((resolve) => {
  const r = new FileReader()
  r.onload = () => resolve({ name: file.name, size: file.size, content: String(r.result || '').slice(0, FILE_CHAR_CAP), truncated: (r.result || '').length > FILE_CHAR_CAP })
  r.onerror = () => resolve(null)
  r.readAsText(file)
})

export default function AssistantBar({ runId, hidden = false }) {
  const [input, setInput] = useState('')
  const [sid, setSid] = useState(null)
  const [msgs, setMsgs] = useState([])
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState('')      // beginning of the latest reply (collapsed bar)
  const [hasNew, setHasNew] = useState(false)     // highlight the bar until a view is opened
  const [view, setView] = useState('bar')         // 'bar' | 'side' | 'full'
  const [mode, setMode] = useState('plan')
  const [toast, setToast] = useState(null)
  const [commands, setCommands] = useState([])
  const [runs, setRuns] = useState([])
  const [pending, setPending] = useState([])      // live HITL confirm requests
  const [sessions, setSessions] = useState([])    // full-view session list
  const [files, setFiles] = useState([])          // attached text files [{name,size,content,truncated}]
  const [sideW, setSideW] = useState(() => Math.min(Math.max(+localStorage.getItem('ll.asstW') || 440, 320), window.innerWidth - 120))

  const mountedRef = useRef(true)
  const abortRef = useRef(null)
  const runningRef = useRef(false)   // a turn is live (stream OR reattach poll); stop clears it to halt both
  const cancelReqRef = useRef(null)  // in-flight server-cancel POST; the next send awaits it so a late
                                     // cancel can't land on (and instantly kill) the NEW turn's event
  const sidRef = useRef(null)   // session the stream callbacks belong to (guards cross-session bleed)
  const inputRef = useRef(null)
  const feedRef = useRef(null)
  const atBottomRef = useRef(true)     // is the feed scrolled to (near) the bottom? gates autoscroll
  const flashTimerRef = useRef(null)   // single toast-clear timer so rapid flashes don't clip each other
  const fileRef = useRef(null)
  useEffect(() => () => { mountedRef.current = false; if (abortRef.current) abortRef.current.abort() }, [])
  useEffect(() => { localStorage.setItem('ll.asstW', sideW) }, [sideW])
  // VS Code-style docking: when the side panel is open, reserve its width on the right so the MAIN
  // view is pushed aside (shrinks) rather than overlaid. The panel is position:fixed; this frees the
  // exact space it occupies. The width lives in a CSS var so a drag-resize reflows the main view live.
  useEffect(() => {
    const b = document.body
    if (view === 'side') { b.classList.add('asst-side-open'); b.style.setProperty('--asst-side-w', sideW + 'px') }
    else b.classList.remove('asst-side-open')
    return () => { b.classList.remove('asst-side-open') }
  }, [view, sideW])

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

  const feedOpen = view === 'side' || view === 'full'
  // Autoscroll ONLY when the user is already near the bottom — don't yank them back down while they've
  // scrolled up to read earlier turns during a streaming reply.
  const onFeedScroll = (e) => { const f = e.currentTarget; atBottomRef.current = f.scrollHeight - f.scrollTop - f.clientHeight < 80 }
  useEffect(() => { if (feedOpen && feedRef.current && atBottomRef.current) requestAnimationFrame(() => { feedRef.current.scrollTop = feedRef.current.scrollHeight }) }, [msgs, view, busy])

  const flash = (m) => { setToast(m); if (flashTimerRef.current) clearTimeout(flashTimerRef.current); flashTimerRef.current = setTimeout(() => mountedRef.current && setToast(null), 2600) }
  const patchLast = (patch) => setMsgs(m => {
    const c = [...m]; const i = c.length - 1
    if (i >= 0) c[i] = { ...c[i], ...(typeof patch === 'function' ? patch(c[i]) : patch) }
    return c
  })
  const lastAssistant = () => { for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'assistant') return msgs[i]; return null }
  // A chat exists (or is starting) → the side/full views have something to show. Gates the side toggle:
  // you can't open the right view from an empty bar or right after /stop cleared it (item: "no button").
  const hasChat = msgs.length > 0 || busy

  // ── view transitions ──
  const openSide = () => { setView('side'); setHasNew(false) }   // openable any time (even empty)
  const openFull = () => { setView('full'); setHasNew(false) }
  const collapseToBar = () => {
    const la = lastAssistant()
    if (la && la.content) { setPreview(firstLine(la.content).slice(0, 120)); setHasNew(true) }
    setView('bar')
  }
  const toggleSide = () => (view === 'side' ? collapseToBar() : openSide())

  // ── resizable side panel (drag its left edge) ──
  const resizeCleanupRef = useRef(null)
  const startResize = (e) => {
    e.preventDefault()
    const x0 = e.clientX, w0 = sideW
    const onMove = (ev) => setSideW(Math.min(Math.max(w0 - (ev.clientX - x0), 320), window.innerWidth - 120))
    const cleanup = () => {
      window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', cleanup)
      document.body.style.cursor = ''; resizeCleanupRef.current = null
    }
    resizeCleanupRef.current = cleanup   // unmount mid-drag must also detach the window listeners
    document.body.style.cursor = 'col-resize'
    window.addEventListener('mousemove', onMove); window.addEventListener('mouseup', cleanup)
  }
  useEffect(() => () => resizeCleanupRef.current?.(), [])

  // ── sessions (full view) ──
  const openSession = async (id, { recover = false } = {}) => {
    // Re-opening the session that is ALREADY live-streaming would abort its own stream (and downgrade
    // to polling) — the thread is already on screen, so just no-op.
    if (id === sidRef.current && runningRef.current) return
    // Leaving a live turn: the departing turn's finally is sid-guarded (it must not clobber the session
    // we switch TO), so IT won't reset the shared flags once sidRef changes — reset them here, or busy
    // stays true forever and the composer wedges on ■.
    if (abortRef.current) { try { abortRef.current.abort() } catch { /* gone */ } abortRef.current = null }
    if (runningRef.current) { runningRef.current = false; setBusy(false); setPending([]) }
    sidRef.current = id; setSid(id)
    try { localStorage.setItem('ll.asstSid', id) } catch { /* private mode */ }
    try {
      const s = await assistantGet(id)
      if (!mountedRef.current) return
      const arr = s.messages || []
      setMsgs(arr); if (s.meta?.mode) setMode(s.meta.mode)
      const la = [...arr].reverse().find(m => m.role === 'assistant' && m.content)
      if (la) setPreview(firstLine(la.content).slice(0, 120))
      // Reattach to a turn still running server-side — after a page RELOAD or after switching AWAY and
      // BACK to this session. The worker keeps going and only persists its reply when done, so the turn
      // state would otherwise vanish from the UI. `progress.active` is authoritative (falls back to the
      // "last turn is the user's, unanswered" heuristic on mount). Show the thinking indicator (a
      // streaming placeholder), stream the live tool steps from the progress channel, and poll until the
      // reply lands. The reply's setMsgs() then replaces the placeholder.
      let prog = { active: false, steps: [] }
      try { prog = await assistantProgress(id) } catch { /* offline */ }
      const inFlight = prog.active || (recover && arr.length && arr[arr.length - 1].role === 'user')
      if (inFlight && mountedRef.current && sidRef.current === id) {
        setBusy(true); runningRef.current = true
        const act = (prog.steps || []).length ? [{ type: 'tools', labels: prog.steps }] : []
        setMsgs(m => (m[m.length - 1] && m[m.length - 1].role === 'assistant' && m[m.length - 1].streaming)
          ? m : [...m, { role: 'assistant', content: '', streaming: true, activity: act }])
        let polling = true
        ;(async () => {
          while (polling && runningRef.current && mountedRef.current && sidRef.current === id) {
            await sleep(1200)
            try {
              const pp = await assistantProgress(id)
              if (!pp.active) break
              if (mountedRef.current && sidRef.current === id && (pp.steps || []).length)
                patchLast(prev => prev && prev.role === 'assistant' && prev.streaming   // only the live placeholder
                  ? { activity: [{ type: 'tools', labels: pp.steps }] } : prev)
              // A reattached turn may be PARKED on a HITL confirm — surface its card too (the send
              // path polls permissions; without this a reload hides the card until the 900s deny).
              const perms = await assistantPermissions(id)
              if (mountedRef.current && sidRef.current === id) setPending(perms.pending || [])
            } catch { /* transient */ }
          }
        })()
        recoverReply(id, arr.length + 1).then(ok => {
          // If recovery gives up (the worker died / no reply ever lands), end the placeholder — else it
          // renders a forever "thinking" spinner (the send path has the same fallback on failure).
          if (!ok && mountedRef.current && sidRef.current === id)
            patchLast(prev => prev && prev.role === 'assistant' && prev.streaming
              ? { streaming: false, content: prev.content || '(the previous turn ended without a reply — ask again)' } : prev)
        }).finally(() => {   // only reset the SHARED busy/runningRef if we're still on this session —
          // else a departing session's late finally would clobber the one the user switched TO.
          polling = false
          if (mountedRef.current && sidRef.current === id) { runningRef.current = false; setBusy(false) }
        })
      }
    } catch (e) {
      // The stored/opened session no longer exists (deleted here or in another tab, run-root reset).
      // Don't leave the dead id in `sid`/localStorage — that wedges the chat (every send targets the
      // 404'd session). Drop it back to a fresh composer.
      if ((e.status === 404 || /404/.test(e.message)) && sidRef.current === id) { newChat() }
      else flash(e.message)
    }
  }
  // Restore the last session on mount so a full page reload never loses the conversation — and if its
  // last turn has no reply yet, recover the in-flight answer (the fix for "typed in the bar, reloaded,
  // got no response"). The reply is persisted server-side only when the worker finishes (server.py).
  useEffect(() => {
    let last = null
    try { last = localStorage.getItem('ll.asstSid') } catch { last = null }
    if (last) openSession(last, { recover: true })
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps
  // Attach a node to the chat context from anywhere (a node card / the Inspector dispatches
  // `ll:attach-node`): append `#<id>` to the composer (deduped), reveal the assistant, and focus.
  useEffect(() => {
    const onAttach = (e) => {
      const id = e?.detail?.id
      if (id == null) return
      setInput(prev => new RegExp(`#(?:node-)?${id}\\b`, 'i').test(prev) ? prev
        : (prev.trim() ? prev.replace(/\s*$/, ' ') : '') + `#${id} `)
      setView(v => (v === 'bar' && hasChat) ? 'side' : v)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
    window.addEventListener('ll:attach-node', onAttach)
    return () => window.removeEventListener('ll:attach-node', onAttach)
  }, [hasChat])
  const newChat = () => {
    if (abortRef.current) { try { abortRef.current.abort() } catch { /* gone */ } abortRef.current = null }
    // the departing turn's finally is sid-guarded — reset the shared flags here (see openSession)
    runningRef.current = false; setBusy(false); setPending([])
    sidRef.current = null; setSid(null); setMsgs([]); setPreview(''); setHasNew(false); setInput(''); setFiles([])
    try { localStorage.removeItem('ll.asstSid') } catch { /* ignore */ }
  }
  const delSession = async (id, e) => {
    e?.stopPropagation()
    setSessions(ss => ss.filter(s => s.id !== id))   // optimistic: drop it now (geesefs list can lag)
    if (id === localStorage.getItem('ll.asstSid')) { try { localStorage.removeItem('ll.asstSid') } catch { /* ignore */ } }
    try { await assistantDelete(id); if (id === sid) newChat() } catch (e2) { flash(e2.message); refreshSessions() }
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

  // ── attached files ──
  const onFiles = async (list) => {
    const picked = [...(list || [])]
    const bad = picked.filter(f => !TEXT_EXT.test(f.name))
    if (bad.length) flash(`skipped non-text: ${bad.map(f => f.name).join(', ')}`)
    const txt = picked.filter(f => TEXT_EXT.test(f.name))
    const secret = txt.filter(f => SECRET_RE.test(f.name))          // don't slurp .env/keys into the prompt
    if (secret.length) flash(`skipped secret-looking: ${secret.map(f => f.name).join(', ')}`)
    const big = txt.filter(f => !SECRET_RE.test(f.name) && f.size > MAX_FILE_BYTES)   // OOM guard BEFORE read
    if (big.length) flash(`too large (>2MB): ${big.map(f => f.name).join(', ')}`)
    const ok = txt.filter(f => !SECRET_RE.test(f.name) && f.size <= MAX_FILE_BYTES)
    const read = (await Promise.all(ok.map(readFileText))).filter(Boolean)
    if (read.length) setFiles(f => {   // dedup by name (React key + removeFile both key on name)
      const seen = new Set(f.map(x => x.name)); return [...f, ...read.filter(r => !seen.has(r.name))]
    })
  }
  const removeFile = (name) => setFiles(f => f.filter(x => x.name !== name))
  const filePreamble = (fs) => fs.length
    ? '\n\n[Attached files — use their content as context]\n' + fs.map(f =>
        `--- ${f.name}${f.truncated ? ' (truncated)' : ''} ---\n${f.content}`).join('\n\n') + '\n'
    : ''

  // Recover a turn whose SSE stream dropped (a buffering proxy can kill a long-lived stream): the
  // background worker keeps running and persists the reply, so poll the session until the assistant
  // message lands, then surface it — instead of stranding the user on "could not reach".
  const recoverReply = async (id, priorLen) => {
    for (let i = 0; i < 180 && runningRef.current && mountedRef.current && sidRef.current === id; i++) {   // ~6min > the 300s turn budget
      await sleep(2000)
      try {
        const s = await assistantGet(id)
        const arr = s.messages || []
        const la = [...arr].reverse().find(m => m.role === 'assistant' && m.content)
        if (arr.length >= priorLen && la) {
          if (mountedRef.current && sidRef.current === id) {
            setMsgs(arr)
            setPreview(firstLine(la.content).slice(0, 120)); setHasNew(view === 'bar')
          }
          return true
        }
      } catch { /* keep polling */ }
    }
    return false
  }

  // Stream one instruction to the assistant. `userText` = the bubble shown; `instruction` = what the
  // model receives (run context + attached files appended, not shown in the bubble).
  const runLLM = async (instruction, { userText = null, ensureVisible = false, context = null } = {}) => {
    if (ensureVisible && view === 'bar' && hasChat) setView('side')
    const wasBar = view === 'bar' && !ensureVisible
    setPreview(''); setHasNew(false)
    const atts = files
    let id = sid
    if (!id) {
      // Create the session FIRST; only then clear the attached-file chips — else a create failure
      // strands the user with their files already gone.
      try { const m = await assistantCreate((userText || instruction).slice(0, 60), mode); id = m.id; sidRef.current = id; try { localStorage.setItem('ll.asstSid', id) } catch { /* ignore */ } if (mountedRef.current) setSid(id) }
      catch { flash('assistant offline'); return }
    }
    setFiles([])   // committed to sending this turn -> clear the composer's attachments
    // What was silently attached to this turn (run, #experiments, files) — shown as a faint caption
    // ABOVE the user's bubble so the injected context is visible, not hidden inside the instruction.
    const ctxInfo = { run: context?.run || null, refs: context?.refs || [], files: atts.map(a => a.name) }
    const hasCtx = ctxInfo.run || ctxInfo.refs.length || ctxInfo.files.length
    atBottomRef.current = true          // sending my own message: always scroll it into view
    setMsgs(m => [...m, { role: 'user', content: userText || instruction, context: hasCtx ? ctxInfo : null },
                        { role: 'assistant', content: '', streaming: true }])
    const priorLen = msgs.length + 2
    setBusy(true); runningRef.current = true
    const ctrl = new AbortController(); abortRef.current = ctrl
    let polling = true
    // sid-guarded like every other callback: after a mid-turn session switch, a late poll result
    // must not surface the DEPARTED session's confirm-cards over the one the user switched to.
    ;(async () => { while (polling && mountedRef.current && sidRef.current === id) { try { const p = await assistantPermissions(id); if (mountedRef.current && sidRef.current === id) setPending(p.pending || []) } catch { /* transient */ } await sleep(800) } })()
    const safeSid = (fn) => (...a) => { if (mountedRef.current && sidRef.current === id) fn(...a) }
    let acc = ''
    const fullInstruction = instruction + filePreamble(atts)
    try {
      // A just-fired Stop's cancel POST may still be in flight — wait it out so it can't register
      // against (and instantly kill) THIS new turn's cancel event server-side.
      if (cancelReqRef.current) { try { await cancelReqRef.current } catch { /* done */ } }
      const res = await assistantMessageStream(id, fullInstruction, mode, {
        onToken: safeSid((tok) => { acc += tokText(tok); patchLast({ content: acc }) }),
        onText: safeSid((txt) => patchLast(prev => ({ activity: [...(prev.activity || []), { type: 'text', content: txt }] }))),
        onStep: safeSid((s) => patchLast(prev => {
          const a = prev.activity || []; const last = a[a.length - 1]
          return last && last.type === 'tools'
            ? { activity: [...a.slice(0, -1), { ...last, labels: [...last.labels, s] }] }
            : { activity: [...a, { type: 'tools', labels: [s] }] }
        })),
        onTodos: safeSid((items) => patchLast({ todos: items })),
        onError: safeSid((e) => flash(e)),
      }, ctrl.signal, userText || instruction)   // persist the CLEAN bubble, not the ctx-augmented instruction
      if (!mountedRef.current || sidRef.current !== id) return
      // Stop was pressed: aborting a fetch mid-stream does NOT throw (the reader catch swallows it and
      // returns), so we land here on the success path — but the turn is cancelled. Don't overwrite the
      // "(stopped)" bubble stop() already wrote with a "(no reply)".
      if (!runningRef.current) return
      const reply = (res && res.reply) || acc || (res && res.ok === false && res.error ? `(error: ${res.error})` : '(no reply)')
      patchLast({ content: reply, streaming: false, steps: res && res.steps, applied: res && res.applied,
                  proposals: res && res.proposals, todos: res && res.todos, tokens: res && res.tokens })
      setPreview(firstLine(reply).slice(0, 120)); setHasNew(wasBar)
    } catch (e) {
      if (!mountedRef.current || sidRef.current !== id || e.name === 'AbortError') { /* handled in finally */ }
      else if (acc) {
        // We already have partial tokens — keep them (the stream dropped mid-answer).
        patchLast({ content: acc, streaming: false })
        flash('stream interrupted — showing partial reply')
      } else {
        // No tokens arrived (proxy killed the stream at the headers). The worker is still running in
        // the background; poll the session for the persisted reply instead of failing.
        patchLast({ content: '', streaming: true, recovering: true })
        flash('reconnecting…')
        const ok = await recoverReply(id, priorLen)
        // `runningRef` guard: if the user hit Stop during recovery, keep the "(stopped)" bubble.
        if (mountedRef.current && sidRef.current === id && runningRef.current && !ok) patchLast({ content: 'Could not reach the assistant.', streaming: false, recovering: false })
      }
    } finally {   // guard shared-ref/state cleanup on still-current session (see reattach finally)
      polling = false
      if (mountedRef.current && sidRef.current === id) { runningRef.current = false; abortRef.current = null; setBusy(false); setPending([]) }
    }
  }

  const send = () => {
    const t = input.trim()
    if ((!t && files.length === 0) || busy) return
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
    const direct = parseDirect(t)
    if (direct) { setInput(''); runDirect(direct); return }
    const pr = runId ? preRoute(t) : null
    if (pr) { setInput(''); runDirect(pr); return }
    setInput('')
    const refs = runId ? refNodes(t) : []
    const safeRun = String(runId).replace(/[\]"\r\n]/g, ' ').slice(0, 200)   // can't break the preamble/stripCtx or inject
    const ctx = runId
      ? `\n\n[UI context: run "${safeRun}" is open.${refs.length ? ` The user is referring to experiment(s) ${refs.map(i => '#' + i).join(', ')} — read them with the run tools.` : ''} Use the run tools if this is about it.]`
      : ''
    runLLM((t || 'See the attached file(s).') + ctx, { userText: t || '(attached files)', context: { run: runId || null, refs } })
  }

  const stop = () => {
    runningRef.current = false                              // halt reattach/recover polling immediately
    if (abortRef.current) { try { abortRef.current.abort() } catch { /* already gone */ } abortRef.current = null }
    const id = sidRef.current || sid                        // cancel server-side (works even post-reload)
    if (id) {
      const pr = assistantCancel(id).catch(() => {})
      cancelReqRef.current = pr
      pr.finally(() => { if (cancelReqRef.current === pr) cancelReqRef.current = null })   // identity-guarded
    }
    // Reset the UI NOW — never wait on a hung LLM call or the server: end the streaming placeholder,
    // drop busy + pending, so the composer is usable again instantly.
    setBusy(false); setPending([])
    patchLast(prev => prev && prev.role === 'assistant' && prev.streaming
      ? { streaming: false, recovering: false, content: prev.content || '(stopped)' } : prev || {})
    flash('stopped')
  }

  const activeMode = MODES.find(x => x.id === mode) || MODES[0]
  // Context usage: the last turn's PROMPT tokens ≈ how much context the assistant is carrying right now
  // (grows as the chat gets longer); the sum of turn totals ≈ what the chat has spent. Shown as a faint
  // chip so you can watch the window fill and know when to start a fresh chat.
  const lastCtxTok = [...msgs].reverse().find(m => m.tokens?.prompt)?.tokens?.prompt || 0
  const chatTok = msgs.reduce((s, m) => s + (m.tokens?.total || 0), 0)
  const ktok = (n) => n >= 1000 ? (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k' : String(n || 0)
  const ctxChip = lastCtxTok > 0
    ? <span className="asst-ctxtok" title={`≈${lastCtxTok.toLocaleString()} tokens in the assistant's context (grows with the conversation — start a new chat to reset) · ≈${chatTok.toLocaleString()} total tokens this chat`}>
        <OpIcon name="sliders" size={10} /> {ktok(lastCtxTok)} ctx</span>
    : null

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

  const onKey = (e) => {
    if (e.key !== 'Enter' || e.shiftKey) return
    // Only the bar view renders the suggestion popup, so only it may hold Enter back — and an
    // exact command ("/stop") must send even there, not be swallowed by its own suggestion.
    const exact = slashMatch && suggestions.some(c => c.name === slashMatch[1].toLowerCase())
    if (view === 'bar' && suggestions.length > 0 && !exact) return
    e.preventDefault(); send()
  }

  if (hidden) return null

  // ── shared sub-renders ──────────────────────────────────────────────────────────────────────────
  const renderThread = () => <>
    {msgs.length === 0 && <div className="asst-empty">
      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
        Ask anything — inspect the code, read your runs, steer or create runs{runId ? ` · run “${runId}” is open` : ''}.
      </div>
      <div className="asst-hints">
        {HINTS.map(h => <button key={h.label} className="asst-hint"
          onClick={() => { setInput(h.text); inputRef.current?.focus() }}>{h.label}</button>)}
      </div>
    </div>}
    {msgs.map((m, i) => <React.Fragment key={i}>
      {m.role === 'user' && m.context && <div className="asst-ctx-cap" title="context attached to this message">
        {m.context.run && <span className="asst-ctx-i"><OpIcon name="folder" size={10} /> {m.context.run}</span>}
        {(m.context.refs || []).map(r => <span key={'r' + r} className="asst-ctx-i">#{r}</span>)}
        {(m.context.files || []).map(f => <span key={'f' + f} className="asst-ctx-i"><OpIcon name="clip" size={10} /> {f}</span>)}
      </div>}
      <Turn m={m} runsById={runsById} onRevert={onRevert} />
    </React.Fragment>)}
    {pending.map(req => <PermCard key={req.id} req={req} onResolve={resolvePerm} />)}
    {/* The streaming placeholder is itself in `msgs`; its Turn renders the activity timeline +
        the "thinking" indicator — no separate block here (which would double the label). */}
  </>

  const fileChips = files.length > 0 && <div className="asst-files">
    {files.map(f => <span key={f.name} className="chip xs file" title={`${(f.size / 1024).toFixed(1)} KB${f.truncated ? ' · truncated' : ''}`}>
      <OpIcon name="doc" size={11} /> {f.name}
      <button className="chip-x" onClick={() => removeFile(f.name)} title="remove">✕</button></span>)}
  </div>

  const attachBtn = (cls) => <button className={cls} title="attach text file(s)" onClick={() => fileRef.current?.click()}>
    <OpIcon name="clip" size={14} /></button>

  // mode selector row — placed BELOW the input in the side + full composers.
  const modeRow = <div className="asst-moderow">
    <div className="asst-modes">
      {MODES.map(x => <button key={x.id} className={'asst-mode' + (x.id === mode ? ' on' : '')}
        title={x.hint} onClick={() => setMode(x.id)}>{x.label}</button>)}
    </div>
    <span className="asst-modehint muted">{activeMode.hint}</span>
  </div>

  // A full composer (textarea + attach + send/stop + mode row below) — reused by side + full views.
  const composer = (placeholder) => <div className="chat-in asst-in">
    {runId && refNodes(input).length > 0 && <div className="cmdbar-ctx">
      {refNodes(input).map(id => <span key={id} className="chip xs">#{id}
        <button className="chip-x" title="detach" onClick={() => setInput(input.replace(new RegExp(`#(?:node-)?${id}\\b`, 'gi'), '').replace(/\s{2,}/g, ' ').trim())}>✕</button></span>)}
    </div>}
    {fileChips}
    <div className="asst-inrow">
      {attachBtn('asst-attach')}
      <textarea className="text" ref={inputRef} value={input}
        onChange={e => setInput(e.target.value)} onKeyDown={onKey} placeholder={placeholder} />
      {busy
        ? <button className="btn sm" title="stop" onClick={stop}>■</button>
        : <button className="btn sm primary" disabled={!input.trim() && files.length === 0} onClick={send}>Send</button>}
    </div>
    {modeRow}
  </div>

  const hiddenFileInput = <input ref={fileRef} type="file" multiple style={{ display: 'none' }}
    onChange={e => { onFiles(e.target.files); e.target.value = '' }} />

  return <>
    {hiddenFileInput}

    {/* ── bottom bar — ONLY in bar view (moves into the side panel otherwise) ── */}
    {view === 'bar' && <div className={'cmdbar-wrap'}><div className={'cmdbar-dock' + (busy ? ' thinking' : '') + (hasNew ? ' fresh' : '')}>
      <button className="cmdbar-ic" title="open the full assistant" onClick={openFull}>✦</button>
      <div className="cmdbar-field">
        {(refNodes(input).length > 0 || files.length > 0) && <div className="cmdbar-ctx">
          {runId && refNodes(input).map(id => <span key={id} className="chip xs">#{id}</span>)}
          {files.map(f => <span key={f.name} className="chip xs file"><OpIcon name="doc" size={10} /> {f.name}
            <button className="chip-x" onClick={() => removeFile(f.name)}>✕</button></span>)}
        </div>}
        {suggestions.length > 0 && <div className="cmdbar-pop">
          {suggestions.map(c => <button key={c.name} className="cmdbar-pop-item"
            onMouseDown={(e) => { e.preventDefault(); setInput(`/${c.name} `); inputRef.current?.focus() }}>
            <b>/{c.name}</b><span className="muted"> {c.desc}</span></button>)}
        </div>}
        <input className="cmdbar-in" ref={inputRef} value={input}
          onChange={e => setInput(e.target.value)} onKeyDown={onKey}
          placeholder={runId
            ? 'Command or ask…  /stop · pause · #12 to attach an experiment · or describe what to do'
            : 'Describe a run to start, or ask the assistant…  ( / for commands )'} />
      </div>
      {attachBtn('cmdbar-attach')}
      {busy
        ? <span className="cmdbar-status thinking"><span className="cmdbar-pip" /> thinking…</span>
        : preview
          ? <button className="cmdbar-status preview" title="open the conversation" onClick={openSide}>
              <span className="cmdbar-who">assistant</span> {preview}<span className="cmdbar-more"> ▸</span></button>
          : null}
      {/* send / stop share ONE slot (you can't send mid-turn) — kept separate from the side button
          so stopping never opens a view. */}
      {busy
        ? <button className="cmdbar-go stop" title="stop the assistant" onClick={stop}>■</button>
        : <button className="cmdbar-go" title="send (Enter)" disabled={!input.trim() && files.length === 0} onClick={send}>▶</button>}
      <button className="cmdbar-drawer-btn" title="open chat on the right (side view)" onClick={openSide}><OpIcon name="chat" size={13} /></button>
      {toast && <div className="cmdbar-toast">{toast}</div>}
    </div></div>}

    {/* ── right side panel (resizable) — the composer lives INSIDE it; no bottom bar while open ── */}
    {view === 'side' && <div className="asst-side-panel" style={{ width: sideW }}>
      <div className="asst-resize" onMouseDown={startResize} title="drag to resize" />
      <div className="asst-drawer-h">
        <b className="asst-drawer-ttl">Assistant</b>
        {ctxChip}
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn sm ghost" title="new chat" onClick={newChat}>＋ New</button>
        <button className="btn sm ghost" title="expand to the full view" onClick={openFull}>⤢ full</button>
        <button className="btn sm ghost" title="collapse to the bar" onClick={collapseToBar}>▾ bar</button>
      </div>
      <div className="asst-drawer-feed" ref={feedRef} onScroll={onFeedScroll}>{renderThread()}</div>
      {composer('Message the assistant…  (/ for commands · Enter to send)')}
      {toast && <div className="cmdbar-toast side">{toast}</div>}
    </div>}

    {/* ── full page — dedicated OPAQUE view (sessions · thread · composer) ── */}
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
            <button className="asst-sess-x" onClick={(e) => delSession(s.id, e)} title="delete chat">✕</button>
          </div>)}
        </div>
      </div>
      <div className="asst-main">
        <div className="asst-main-h">
          <span className="ttl" style={{ flex: 1 }}>{sessions.find(s => s.id === sid)?.title || 'New chat'}</span>
          {ctxChip}
          {sid && <button className="btn sm ghost" title="fork this chat into a new session" onClick={async () => {
            try { const c = await assistantFork(sid); await refreshSessions(); openSession(c.id) } catch (e) { flash(e.message) }
          }}>⑂ fork</button>}
          {sid && <button className="btn sm ghost" title="share a read-only link to this chat" onClick={async () => {
            try {
              const r = await assistantShare(sid)
              const url = location.origin + location.pathname + r.url
              try { await navigator.clipboard.writeText(url) } catch { /* clipboard blocked */ }
              location.hash = r.url.replace(/^#/, '')   // navigate AFTER copying (the bar hides on the shared page)
            } catch (e) { flash(e.message) }
          }}>⤴ share</button>}
          <button className="btn sm ghost" title="dock to the right" onClick={openSide}>▧ side</button>
          <button className="btn sm ghost" title="fold to the bar" onClick={collapseToBar}>▾ bar</button>
        </div>
        <div className="asst-feed" ref={feedRef} onScroll={onFeedScroll}>{renderThread()}</div>
        {composer('Message the assistant…  (/ for commands · Enter to send)')}
        {toast && <div className="cmdbar-toast side">{toast}</div>}
      </div>
    </div>}
  </>
}
