import React, { useEffect, useRef, useState } from 'react'
import { Turn, PermCard } from './AssistantChat.jsx'
import { OpIcon } from './icons.jsx'
import { usePoll } from './hooks.js'
import { getRunAccess } from './runMode.js'
import { assistantErrorInfo, assistantPreview } from './assistantErrors.js'
import {
  assistantDirectObservationKind, assistantDirectStatus, assistantRunChanged,
  assistantStorageFailureOwnsLock, pollAssistantDirectOnce,
  presentAssistantCommandResult, submitAssistantDirect,
} from './assistantCommand.js'
import {
  assistantRecoveryFailure, assistantRecoveryPayload, assistantReplyCompletesTurn, danglingAssistantTurn,
  unavailableAssistantRecovery,
} from './assistantRecovery.js'
import './assistant-polish.css'
import {
  get, fmtAgo, ASSISTANT_MODES as MODES, tokText, assistantCreate, assistantMessageStream,
  assistantCommands, assistantRevert, assistantSessions, assistantGet, assistantDelete,
  assistantPermissions, assistantResolve, assistantCancel, assistantProgress,
  assistantFork, assistantShare, commandActionForEvent, commandCanRetry, commandErrorMessage,
  commandEventForAction,
  commandFailureRecord, commandFeedback, commandRecordMatchesAction, getRunCommand, retryRunCommand,
  COMMAND_SUCCEEDED, COMMAND_FAILED,
  createIdempotencyKey, saveAssistantRunTransport, loadAssistantRunTransport,
  clearAssistantRunTransport, clearRunCommandLock, loadRunCommandLock, saveRunCommandLock,
  subscribeRunCommandLock, storageGet, storageSet, storageRemove,
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
  stop:     { success: '⏸ run stopped (frozen — not finalized)',
    noop: '⏸ run was already stopped', executing: 'Stop requested — waiting for the run to freeze' },
  finalize: { success: '⏹ run finalized (wrap-up complete)',
    noop: '⏹ run was already finalized', executing: 'Finalize requested — waiting for report and wrap-up' },
  resume:   { success: '▶ run resumed',
    noop: '▶ run was already running', executing: 'Resume requested — waiting for the engine' },
  pause:    { success: '⏸ run stopped (frozen)',
    noop: '⏸ run was already stopped', executing: 'Pause requested — waiting for the run to freeze' },
  abort:    { success: '⏹ run finalized',
    noop: '⏹ run was already finalized', executing: 'Finalize requested — waiting for wrap-up' },
  ratify:   { success: '✓ eval spec ratified',
    noop: '✓ eval spec was already ratified', executing: 'Ratification requested — waiting for confirmation' },
  approve:  { arg: true, success: (id) => `✓ approved #${id}`,
    noop: (id) => `✓ #${id} was already approved`, executing: (id) => `Approval of #${id} requested — waiting for confirmation` },
}
const UNKNOWN_DIRECT_SPEC = {
  success: 'Run command completed', noop: 'Run command was already satisfied',
  executing: 'Run command is pending',
}
const directCopy = (value, arg) => typeof value === 'function' ? value(arg) : value
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
const previewText = (value) => firstLine(assistantPreview(value)).slice(0, 120)

// Keep only an allow-listed error marker in newly rendered turns. The classifier can reconstruct the
// canonical card from this marker, while URLs, provider routing, model names and account ids never
// enter the visible reply or collapsed preview.
const normalizedFailureText = (value) => {
  const info = assistantErrorInfo(`Assistant error: ${String(value || '')}`)
  if (!info) return 'Assistant error: provider returned error'
  const status = info.status ? ` Error code: ${info.status}.` : ''
  if (info.kind === 'rate_limit') return `Assistant error: temporarily rate-limited.${status}`
  if (info.kind === 'credentials') return `Assistant error: credentials need attention.${status}`
  if (info.kind === 'unavailable') return `Assistant error: connection unavailable.${status}`
  return `Assistant error: provider returned error.${status}`
}

const safeErrorNotice = (value) => assistantErrorInfo(`Assistant error: ${String(value || '')}`)?.title || 'Assistant request failed'

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
  const [runAccess, setRunAccessState] = useState(() => getRunAccess(runId))
  const historical = !!runId && runAccess.readOnly
  const [input, setInput] = useState('')
  const [sid, setSid] = useState(null)
  const [msgs, setMsgs] = useState([])
  const [busy, setBusy] = useState(false)
  const [directPending, setDirectPending] = useState(null) // retained while an accepted direct command executes
  const [directFailure, setDirectFailure] = useState(null)
  const [runCommandLock, setRunCommandLock] = useState(() => loadRunCommandLock(runId))
  const externalCommandPending = runCommandLock?.source === 'dock' ? runCommandLock : null
  const ownStorageFailureLock = assistantStorageFailureOwnsLock(directFailure, runCommandLock)
  const commandBusy = directPending != null || (runCommandLock != null && !ownStorageFailureLock)
  const [preview, setPreview] = useState('')      // beginning of the latest reply (collapsed bar)
  const [hasNew, setHasNew] = useState(false)     // highlight the bar until a view is opened
  const [view, setView] = useState('bar')         // 'bar' | 'side' | 'full'
  const [mode, setMode] = useState('plan')
  const [toast, setToast] = useState(null)
  const [commands, setCommands] = useState([])
  const [suggestionIndex, setSuggestionIndex] = useState(0)
  const [suggestionsDismissed, setSuggestionsDismissed] = useState(false)
  const [runs, setRuns] = useState([])
  const [pending, setPending] = useState([])      // live HITL confirm requests
  const [sessions, setSessions] = useState([])    // full-view session list
  const [files, setFiles] = useState([])          // attached text files [{name,size,content,truncated}]
  const [sideW, setSideW] = useState(() => Math.min(
    Math.max(+storageGet('ll.asstW', 440) || 440, 320), window.innerWidth - 120))

  const mountedRef = useRef(true)
  const currentRunIdRef = useRef(runId)
  currentRunIdRef.current = runId
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
  const commandStatusRef = useRef(null)
  const commandFocusRequestedRef = useRef(false)
  const toastRunIdRef = useRef(runId)
  useEffect(() => {
    setRunAccessState(getRunAccess(runId))
    const onAccess = (e) => {
      if (String(e.detail?.runId) === String(runId)) setRunAccessState(getRunAccess(runId))
    }
    window.addEventListener('ll:run-access', onAccess)
    return () => window.removeEventListener('ll:run-access', onAccess)
  }, [runId])
  useEffect(() => {
    setRunCommandLock(loadRunCommandLock(runId))
    return subscribeRunCommandLock(runId, setRunCommandLock)
  }, [runId])
  useEffect(() => {
    if (assistantRunChanged(toastRunIdRef.current, runId)) {
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current)
      flashTimerRef.current = null
      setToast(null)
    }
    toastRunIdRef.current = runId
  }, [runId])
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (abortRef.current) abortRef.current.abort()
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current)
    }
  }, [])
  useEffect(() => { storageSet('ll.asstW', sideW) }, [sideW])
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
  usePoll((alive) => get('/api/runs').then(r => alive() && setRuns(r || [])).catch(() => {}), 6000, [])
  const runsById = React.useMemo(() => Object.fromEntries(runs.map(r => [r.run_id, r])), [runs])
  const refreshSessions = () => assistantSessions().then(r => setSessions(r.sessions || [])).catch(() => {})
  useEffect(() => { if (view === 'full') refreshSessions() }, [view])

  const feedOpen = view === 'side' || view === 'full'
  // Autoscroll ONLY when the user is already near the bottom — don't yank them back down while they've
  // scrolled up to read earlier turns during a streaming reply.
  const onFeedScroll = (e) => { const f = e.currentTarget; atBottomRef.current = f.scrollHeight - f.scrollTop - f.clientHeight < 80 }
  useEffect(() => { if (feedOpen && feedRef.current && atBottomRef.current) requestAnimationFrame(() => { feedRef.current.scrollTop = feedRef.current.scrollHeight }) }, [msgs, view, busy])

  const flash = (m) => { setToast(m); if (flashTimerRef.current) clearTimeout(flashTimerRef.current); flashTimerRef.current = setTimeout(() => mountedRef.current && setToast(null), 5000) }
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
  const openCommandView = (next) => {
    // The status element is replaced when the Assistant changes surface. Re-arm focus so pending and
    // failed command recovery remains announced/focused in the newly mounted side/full status node.
    if (commandBusy || directFailure) commandFocusRequestedRef.current = true
    setView(next); setHasNew(false)
  }
  const openSide = () => openCommandView('side')   // openable any time (even empty)
  const openFull = () => openCommandView('full')
  const collapseToBar = () => {
    const la = lastAssistant()
    if (la && la.content) { setPreview(previewText(la.content)); setHasNew(true) }
    if (commandBusy || directFailure) commandFocusRequestedRef.current = true
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
    storageSet('ll.asstSid', id)
    try {
      const s = await assistantGet(id)
      if (!mountedRef.current) return
      const arr = s.messages || []
      setMsgs(arr); if (s.meta?.mode) setMode(s.meta.mode)
      const la = [...arr].reverse().find(m => m.role === 'assistant' && m.content)
      if (la) setPreview(previewText(la.content))
      // Reattach to a live worker after reload/session switching. If the durable transcript instead ends
      // in a staged user turn and this process has no worker, the guarded path below replays that exact
      // raw/display/mode identity once and polls until its reply lands. The placeholder is UI-only: it
      // never appends a second user turn or creates a fresh mutation namespace.
      let prog = { active: false, steps: [] }
      let progressKnown = false
      try { prog = await assistantProgress(id); progressKnown = true } catch { /* offline: observe only */ }
      const trailingUser = arr.length && arr[arr.length - 1].role === 'user'
      const dangling = danglingAssistantTurn(arr)
      const inFlight = prog.active || !!dangling || (recover && trailingUser)
      if (inFlight && mountedRef.current && sidRef.current === id) {
        setBusy(true); runningRef.current = true
        const act = (prog.steps || []).length ? [{ type: 'tools', labels: prog.steps }] : []
        setMsgs(m => (m[m.length - 1] && m[m.length - 1].role === 'assistant' && m[m.length - 1].streaming)
          ? m : [...m, { role: 'assistant', content: '', streaming: true, activity: act,
            recoveryNeeded: !!dangling }])
        let polling = true
        let exactFailure = null
        let recoveryCtrl = null
        let exactState = 'idle'
        const startExactRecovery = async () => {
          if (!dangling || exactState !== 'idle') return
          exactState = 'checking'
          let latest
          try { latest = await assistantGet(id) } catch { exactState = 'idle'; return }
          if (!mountedRef.current || sidRef.current !== id || !runningRef.current) return
          const latestTurn = danglingAssistantTurn(latest.messages || [])
          if (!latestTurn) { exactState = 'settled'; return } // the original reply won the race
          if (latestTurn.turn_id !== dangling.turn_id) {
            exactState = 'settled'; exactFailure = unavailableAssistantRecovery; return
          }
          const recovery = assistantRecoveryPayload(latestTurn)
          if (!recovery) { exactState = 'settled'; exactFailure = unavailableAssistantRecovery; return }
          exactState = 'posted'
          recoveryCtrl = new AbortController(); abortRef.current = recoveryCtrl
          // Re-read above before POST: if the old worker persisted its reply after our first GET, this
          // path observes it instead of accidentally appending a fresh duplicate turn.
          assistantMessageStream(id, recovery.instruction, recovery.mode, {},
            recoveryCtrl.signal, recovery.display).catch(error => {
            exactFailure = assistantRecoveryFailure(error)
          })
        }
        if (progressKnown && !prog.active) startExactRecovery()
        ;(async () => {
          while (polling && runningRef.current && mountedRef.current && sidRef.current === id) {
            await sleep(1200)
            try {
              const pp = await assistantProgress(id)
              if (!pp.active) {
                // The server may have been offline for the first progress read, or an attached worker
                // may have died. Re-check the durable transcript before the one allowed recovery POST.
                if (dangling && exactState === 'idle') startExactRecovery()
                if (!dangling) break
                continue
              }
              if (mountedRef.current && sidRef.current === id && ((pp.steps || []).length || pp.text))
                patchLast(prev => prev && prev.role === 'assistant' && prev.streaming   // only the live placeholder
                  ? { ...(pp.text ? { content: pp.text } : {}),
                      activity: (pp.steps || []).length ? [{ type: 'tools', labels: pp.steps }] : prev.activity } : prev)
              // A reattached turn may be PARKED on a HITL confirm — surface its card too (the send
              // path polls permissions; without this a reload hides the card until the 900s deny).
              const perms = await assistantPermissions(id)
              if (mountedRef.current && sidRef.current === id) setPending(perms.pending || [])
            } catch { /* transient */ }
          }
        })()
        recoverReply(id, arr.length + 1, () => exactFailure).then(ok => {
          // If recovery gives up (the worker died / no reply ever lands), end the placeholder — else it
          // renders a forever "thinking" spinner (the send path has the same fallback on failure).
          if (!ok && mountedRef.current && sidRef.current === id) {
            if (exactFailure?.notice) flash(exactFailure.notice)
            patchLast(prev => prev && prev.role === 'assistant' && prev.streaming
              ? { streaming: false, recovering: false,
                  content: exactFailure?.message || prev.content
                    || normalizedFailureText('connection unreachable'),
                  recoveryNeeded: !!dangling && !exactFailure?.blocked,
                  recoveryBlocked: !!exactFailure?.blocked } : prev)
          }
        }).finally(() => {   // only reset the SHARED busy/runningRef if we're still on this session —
          // else a departing session's late finally would clobber the one the user switched TO.
          polling = false
          if (mountedRef.current && sidRef.current === id) {
            runningRef.current = false; setBusy(false)
            if (abortRef.current === recoveryCtrl) abortRef.current = null
          }
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
    last = storageGet('ll.asstSid')
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
    storageRemove('ll.asstSid')
  }
  const delSession = async (id, e) => {
    e?.stopPropagation()
    setSessions(ss => ss.filter(s => s.id !== id))   // optimistic: drop it now (geesefs list can lag)
    if (id === storageGet('ll.asstSid')) storageRemove('ll.asstSid')
    try { await assistantDelete(id); if (id === sid) newChat() } catch (e2) { flash(e2.message); refreshSessions() }
  }

  const resolvePerm = async (reqId, decision) => {
    if (historical) { flash(`History seq ${runAccess.seq} is read-only — return live to resolve actions`); return }
    setPending(p => p.filter(x => x.id !== reqId))
    try { await assistantResolve(reqId, decision) } catch (e) { flash(e.message) }
  }
  const onRevert = async (absPath) => {
    if (historical) { flash(`History seq ${runAccess.seq} is read-only — return live to undo edits`); return }
    try { const r = await assistantRevert(absPath); flash(r.result || 'reverted') } catch (e) { flash(e.message) }
  }

  const directLabels = entry => entry.spec.arg && entry.arg == null ? {
    success: 'Run command completed', noop: 'Run command was already satisfied',
    executing: 'Run command is pending', failure: `/${entry.name} failed`,
  } : {
    success: directCopy(entry.spec.success, entry.arg), noop: directCopy(entry.spec.noop, entry.arg),
    executing: directCopy(entry.spec.executing, entry.arg), failure: `/${entry.name} failed`,
  }
  const flashDirect = (entry, message) => presentAssistantCommandResult(
    currentRunIdRef.current, entry.runId, () => flash(message))
  const persistDirect = entry => saveAssistantRunTransport(entry.runId, {
    action: entry.name, arg: entry.arg, idempotencyKey: entry.idempotencyKey,
    commandId: entry.record?.id || '', record: entry.record,
    statusUnavailable: entry.statusUnavailable, observationKind: entry.observationKind,
    retrying: entry.retrying, checking: entry.checking,
  })
  const setCurrentDirect = (entry, next) => setDirectPending(current =>
    current?.runId === entry.runId && current?.idempotencyKey === entry.idempotencyKey ? next : current)
  const setCurrentFailure = (entry, next) => {
    if (String(currentRunIdRef.current ?? '') === String(entry.runId ?? '')) setDirectFailure(next)
  }
  const clearUnsentDirectRecovery = entry => {
    const expected = {
      source: 'assistant', idempotencyKey: entry.idempotencyKey,
      action: entry.name, commandId: '',
    }
    const transportCleared = clearAssistantRunTransport(entry.runId, undefined, {
      idempotencyKey: entry.idempotencyKey,
    })
    const lockCleared = clearRunCommandLock(entry.runId, expected)
    const remainingTransport = loadAssistantRunTransport(entry.runId)
    const remainingLock = loadRunCommandLock(entry.runId)
    return (transportCleared || !remainingTransport)
      && (lockCleared || !assistantStorageFailureOwnsLock(entry, remainingLock))
  }
  const localStorageFailure = entry => {
    const failure = { ...entry, record: { status: 'rejected', error: {
      code: 'command_storage_unavailable',
      message: 'The command was not sent because durable tab storage is unavailable.',
      remediation: 'Enable session storage or free browser storage, then try again.', retryable: false,
    } } }
    // No POST happened. Best-effort removal keeps this tab immediately usable; if storage refuses the
    // cleanup, exact own-lock detection above keeps this failure + Dismiss visible instead of showing
    // a fake generic pending state. The staged envelope remains quarantined and cannot auto-submit.
    clearUnsentDirectRecovery(failure)
    commandFocusRequestedRef.current = true
    setCurrentDirect(entry, null)
    setCurrentFailure(entry, failure)
    flashDirect(entry, 'Command not sent — durable recovery storage is unavailable')
    return failure
  }
  const verifiedDirectEntry = (entry, record) => {
    let name = entry.name
    if (entry.protocolInvalid || !DIRECT[name]) name = commandActionForEvent(record?.event_type)
    if (!name || !DIRECT[name] || !commandRecordMatchesAction(record, name, 'assistant')) return null
    return { ...entry, name, spec: DIRECT[name], record, protocolInvalid: false,
      canResubmit: true, retrying: false }
  }
  const protocolDirect = (entry, record = entry.record, message = 'Invalid command response') => {
    const commandId = /^cmd_[0-9a-f]{32}$/.test(String(record?.id || '')) ? String(record.id) : ''
    const next = { ...entry, record: commandId ? { id: commandId, status: 'accepted' } : { status: 'submitting' },
      statusUnavailable: true, observationKind: 'protocol', checking: false, retrying: false,
      protocolInvalid: true, canResubmit: false, lastError: message }
    saveRunCommandLock(entry.runId, {
      source: 'assistant', action: next.name || 'unknown', idempotencyKey: next.idempotencyKey,
      commandId, record: next.record, statusUnavailable: true,
    })
    if (mountedRef.current) {
      commandFocusRequestedRef.current = true
      setCurrentDirect(entry, next); setCurrentFailure(entry, null)
      flashDirect(entry, `${message} — the stored intent was not replayed`)
    }
    return next
  }
  const acceptDirectRecord = (entry, record, { announce = true } = {}) => {
    const verified = verifiedDirectEntry(entry, record)
    if (!verified) return protocolDirect(entry, record, 'Command identity does not match the requested action')
    if (entry.protocolInvalid) {
      const identity = entry.lockIdentity || {
        source: 'assistant', idempotencyKey: entry.idempotencyKey,
        action: entry.name || 'unknown', commandId: entry.record?.id || '',
      }
      clearRunCommandLock(entry.runId, identity)
    }
    const feedback = commandFeedback(record, directLabels(verified))
    if (announce) flashDirect(verified, feedback.message)
    if (feedback.kind === 'pending') {
      const next = { ...verified, statusUnavailable: false, observationKind: null,
        checking: false, retrying: false, lastError: '' }
      if (!persistDirect(next)) {
        const unavailable = { ...next, statusUnavailable: true, observationKind: 'transport',
          lastError: 'Command accepted, but its updated durable status could not be stored.' }
        saveRunCommandLock(entry.runId, { ...unavailable, source: 'assistant' })
        if (mountedRef.current) setCurrentDirect(entry, unavailable)
      } else if (mountedRef.current) setCurrentDirect(entry, next)
      if (mountedRef.current) setCurrentFailure(entry, null)
    } else if (feedback.kind === 'error') {
      const failure = { ...verified, statusUnavailable: false, checking: false, retrying: false }
      if (!persistDirect(failure)) {
        clearAssistantRunTransport(entry.runId, undefined, { idempotencyKey: entry.idempotencyKey })
      }
      if (mountedRef.current) {
        commandFocusRequestedRef.current = true
        setCurrentDirect(entry, null); setCurrentFailure(entry, failure)
      }
    } else {
      clearAssistantRunTransport(entry.runId, undefined, { idempotencyKey: entry.idempotencyKey })
      if (mountedRef.current) {
        setCurrentDirect(entry, null); setCurrentFailure(entry, null)
        if (String(currentRunIdRef.current ?? '') === String(entry.runId ?? '')) {
          requestAnimationFrame(() => inputRef.current?.focus())
        }
      }
    }
    return feedback
  }
  const unavailableDirect = (entry, error, record = entry.record) => {
    let recoveryRecord = record || { status: 'submitting' }
    if (recoveryRecord.id && !recoveryRecord.event_type && DIRECT[entry.name]) {
      recoveryRecord = { ...recoveryRecord, event_type: commandEventForAction(entry.name, 'assistant') }
    }
    const next = { ...entry, record: recoveryRecord, statusUnavailable: true,
      observationKind: assistantDirectObservationKind(error), checking: false,
      lastError: error?.message || String(error) }
    if (!persistDirect(next)) {
      saveRunCommandLock(entry.runId, { ...next, source: 'assistant' })
    }
    if (mountedRef.current) { setCurrentDirect(entry, next); setCurrentFailure(entry, null) }
    return next
  }
  const failDirectObservation = (entry, error) => {
    const record = commandFailureRecord(error, entry.record)
    const failure = { ...entry, record, statusUnavailable: false, checking: false, retrying: false }
    if (!persistDirect(failure)) {
      clearAssistantRunTransport(entry.runId, undefined, { idempotencyKey: entry.idempotencyKey })
    }
    if (mountedRef.current) {
      commandFocusRequestedRef.current = true
      setCurrentDirect(entry, null); setCurrentFailure(entry, failure)
    }
    flashDirect(entry, commandFeedback(record, directLabels(entry)).message)
  }
  const executeDirect = async (entry, { recovery = false } = {}) => {
    const submitting = { ...entry, record: { status: 'submitting' }, statusUnavailable: false,
      observationKind: null, checking: false, retrying: false, lastError: '' }
    if (!persistDirect(submitting)) { localStorageFailure(entry); return }
    if (mountedRef.current) { setCurrentDirect(entry, submitting); setCurrentFailure(entry, null) }
    try {
      const record = await submitAssistantDirect(entry.runId, entry.name, entry.arg,
        entry.idempotencyKey, {
          onRecord: next => {
            const verified = verifiedDirectEntry(entry, next)
            if (verified) persistDirect({ ...verified, statusUnavailable: false })
          },
        })
      acceptDirectRecord(entry, record, { announce: !recovery || COMMAND_SUCCEEDED.has(record.status) || COMMAND_FAILED.has(record.status) })
    } catch (error) {
      const record = error?.commandRecord || (error?.commandId
        ? { id: error.commandId, status: 'accepted' } : null)
      const kind = assistantDirectObservationKind(error)
      if (error?.commandUnknown || error?.submissionMayHaveSucceeded
          || (record?.id && ['transport', 'access', 'protocol'].includes(kind))) {
        unavailableDirect(entry, error, record)
        flashDirect(entry, `/${entry.name}: command status unavailable — the same intent was preserved`)
      } else {
        // A cross-action 409 may reference another command. It is remediation context only, never the
        // durable id of this requested action.
        clearAssistantRunTransport(entry.runId, undefined, { idempotencyKey: entry.idempotencyKey })
        if (mountedRef.current) setCurrentDirect(entry, null)
        const conflict = error?.existingCommandId ? ` (active command ${String(error.existingCommandId).slice(0, 12)}…)` : ''
        const failure = { ...entry, record: commandFailureRecord(error), retrying: false }
        if (mountedRef.current) setCurrentFailure(entry, failure)
        flashDirect(entry, `/${entry.name} failed: ${error?.message || error}${conflict}`)
      }
    }
  }
  const runDirect = (d) => {
    if (!runId) { flash(`/${d.name} needs an open run`); return }
    if (historical) { flash(`History seq ${runAccess.seq} is read-only — return live to act`); return }
    // Read synchronously as well as using React state: two rapid clicks in one batch must not create
    // competing intents before the lock event has caused a render.
    if (loadRunCommandLock(runId) || (loadAssistantRunTransport(runId) && !directFailure)) {
      flash('A stored run command must be recovered before starting another action'); return
    }
    if (directFailure) clearAssistantRunTransport(directFailure.runId, undefined, {
      idempotencyKey: directFailure.idempotencyKey,
    })
    const entry = { ...d, runId, idempotencyKey: createIdempotencyKey(), record: { status: 'submitting' } }
    commandFocusRequestedRef.current = true
    setCurrentFailure(entry, null)
    setDirectPending(entry)
    executeDirect(entry)
  }
  const checkDirect = async () => {
    const entry = directPending
    if (!entry || entry.checking) return
    commandFocusRequestedRef.current = true
    const checking = { ...entry, checking: true }
    if (!entry.protocolInvalid) persistDirect(checking)
    setDirectPending(checking)
    if (!entry.record?.id) {
      if (entry.canResubmit === false || entry.protocolInvalid) {
        const next = { ...entry, checking: false, statusUnavailable: true, observationKind: 'protocol' }
        setDirectPending(next)
        flashDirect(entry, 'Stored command identity is invalid and cannot be safely replayed; dismiss it to continue')
        return
      }
      // The POST response was lost. Re-submit only the stored key + allow-listed deterministic intent;
      // this is recovery of the same logical command, never a new submit.
      await executeDirect(entry, { recovery: true })
      return
    }
    try {
      const record = await getRunCommand(entry.runId, entry.record.id)
      acceptDirectRecord(entry, record)
    } catch (error) {
      const kind = assistantDirectObservationKind(error)
      if (entry.protocolInvalid) {
        protocolDirect(entry, entry.record, error?.message || 'Stored command could not be verified')
      } else if (kind === 'transport' || kind === 'access' || kind === 'protocol') {
        unavailableDirect(entry, error, entry.record)
      } else failDirectObservation(entry, error)
    }
  }
  const retryDirect = async () => {
    const failure = directFailure
    if (!failure || directPending || !commandCanRetry(failure.record)) return
    if (loadRunCommandLock(failure.runId)) {
      flashDirect(failure, 'Another run command is pending; wait before retrying this command')
      return
    }
    const retrying = { ...failure, retrying: true, checking: false, statusUnavailable: false }
    if (!persistDirect(retrying)) {
      flashDirect(failure, 'Retry not sent — durable recovery storage is unavailable')
      return
    }
    commandFocusRequestedRef.current = true
    setCurrentFailure(failure, null); setDirectPending(retrying)
    try {
      const record = await retryRunCommand(failure.runId, failure.record.id, {
        waitMs: 0,
        onRecord: next => {
          const verified = verifiedDirectEntry(failure, next)
          if (verified) persistDirect({ ...verified, retrying: true })
        },
      })
      acceptDirectRecord(failure, record)
    } catch (error) {
      const kind = assistantDirectObservationKind(error)
      if (['transport', 'access', 'protocol'].includes(kind)) {
        unavailableDirect(failure, error, error?.commandRecord || failure.record)
      } else {
        const restored = { ...failure, retrying: false }
        if (!persistDirect(restored)) {
          clearAssistantRunTransport(failure.runId, undefined, { idempotencyKey: failure.idempotencyKey })
        }
        if (mountedRef.current && String(currentRunIdRef.current ?? '') === String(failure.runId ?? '')) {
          setDirectPending(null); setDirectFailure(restored)
        }
        const conflict = error?.existingCommandId
          ? ` Active command: ${String(error.existingCommandId).slice(0, 12)}…` : ''
        flashDirect(failure, `Retry failed: ${error?.message || error}.${conflict}`)
      }
    }
  }
  const dismissDirectFailure = () => {
    const failure = directFailure
    if (!failure) return
    if (failure.record?.error?.code === 'command_storage_unavailable') {
      if (!clearUnsentDirectRecovery(failure)) {
        flashDirect(failure, 'Recovery storage is still unavailable; the unsent command remains quarantined')
        return
      }
    } else {
      clearAssistantRunTransport(failure.runId, undefined, { idempotencyKey: failure.idempotencyKey })
    }
    setDirectFailure(null)
    requestAnimationFrame(() => inputRef.current?.focus())
  }
  const dismissProtocolDirect = () => {
    const pending = directPending
    if (!pending?.protocolInvalid) return
    clearAssistantRunTransport(pending.runId, undefined, { idempotencyKey: pending.idempotencyKey })
    const identity = pending.lockIdentity || {
      source: 'assistant', idempotencyKey: pending.idempotencyKey,
      action: pending.name || 'unknown', commandId: pending.record?.id || '',
    }
    clearRunCommandLock(pending.runId, identity)
    setDirectPending(null)
    requestAnimationFrame(() => inputRef.current?.focus())
  }
  useEffect(() => {
    const saved = loadAssistantRunTransport(runId)
    const lock = loadRunCommandLock(runId)
    setDirectPending(null); setDirectFailure(null)
    if (!saved) {
      if (lock?.source === 'assistant') clearRunCommandLock(runId, {
        source: 'assistant', idempotencyKey: lock.idempotencyKey,
        action: lock.action, commandId: lock.commandId,
      })
      return
    }
    if (lock && lock.source !== 'assistant') {
      clearAssistantRunTransport(runId, undefined, { idempotencyKey: saved.idempotencyKey })
      return
    }
    const spec = DIRECT[saved.action] || UNKNOWN_DIRECT_SPEC
    const lockMismatch = lock?.source === 'assistant' && (
      lock.idempotencyKey !== saved.idempotencyKey || lock.action !== saved.action
      || (lock.commandId && saved.commandId && lock.commandId !== saved.commandId)
    )
    if (saved.protocolInvalid || lockMismatch) {
      const restored = { name: saved.action, spec, arg: saved.arg, runId,
        idempotencyKey: saved.idempotencyKey, record: saved.record,
        statusUnavailable: true, observationKind: 'protocol', checking: false, retrying: false,
        protocolInvalid: true, canResubmit: false, lockIdentity: lock?.source === 'assistant' ? lock : null }
      saveRunCommandLock(runId, { ...restored, source: 'assistant', action: restored.name })
      setDirectPending(restored)
      return
    }
    if (COMMAND_SUCCEEDED.has(saved.record?.status)) {
      clearAssistantRunTransport(runId, undefined, { idempotencyKey: saved.idempotencyKey })
      return
    }
    const restored = { name: saved.action, spec, arg: saved.arg, runId,
      idempotencyKey: saved.idempotencyKey,
      record: saved.record || (saved.commandId ? { id: saved.commandId, status: 'accepted' } : { status: 'submitting' }),
      statusUnavailable: !!saved.statusUnavailable, observationKind: saved.observationKind,
      checking: false, retrying: !!saved.retrying, lastError: '' }
    if (COMMAND_FAILED.has(saved.record?.status) && !saved.statusUnavailable
        && !saved.retrying && !saved.checking) {
      clearRunCommandLock(runId, {
        source: 'assistant', idempotencyKey: saved.idempotencyKey,
        action: saved.action, commandId: saved.commandId,
      })
      setDirectFailure(restored)
      return
    }
    if (saved.retrying || saved.checking) {
      const uncertain = { ...restored, retrying: false, statusUnavailable: true,
        observationKind: 'transport', lastError: 'Recovery was interrupted; check the same command.' }
      persistDirect(uncertain); setDirectPending(uncertain)
      return
    }
    if (!persistDirect(restored)) { protocolDirect(restored, restored.record, 'Stored command envelope is invalid'); return }
    setDirectPending(restored)
    if (!restored.record?.id) executeDirect(restored, { recovery: true })
  }, [runId])
  useEffect(() => {
    const entry = directPending
    const command = entry?.record
    if (entry?.statusUnavailable || entry?.checking || !command?.id
        || (command.status !== 'accepted' && command.status !== 'executing')) return
    let active = true, timer = null, transientFailures = 0
    const schedule = delay => { if (active) timer = setTimeout(poll, delay) }
    const poll = async () => {
      try {
        const record = await pollAssistantDirectOnce(entry.runId, command)
        if (!active) return
        transientFailures = 0
        const terminal = COMMAND_SUCCEEDED.has(record?.status) || COMMAND_FAILED.has(record?.status)
        const feedback = acceptDirectRecord(entry, record, { announce: terminal })
        if (feedback?.terminal || !verifiedDirectEntry(entry, record)) return
        schedule(1500)
      } catch (error) {
        if (!active) return
        const kind = assistantDirectObservationKind(error)
        if (kind === 'transport') {
          if (++transientFailures < 3) {
            schedule(Math.max(Number(error.retryAfterMs) || 0, Math.min(6000, 750 * (2 ** transientFailures))))
            return
          }
          unavailableDirect(entry, error, command); return
        }
        if (kind === 'access' || kind === 'protocol') unavailableDirect(entry, error, command)
        else failDirectObservation(entry, error)
      }
    }
    timer = setTimeout(poll, 1000)
    return () => { active = false; clearTimeout(timer) }
  }, [runId, directPending?.record?.id, directPending?.statusUnavailable, directPending?.checking])

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
  const recoverReply = async (id, priorLen, authoritativeFailure = null) => {
    for (let i = 0; i < 180 && runningRef.current && mountedRef.current && sidRef.current === id; i++) {   // ~6min > the 300s turn budget
      await sleep(2000)
      if (authoritativeFailure?.()) return false
      try {
        const s = await assistantGet(id)
        const arr = s.messages || []
        const la = [...arr].reverse().find(m => m.role === 'assistant' && m.content)
        if (arr.length >= priorLen && la) {
          if (mountedRef.current && sidRef.current === id) {
            setMsgs(arr)
            setPreview(previewText(la.content)); setHasNew(view === 'bar')
          }
          return true
        }
      } catch { /* keep polling */ }
    }
    return false
  }

  // Stream one instruction to the assistant. `userText` = the bubble shown; `instruction` = what the
  // model receives (run context + attached files appended, not shown in the bubble).
  const runLLM = async (instruction, { userText = null, ensureVisible = false, context = null,
    retryFiles = null, turnMode = null } = {}) => {
    if (ensureVisible && view === 'bar' && hasChat) setView('side')
    const wasBar = view === 'bar' && !ensureVisible
    setPreview(''); setHasNew(false)
    const atts = retryFiles || files
    const effectiveMode = turnMode || mode
    let id = sid
    if (!id) {
      // Create the session FIRST; only then clear the attached-file chips — else a create failure
      // strands the user with their files already gone.
      try { const m = await assistantCreate((userText || instruction).slice(0, 60), effectiveMode); id = m.id; sidRef.current = id; storageSet('ll.asstSid', id); if (mountedRef.current) setSid(id) }
      catch { flash('assistant offline'); return }
    }
    if (!retryFiles) setFiles([])   // a retry reuses its own snapshot; do not clear newly composed files
    // What was silently attached to this turn (run, #experiments, files) — shown as a faint caption
    // ABOVE the user's bubble so the injected context is visible, not hidden inside the instruction.
    const ctxInfo = { run: context?.run || null, refs: context?.refs || [], files: atts.map(a => a.name) }
    const hasCtx = ctxInfo.run || ctxInfo.refs.length || ctxInfo.files.length
    const fullInstruction = instruction + filePreamble(atts)
    atBottomRef.current = true          // sending my own message: always scroll it into view
    setMsgs(m => [...m, { role: 'user', content: userText || instruction, context: hasCtx ? ctxInfo : null,
                          retryPayload: { instruction, raw: fullInstruction.trim(),
                            userText: userText || instruction, context, files: atts,
                            mode: effectiveMode, historyLength: m.length } },
                        { role: 'assistant', content: '', streaming: true }])
    const priorLen = msgs.length + 2
    setBusy(true); runningRef.current = true
    const ctrl = new AbortController(); abortRef.current = ctrl
    let polling = true
    // sid-guarded like every other callback: after a mid-turn session switch, a late poll result
    // must not surface the DEPARTED session's confirm-cards over the one the user switched to.
    ;(async () => { while (polling && mountedRef.current && sidRef.current === id) { try { const p = await assistantPermissions(id); if (mountedRef.current && sidRef.current === id) setPending(p.pending || []) } catch { /* transient */ } await sleep(800) } })()
    let acc = ''
    let streamedFailure = ''
    // Concurrent PROGRESS poll — the SSE fallback. Behind a buffering proxy (jupyter-server-proxy /
    // nginx) the token/text/step SSE events arrive batched only at the END, leaving a dead "thinking"
    // bubble the whole time. So ALSO poll /progress: while the real SSE tokens haven't arrived yet
    // (acc still shorter than the server's mirrored answer-so-far), surface that live text + tool steps.
    // Once tokens actually flow, acc overtakes it and the authoritative SSE content wins — this only
    // fills the buffered gap, never fights a working stream.
    ;(async () => {
      while (polling && runningRef.current && mountedRef.current && sidRef.current === id) {
        await sleep(1000)
        try {
          const pp = await assistantProgress(id)
          if (!pp || !pp.active) continue
          if (mountedRef.current && sidRef.current === id && acc.length < (pp.text || '').length)
            patchLast(prev => (prev && prev.role === 'assistant' && prev.streaming)
              ? { content: assistantErrorInfo(pp.text) ? normalizedFailureText(pp.text) : (pp.text || prev.content),
                  activity: (pp.steps || []).length ? [{ type: 'tools', labels: pp.steps }] : prev.activity }
              : prev)
        } catch { /* transient */ }
      }
    })()
    const safeSid = (fn) => (...a) => { if (mountedRef.current && sidRef.current === id) fn(...a) }
    try {
      // A just-fired Stop's cancel POST may still be in flight — wait it out so it can't register
      // against (and instantly kill) THIS new turn's cancel event server-side.
      if (cancelReqRef.current) { try { await cancelReqRef.current } catch { /* done */ } }
      const res = await assistantMessageStream(id, fullInstruction, effectiveMode, {
        onToken: safeSid((tok) => {
          acc += tokText(tok)
          patchLast({ content: assistantErrorInfo(acc) ? normalizedFailureText(acc) : acc })
        }),
        onText: safeSid((txt) => patchLast(prev => ({ activity: [...(prev.activity || []), { type: 'text', content: txt }] }))),
        onStep: safeSid((s) => patchLast(prev => {
          const a = prev.activity || []; const last = a[a.length - 1]
          return last && last.type === 'tools'
            ? { activity: [...a.slice(0, -1), { ...last, labels: [...last.labels, s] }] }
            : { activity: [...a, { type: 'tools', labels: [s] }] }
        })),
        onTodos: safeSid((items) => patchLast({ todos: items })),
        onError: safeSid((e) => {
          streamedFailure = normalizedFailureText(e)
          patchLast({ content: streamedFailure })
          flash(safeErrorNotice(e))
        }),
      }, ctrl.signal, userText || instruction)   // persist the CLEAN bubble, not the ctx-augmented instruction
      if (!mountedRef.current || sidRef.current !== id) return
      // Stop was pressed: aborting a fetch mid-stream does NOT throw (the reader catch swallows it and
      // returns), so we land here on the success path — but the turn is cancelled. Don't overwrite the
      // "(stopped)" bubble stop() already wrote with a "(no reply)".
      if (!runningRef.current) return
      const rawReply = streamedFailure || (res && res.reply) || acc || (res && res.ok === false && res.error ? `Assistant error: ${res.error}` : '(no reply)')
      const reply = assistantErrorInfo(rawReply) ? normalizedFailureText(rawReply) : rawReply
      patchLast({ content: reply, streaming: false, steps: res && res.steps, applied: res && res.applied,
                  proposals: res && res.proposals, todos: res && res.todos, tokens: res && res.tokens,
                  error_kind: res && res.error_kind })
      setPreview(previewText(reply)); setHasNew(wasBar)
    } catch (e) {
      if (!mountedRef.current || sidRef.current !== id || e.name === 'AbortError') { /* handled in finally */ }
      else if (streamedFailure) {
        patchLast({ content: streamedFailure, streaming: false })
      }
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
        if (mountedRef.current && sidRef.current === id && runningRef.current && !ok) patchLast({
          content: normalizedFailureText('connection unreachable'), streaming: false, recovering: false,
          recoveryNeeded: true,
        })
      }
    } finally {   // guard shared-ref/state cleanup on still-current session (see reattach finally)
      polling = false
      if (mountedRef.current && sidRef.current === id) { runningRef.current = false; abortRef.current = null; setBusy(false); setPending([]) }
    }
  }

  const requestNewRun = (goal = '') => runLLM(
    goal ? `Plan a new run for this goal and show me a launch card to start it: ${goal}`
         : 'I want to start a new run. Propose a run spec (name, task, key settings) as a launch card I can start; ask me for anything you need first.',
    { userText: goal ? `/new ${goal}` : '/new', ensureVisible: true })

  const retryTurn = async (assistantIndex) => {
    if (busy || commandBusy) { flash(commandBusy ? 'A run command is pending' : 'Assistant is busy'); return }
    if (historical) { flash(`Assistant is paused for history seq ${runAccess.seq} — return live to retry`); return }
    const failedTurn = msgs[assistantIndex]
    const prior = [...msgs.slice(0, assistantIndex)].reverse().find(m => m.role === 'user')
    if (!prior) { flash('The original message is no longer available'); return }
    if (failedTurn?.recoveryBlocked) {
      flash('This saved turn cannot be retried safely; start a new chat')
      return
    }
    if (failedTurn?.recoveryNeeded) {
      const id = sidRef.current || sid
      if (!id) { flash('The saved Assistant session is no longer available'); return }
      try {
        // Refresh first: if the original POST was staged, openSession performs exact raw/mode recovery;
        // if its reply landed late, simply surface it. Only a genuinely unstaged request falls through
        // to the ordinary new-turn retry below.
        const session = await assistantGet(id)
        if (!mountedRef.current || sidRef.current !== id) return
        const durableMessages = session.messages || []
        if (danglingAssistantTurn(durableMessages)) {
          await openSession(id, { recover: true })
          return
        }
        if (assistantReplyCompletesTurn(durableMessages, prior)) {
          setMsgs(durableMessages)
          const latest = [...durableMessages].reverse().find(m => m.role === 'assistant' && m.content)
          if (latest) { setPreview(previewText(latest.content)); setHasNew(view === 'bar') }
          return
        }
      } catch (error) {
        flash(error?.status === 404 ? 'This Assistant session no longer exists' : 'Could not check the saved Assistant turn')
        return
      }
    }
    if (prior.turn_id) {
      // A completed turn loaded from disk has no in-memory file objects/retryPayload, but it does retain
      // the canonical raw instruction and mode. Retrying is a NEW logical turn here, while its intent is
      // still exact: never rebuild a clean-content/current-mode approximation.
      const persisted = assistantRecoveryPayload(prior)
      if (!persisted) { flash('The saved Assistant turn cannot be retried safely'); return }
      runLLM(persisted.instruction, { userText: persisted.display, ensureVisible: true,
        turnMode: persisted.mode })
      return
    }
    if (prior.retryPayload) {
      const payload = prior.retryPayload
      runLLM(payload.instruction, { userText: payload.userText, ensureVisible: true,
        context: payload.context || null, retryFiles: payload.files || [], turnMode: payload.mode || null })
      return
    }
    if (prior.context?.files?.length) {
      flash(`Reattach ${prior.context.files.join(', ')} before retrying this turn`)
      return
    }
    const userText = String(prior.content || '').replace(/\n*\[UI context:[^\]]*\]\s*$/, '').trim()
    if (!userText) { flash('The original message is empty'); return }
    const contextRun = prior.context?.run || null
    const refs = Array.isArray(prior.context?.refs) ? prior.context.refs : []
    const safeRun = String(contextRun || '').replace(/[\]"\r\n]/g, ' ').slice(0, 200)
    const ctx = contextRun
      ? `\n\n[UI context: run "${safeRun}" is open.${refs.length ? ` The user is referring to experiment(s) ${refs.map(i => '#' + i).join(', ')} — read them with the run tools.` : ''} Use the run tools if this is about it.]`
      : ''
    runLLM(userText + ctx, { userText, ensureVisible: true, context: prior.context || null })
  }

  const openAssistantSettings = () => {
    setView('bar')
    setHasNew(false)
    location.hash = '#/settings'
  }

  const send = () => {
    if (historical) { flash(`Assistant is paused for history seq ${runAccess.seq} — return live to use run context`); return }
    const t = input.trim()
    const storedCommand = !!runId && (!!loadRunCommandLock(runId)
      || (!!loadAssistantRunTransport(runId) && !directFailure))
    if ((!t && files.length === 0) || busy || commandBusy || storedCommand) {
      if (storedCommand) flash('Recover the stored run command before continuing')
      return
    }
    const mNew = /^\/(new|genesis|run)\b\s*([\s\S]*)$/i.exec(t)
    if (mNew) {
      setInput('')
      const goal = mNew[2].trim()
      requestNewRun(goal)
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
  useEffect(() => {
    const onNewRun = (event) => {
      if (busy || commandBusy || historical) { flash(historical ? 'Return live before starting a run from this context' : commandBusy ? 'A run command is pending' : 'Assistant is busy'); return }
      setInput('')
      requestNewRun(String(event.detail?.goal || '').trim())
    }
    window.addEventListener('ll:new-run', onNewRun)
    return () => window.removeEventListener('ll:new-run', onNewRun)
  }, [busy, commandBusy, historical])

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
  useEffect(() => {
    if (historical && runningRef.current) stop()
  }, [historical])

  const activeMode = MODES.find(x => x.id === mode) || MODES[0]
  const pendingCommandText = directPending
    ? assistantDirectStatus(directPending)
    : externalCommandPending ? `/${externalCommandPending.action} is pending in the run timeline` : ''
  const canCheckDirect = !!directPending?.statusUnavailable && !directPending?.checking
  const canRetryDirect = !commandBusy && commandCanRetry(directFailure?.record)
  const showDirectFailure = !!directFailure && !commandBusy
  const directFailureText = directFailure
    ? `/${directFailure.name} failed: ${commandErrorMessage(directFailure.record)}` : ''
  const directNeedsAlert = directPending?.observationKind === 'access' || showDirectFailure
  // Suppress an A-scoped toast during the very first render for B; the effect below then clears its
  // timer/state. This avoids a one-frame stale announcement between route commit and useEffect.
  const visibleToast = assistantRunChanged(toastRunIdRef.current, runId) ? null : toast
  useEffect(() => {
    if (!commandFocusRequestedRef.current || (!directPending && !directFailure && !externalCommandPending)) return
    requestAnimationFrame(() => {
      if (!commandStatusRef.current) return
      commandFocusRequestedRef.current = false
      commandStatusRef.current.focus()
    })
  }, [directPending?.record?.id, directPending?.record?.status, directPending?.statusUnavailable,
    directPending?.checking, directPending?.retrying,
    directFailure?.record?.id, directFailure?.record?.status, busy, view])
  // Context usage: the last turn's CONTEXT (its peak single prompt) ≈ how much the assistant is carrying
  // right now (grows as the chat gets longer) — NOT tokens.prompt, which SUMS the same context re-sent by
  // every tool-loop call in the turn (billed, O(calls²)). The sum of turn totals ≈ what the chat spent
  // (billed). Shown as a faint chip so you can watch the window fill and know when to start a fresh chat.
  const lastCtx = [...msgs].reverse().find(m => m.tokens?.context || m.tokens?.prompt)?.tokens || {}
  const lastCtxTok = lastCtx.context || lastCtx.prompt || 0
  const chatTok = msgs.reduce((s, m) => s + (m.tokens?.total || 0), 0)
  const ktok = (n) => n >= 1000 ? (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k' : String(n || 0)
  const ctxChip = lastCtxTok > 0
    ? <span className="asst-ctxtok" title={`≈${lastCtxTok.toLocaleString()} tokens in the assistant's context (grows with the conversation — start a new chat to reset) · ≈${chatTok.toLocaleString()} total tokens this chat`}>
        <OpIcon name="sliders" size={10} /> {ktok(lastCtxTok)} ctx</span>
    : null

  const slashMatch = /^\/(\w*)$/.exec(input)
  const directNames = [
    { name: 'new', desc: 'plan & start a run — in this chat' },
    ...(runId ? Object.keys(DIRECT).map(n => ({ name: n, desc: 'run control · no LLM' })) : []),
  ]
  const suggestions = slashMatch
    ? [...directNames, ...commands.map(c => ({ name: c.name, desc: c.desc }))]
        .filter(c => c.name.startsWith(slashMatch[1].toLowerCase()))
        .filter((c, i, a) => a.findIndex(x => x.name === c.name) === i).slice(0, 6)
    : []
  const showSuggestions = view === 'bar' && !historical && !suggestionsDismissed && suggestions.length > 0
  const activeSuggestionIndex = showSuggestions ? Math.min(suggestionIndex, suggestions.length - 1) : -1
  const chooseSuggestion = (index = activeSuggestionIndex) => {
    const choice = suggestions[index]
    if (!choice) return
    setInput(`/${choice.name} `)
    setSuggestionsDismissed(true)
    setSuggestionIndex(0)
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  const onKey = (e) => {
    if (showSuggestions && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault()
      const delta = e.key === 'ArrowDown' ? 1 : -1
      setSuggestionIndex(i => (Math.min(i, suggestions.length - 1) + delta + suggestions.length) % suggestions.length)
      return
    }
    if (showSuggestions && e.key === 'Escape') {
      e.preventDefault()
      setSuggestionsDismissed(true)
      setSuggestionIndex(0)
      return
    }
    if (e.key !== 'Enter' || e.shiftKey) return
    // An exact command ("/stop") executes immediately. A partial command accepts the keyboard-active
    // option and leaves a trailing space for its argument, matching pointer selection.
    const exact = slashMatch && suggestions[activeSuggestionIndex]?.name === slashMatch[1].toLowerCase()
    if (showSuggestions && !exact) { e.preventDefault(); chooseSuggestion(); return }
    e.preventDefault(); send()
  }

  if (hidden) return null

  // ── shared sub-renders ──────────────────────────────────────────────────────────────────────────
  const retryHandlerFor = (assistantIndex) => {
    if (historical) return null
    if (msgs[assistantIndex]?.recoveryBlocked) return null
    const prior = [...msgs.slice(0, assistantIndex)].reverse().find(x => x.role === 'user')
    if (prior?.context?.files?.length && !prior.retryPayload) return null
    return () => retryTurn(assistantIndex)
  }
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
      <Turn m={m} runsById={runsById} readOnly={historical} onRevert={historical ? null : onRevert}
        onRetry={retryHandlerFor(i)}
        onOpenSettings={openAssistantSettings} />
    </React.Fragment>)}
    {!historical && pending.map(req => <PermCard key={req.id} req={req} onResolve={resolvePerm} />)}
    {/* The streaming placeholder is itself in `msgs`; its Turn renders the activity timeline +
        the "thinking" indicator — no separate block here (which would double the label). */}
  </>

  const fileChips = files.length > 0 && <div className="asst-files">
    {files.map(f => <span key={f.name} className="chip xs file" title={`${(f.size / 1024).toFixed(1)} KB${f.truncated ? ' · truncated' : ''}`}>
      <OpIcon name="doc" size={11} /> {f.name}
      <button className="chip-x" onClick={() => removeFile(f.name)} title="remove">✕</button></span>)}
  </div>

  const attachBtn = (cls) => <button className={cls} title={historical ? 'Unavailable while viewing history' : 'attach text file(s)'}
    disabled={historical} onClick={() => fileRef.current?.click()}>
    <OpIcon name="clip" size={14} /></button>

  // mode selector row — placed BELOW the input in the side + full composers.
  const modeRow = <div className="asst-moderow">
    <div className="asst-modes">
      {MODES.map(x => <button key={x.id} className={'asst-mode' + (x.id === mode ? ' on' : '')}
        disabled={historical} title={historical ? 'Return live to use run context' : x.hint}
        onClick={() => setMode(x.id)}>{x.label}</button>)}
    </div>
    <span className="asst-modehint muted">{activeMode.hint}</span>
  </div>

  // A full composer (textarea + attach + send/stop + mode row below) — reused by side + full views.
  const composer = (placeholder) => <div className="chat-in asst-in">
    {historical && <div className="assistant-history-lock">History seq {runAccess.seq} · Assistant paused. Return live to ask about or change this run.</div>}
    {(commandBusy || directFailure) && <div ref={commandStatusRef} tabIndex={-1}
      className={'assistant-command-pending' + (showDirectFailure ? ' error' : '')}
      role={directNeedsAlert ? 'alert' : 'status'} aria-live={directNeedsAlert ? 'assertive' : 'polite'} aria-atomic="true">
      <span>{showDirectFailure ? directFailureText : pendingCommandText}</span>
      {canCheckDirect && <button className="btn sm" onClick={checkDirect}>Check same command</button>}
      {directPending?.protocolInvalid && <button className="btn sm ghost" onClick={dismissProtocolDirect}>Dismiss</button>}
      {canRetryDirect && <button className="btn sm" onClick={retryDirect}>Retry same command</button>}
      {showDirectFailure && <button className="btn sm ghost" onClick={dismissDirectFailure}>Dismiss</button>}
    </div>}
    {runId && refNodes(input).length > 0 && <div className="cmdbar-ctx">
      {refNodes(input).map(id => <span key={id} className="chip xs">#{id}
        <button className="chip-x" title="detach" onClick={() => setInput(input.replace(new RegExp(`#(?:node-)?${id}\\b`, 'gi'), '').replace(/\s{2,}/g, ' ').trim())}>✕</button></span>)}
    </div>}
    {fileChips}
    <div className="asst-inrow">
      {attachBtn('asst-attach')}
      <textarea className="text" ref={inputRef} value={input}
        disabled={historical || commandBusy} onChange={e => { setInput(e.target.value); setSuggestionsDismissed(false); setSuggestionIndex(0) }} onKeyDown={onKey}
        placeholder={historical ? `History seq ${runAccess.seq} is read-only` : placeholder} />
      {busy
        ? <button className="btn sm" title="stop" onClick={stop}>■</button>
        : <button className="btn sm primary" disabled={historical || commandBusy || (!input.trim() && files.length === 0)} onClick={send}>{commandBusy ? 'Waiting…' : 'Send'}</button>}
    </div>
    {modeRow}
  </div>

  const hiddenFileInput = <input ref={fileRef} type="file" multiple style={{ display: 'none' }}
    onChange={e => { onFiles(e.target.files); e.target.value = '' }} />

  return <>
    {hiddenFileInput}

    {/* ── bottom bar — ONLY in bar view (moves into the side panel otherwise) ── */}
    {view === 'bar' && <div className={'cmdbar-wrap'}><div className={'cmdbar-dock' + (busy || commandBusy ? ' thinking' : '') + (hasNew ? ' fresh' : '')}>
      <button className="cmdbar-ic" title="open the full assistant" onClick={openFull}>✦</button>
      <div className="cmdbar-field">
        {(refNodes(input).length > 0 || files.length > 0) && <div className="cmdbar-ctx">
          {runId && refNodes(input).map(id => <span key={id} className="chip xs">#{id}</span>)}
          {files.map(f => <span key={f.name} className="chip xs file"><OpIcon name="doc" size={10} /> {f.name}
            <button className="chip-x" onClick={() => removeFile(f.name)}>✕</button></span>)}
        </div>}
        {showSuggestions && <div className="cmdbar-pop" id="assistant-command-listbox" role="listbox" aria-label="Assistant commands">
          {suggestions.map((c, index) => <button key={c.name} id={`assistant-command-option-${index}`}
            className="cmdbar-pop-item" role="option" tabIndex={-1} aria-selected={index === activeSuggestionIndex}
            onMouseMove={() => setSuggestionIndex(index)}
            onMouseDown={(e) => { e.preventDefault(); chooseSuggestion(index) }}>
            <b>/{c.name}</b><span className="muted"> {c.desc}</span></button>)}
        </div>}
        <input className="cmdbar-in" ref={inputRef} value={input}
          role="combobox" aria-autocomplete="list" aria-expanded={showSuggestions}
          aria-controls="assistant-command-listbox"
          aria-activedescendant={activeSuggestionIndex >= 0 ? `assistant-command-option-${activeSuggestionIndex}` : undefined}
          disabled={historical || commandBusy} onChange={e => { setInput(e.target.value); setSuggestionsDismissed(false); setSuggestionIndex(0) }} onKeyDown={onKey}
          placeholder={historical ? `History seq ${runAccess.seq} · return live to use Assistant` : runId
            ? 'Command or ask…  /stop · pause · #12 to attach an experiment · or describe what to do'
            : 'Describe a run to start, or ask the assistant…  ( / for commands )'} />
      </div>
      {attachBtn('cmdbar-attach')}
      {busy
        ? <span className="cmdbar-status thinking"><span className="cmdbar-pip" /> thinking…</span>
        : commandBusy
          ? <span ref={commandStatusRef} tabIndex={-1}
              className={'cmdbar-status thinking' + (canCheckDirect ? ' recovery' : '')}
              role={directNeedsAlert ? 'alert' : 'status'}
              aria-live={directNeedsAlert ? 'assertive' : 'polite'} aria-atomic="true">
              <span className="cmdbar-pip" /> {pendingCommandText || 'Waiting for command…'}
              {canCheckDirect && <button className="btn sm" onClick={checkDirect}>Check</button>}
              {directPending?.protocolInvalid && <button className="btn sm ghost" onClick={dismissProtocolDirect}>Dismiss</button>}
            </span>
          : directFailure
            ? <span ref={commandStatusRef} tabIndex={-1} className="cmdbar-status thinking recovery error"
                role="alert" aria-live="assertive" aria-atomic="true">
                <span>{directFailureText}</span>
                {canRetryDirect && <button className="btn sm" onClick={retryDirect}>Retry same command</button>}
                <button className="btn sm ghost" onClick={dismissDirectFailure}>Dismiss</button>
              </span>
          : preview
          ? <button className="cmdbar-status preview" title="open the conversation" onClick={openSide}>
              <span className="cmdbar-who">assistant</span> {preview}<span className="cmdbar-more"> ▸</span></button>
          : null}
      {/* send / stop share ONE slot (you can't send mid-turn) — kept separate from the side button
          so stopping never opens a view. */}
      {busy
        ? <button className="cmdbar-go stop" title="stop the assistant" onClick={stop}>■</button>
        : <button className="cmdbar-go" title={commandBusy ? 'Waiting for the current run command' : historical ? 'Return live to use Assistant' : 'send (Enter)'} disabled={historical || commandBusy || (!input.trim() && files.length === 0)} onClick={send}>▶</button>}
      <button className="cmdbar-drawer-btn" title="open chat on the right (side view)" onClick={openSide}><OpIcon name="chat" size={13} /></button>
      {visibleToast && <div className="cmdbar-toast" role="status" aria-live="polite" aria-atomic="true">{visibleToast}</div>}
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
      {visibleToast && <div className="cmdbar-toast side" role="status" aria-live="polite" aria-atomic="true">{visibleToast}</div>}
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
        {visibleToast && <div className="cmdbar-toast side" role="status" aria-live="polite" aria-atomic="true">{visibleToast}</div>}
      </div>
    </div>}
  </>
}
