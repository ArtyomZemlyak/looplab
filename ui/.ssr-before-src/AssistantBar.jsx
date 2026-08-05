import React, { useEffect, useRef, useState } from 'react'
import { Turn, PermCard } from './AssistantChat.jsx'
import { OpIcon } from './icons.jsx'
import { useMediaQuery, usePoll } from './hooks.js'
import { useToast } from './useToast.js'
import { getRunAccess } from './runMode.js'
import { DIALOG_PRIORITY, useDialogFocus } from './useDialogFocus.js'
import { AttentionLauncher, openAttentionCenter, useAttentionIndicator } from './attentionIndicator.jsx'
import { pendingApprovalTarget } from './runIndex.js'
import { assistantErrorInfo, assistantPreview } from './assistantErrors.js'
import { reconcilePendingPermissions } from './assistantPermission.js'
import {
  clearLaunchDraftSession, launchDraftKey, launchDraftSession, removeLaunchDraft, retainLaunchDraft,
} from './launchDraftStore.js'
import { proposalLaunchChat } from './launchProvenance.js'
import {
  assistantDirectObservationKind, assistantDirectStatus, assistantRunChanged,
  assistantStorageFailureOwnsLock, pollAssistantDirectOnce,
  presentAssistantCommandResult, restoreAssistantDirectEntry, submitAssistantDirect,
} from './assistantCommand.js'
import { assistantDirectDecision, assistantDirectPresentation } from './assistantDirectPolicy.js'
import {
  assistantRecoveryFailure, assistantRecoveryPayload, assistantReplyCompletesTurn, assistantTurnIndex,
  danglingAssistantTurn,
  unavailableAssistantRecovery,
} from './assistantRecovery.js'
import './assistant-polish.css'
import {
  get, fmtAgo, fmtDate, ASSISTANT_MODES as MODES, tokText, assistantCreate, assistantMessageStream,
  assistantCommands, assistantRevert, assistantSessions, assistantGet, assistantDelete,
  assistantPermissions, assistantResolve, assistantCancel, assistantProgress,
  assistantFork, assistantForkStatus, assistantShare, assistantUnshare,
  commandActionForEvent, commandCanRetry, commandErrorMessage,
  commandEventForAction,
  commandFailureRecord, commandFeedback, commandRecordMatchesAction, getRunCommand, retryRunCommand,
  getObservedRunGeneration, normalizeRunGeneration,
  COMMAND_SUCCEEDED, COMMAND_FAILED,
  createIdempotencyKey, saveAssistantRunTransport, loadAssistantRunTransport,
  clearAssistantRunTransport, clearRunCommandLock, loadRunCommandLock, saveRunCommandLock,
  clearDamagedLaunchTransport, clearLaunchTransport, listLaunchTransports, loadLaunchTransport,
  subscribeLaunchTransports, subscribeRunCommandLock,
  storageGet, storageSet, storageRemove, runApiPath,
} from './util.js'
import { boundedRequest, deadlineRequest } from './requestDeadline.js'
import { followClientRoute } from './accessibility.jsx'

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
const commitAssistantRoute = href => {
  if (location.hash === href) {
    requestAnimationFrame(() => document.querySelector('[data-route-main]')?.focus({ preventScroll: true }))
  } else location.hash = href
}
const ASSISTANT_FORK_ACTION_RE = /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/
const ASSISTANT_SESSION_RE = /^[\da-f]{16}$/
const assistantForkRecoveryKey = sid =>
  `ll.assistant-fork-recovery.${encodeURIComponent(String(sid || ''))}`
const validAssistantForkRecovery = value => value && typeof value === 'object'
  && !Array.isArray(value) && Object.keys(value).length === 2
  && typeof value.actionId === 'string' && ASSISTANT_FORK_ACTION_RE.test(value.actionId)
  && Number.isSafeInteger(value.expectedMessages) && value.expectedMessages >= 0
const loadAssistantForkRecovery = sid => {
  if (!ASSISTANT_SESSION_RE.test(String(sid || ''))) return null
  const key = assistantForkRecoveryKey(sid)
  const raw = storageGet(key)
  if (typeof raw !== 'string') return null
  if (raw.length > 256) { storageRemove(key); return null }
  try {
    const parsed = JSON.parse(raw)
    if (validAssistantForkRecovery(parsed)) return parsed
  } catch { /* invalid optional recovery state is discarded below */ }
  storageRemove(key)
  return null
}
const saveAssistantForkRecovery = (sid, recovery) => ASSISTANT_SESSION_RE.test(String(sid || ''))
  && validAssistantForkRecovery(recovery)
  && storageSet(assistantForkRecoveryKey(sid), JSON.stringify(recovery))
const clearAssistantForkRecovery = (sid, actionId) => {
  const current = loadAssistantForkRecovery(sid)
  if (current?.actionId !== actionId) return false
  return storageRemove(assistantForkRecoveryKey(sid))
}
const messagesOwnLaunchIdentity = (messages, sessionId, identity) => Array.isArray(messages)
  && messages.some((message, messageIndex) => Array.isArray(message?.proposals)
    && message.proposals.some((proposal, proposalIndex) => launchDraftKey({
      sessionId,
      messageId: message.turn_id || message.id,
      messageIndex,
      proposalId: proposal?.proposal_id,
      proposalIndex,
    }) === identity))

const retainLaunchDisclosure = (store, key, open, limit = 50) => {
  if (!key) return store || {}
  const next = { ...(store || {}) }
  delete next[key]
  if (open) next[key] = true
  const keys = Object.keys(next)
  for (const stale of keys.slice(0, Math.max(0, keys.length - limit))) delete next[stale]
  return next
}

// Run-control commands safe to fire directly (no model). `arg:true` needs a node id (e.g. /approve #12).
const FREEZE = { success: '⏸ run stopped (not finalized)',
  noop: '⏸ run already stopped', executing: 'Stop requested — waiting for freeze' }
const FINALIZE = { success: '⏹ run finalized',
  noop: '⏹ run already finalized', executing: 'Finalize requested — waiting for wrap-up' }
const DIRECT = {
  stop: FREEZE, pause: FREEZE, finalize: FINALIZE, abort: FINALIZE,
  resume:   { success: '▶ run resumed',
    noop: '▶ run already running', executing: 'Resume requested — waiting for engine' },
  ratify:   { success: '✓ eval spec ratified',
    noop: '✓ eval spec already ratified', executing: 'Ratification requested — awaiting confirmation' },
  approve:  { arg: true, success: (id) => `✓ approved #${id}`,
    noop: (id) => `✓ #${id} already approved`, executing: (id) => `Approval #${id} requested — awaiting confirmation` },
}
const directSpec = name => typeof name === 'string' && Object.hasOwn(DIRECT, name)
  ? DIRECT[name] : null
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
  const spec = directSpec(name)
  if (!spec) return null
  const arg = m[2] ? Number(m[2]) : null
  if (spec.arg && arg == null) return null
  // A node token changes the meaning of a run-wide lifecycle command. Treat that input as
  // invalid instead of silently discarding the node and stopping the whole run (or asking an LLM
  // to reinterpret a control-shaped typo). Preserve the draft so the user can correct it in place.
  if (!spec.arg && arg != null) return {
    invalid: true,
    message: `/${name} controls the whole run and does not accept #${arg}. Remove the node id to continue.`,
  }
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
  const name = Object.hasOwn(_NL_CONTROL, norm) ? _NL_CONTROL[norm] : null
  const spec = directSpec(name)
  return name && spec ? { name, spec, arg: null } : null
}
// `#N` must start a token (not follow a word/# char) and end at a boundary — so `#3498db` (hex color),
// URL fragments (`page#12`), and `x#5` don't fabricate an experiment reference.
const refNodes = (t) => [...new Set([...(t || '').matchAll(/(?<![\w#])#(?:node-)?(\d+)\b/gi)].map(m => Number(m[1])))]
const NEW_RUN_DRAFT_RE = /^\/(?:new|genesis|run)\b/i
const composerUsesRun = (input, files = [], pendingFileReads = 0) => {
  const text = String(input || '').trim()
  return files.length > 0 || pendingFileReads > 0
    || (!NEW_RUN_DRAFT_RE.test(text) && text.length > 0)
}
const composerRunKey = runId => runId == null ? '' : String(runId)
const uiRunContext = (runId, refs) => {
  if (!runId) return ''
  const safe = String(runId).replace(/[\]"\r\n]/g, ' ').slice(0, 200)
  const nodes = refs.length
    ? ` The user refers to ${refs.map(id => '#' + id).join(', ')}; read them with run tools.` : ''
  return `\n\n[UI context: run "${safe}" is open.${nodes} Use run tools if relevant.]`
}

// Popular one-tap prompts surfaced in the full view (and side view when empty). Keep short + generic.
const HINTS = [
  { label: 'Summarize my runs', text: 'Summarize my runs: best results, active work, and failures.' },
  { label: 'Start a new run', text: '/new ' },
  { label: 'Explain the best result', text: 'Explain the best experiment and why it works.' },
  { label: "What's next?", text: 'Propose the highest-value next experiment and why.' },
]

// Attach only text-ish files we can read as plain text (no special parsing). Cap each file so a huge
// paste doesn't blow the context; the backend receives the content inline in the instruction.
const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|log|py|js|jsx|ts|tsx|sh|c|cpp|h|hpp|java|go|rs|rb|sql|html|css|xml|env)$/i
const FILE_CHAR_CAP = 20000
const MAX_FILE_BYTES = 2 * 1024 * 1024   // never readAsText a giant log/csv into the tab (OOM)
const SECRET_RE = /(^|\/)\.env(\.|$)|\.pem$|\.key$|(^|\/)(id_rsa|id_ed25519)$|secret|credential/i
const NEW_CHAT_COMPOSER_KEY = '__new__'
const normalizeComposerMode = value => (
  MODES.some(candidate => candidate.id === value) ? value : 'plan'
)
const newComposerDraft = (mode = 'plan') => ({
  input: '', files: [], pendingFileReads: 0, runScope: null, mode: normalizeComposerMode(mode),
})
const ASSISTANT_SHARE_ID_RE = /^[0-9a-f]{32}$/
const ASSISTANT_SHARE_URL_RE = /^#\/assistant\/shared\/([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$/
const ASSISTANT_SHARE_IDS_MAX = 4096
const validAssistantShareId = value => typeof value === 'string' && ASSISTANT_SHARE_ID_RE.test(value)
const boundedAssistantShareIds = value => {
  if (!Array.isArray(value) || value.length > ASSISTANT_SHARE_IDS_MAX) return null
  const ids = []
  const seen = new Set()
  for (const id of value) {
    if (!validAssistantShareId(id) || seen.has(id)) return null
    seen.add(id); ids.push(id)
  }
  return ids
}
const assistantShareIds = meta => boundedAssistantShareIds(meta?.share_ids)
const assistantLiveShareIds = meta => boundedAssistantShareIds(meta?.live_share_ids)
const validAssistantShareMeta = meta => {
  const ids = assistantShareIds(meta)
  const liveIds = assistantLiveShareIds(meta)
  const shareSet = ids == null ? null : new Set(ids)
  if (ids == null || liveIds == null
      || liveIds.some(id => !shareSet.has(id))
      || typeof meta.shared !== 'boolean' || meta.shared !== (ids.length > 0)
      || !Number.isInteger(meta.share_count) || meta.share_count !== ids.length
      || typeof meta.share_live !== 'boolean' || meta.share_live !== (liveIds.length > 0)) return false
  return ids.length
    ? Number.isFinite(meta.share_expires_at) && meta.share_expires_at > 0
    : meta.share_expires_at == null
}
const assistantLiveShareAckRequired = error => error?.status === 409
  && error?.code === 'assistant_live_share_ack_required'
const assistantForkTurnInProgress = error => error?.status === 409
  && error?.code === 'assistant_turn_fork_in_progress'
const assistantLiveShareRecoveryFailure = {
  message: 'Saved Assistant turn paused because the live public-link set changed. Verify its status, then retry this exact turn.',
  notice: 'Saved Assistant turn paused · live public-link status changed',
  blocked: false,
}
const assistantShareReceipt = (value, expectedSession) => {
  const shareId = value?.share_id
  const match = typeof value?.url === 'string' ? ASSISTANT_SHARE_URL_RE.exec(value.url) : null
  const expiresAt = value?.expires_at
  if (value?.ok !== true || String(value?.session || '') !== String(expectedSession || '')
      || value?.live !== false || !validAssistantShareId(shareId) || match?.[1] !== shareId
      || !Number.isFinite(expiresAt) || expiresAt <= 0) return null
  return { shareId, relativeUrl: value.url, expiresAt }
}
const validAssistantShareFallback = value => {
  if (!value || !validAssistantShareId(value.shareId) || !Number.isFinite(value.expiresAt)
      || value.expiresAt <= 0 || typeof value.url !== 'string') return false
  try {
    const parsed = new URL(value.url)
    const match = ASSISTANT_SHARE_URL_RE.exec(parsed.hash)
    return parsed.origin === location.origin && parsed.pathname === location.pathname
      && !parsed.search && match?.[1] === value.shareId
  } catch { return false }
}
const ASSISTANT_OVERLAY_MAX_PX = 1439
const assistantMaxWidth = compact => Math.max(320, window.innerWidth - (compact ? 120 : 880))
const clampAssistantWidth = (value, compact = window.innerWidth <= ASSISTANT_OVERLAY_MAX_PX) => {
  const max = assistantMaxWidth(compact)
  return Math.min(Math.max(Number(value) || 440, 320), max)
}
const assistantRevertKey = (sessionId, change) => {
  const path = typeof change?.abs_path === 'string' ? change.abs_path : ''
  const recoveryId = typeof change?.recovery_id === 'string' ? change.recovery_id : ''
  return path && recoveryId
    ? JSON.stringify([String(sessionId || NEW_CHAT_COMPOSER_KEY), path, recoveryId]) : ''
}
const rememberBoundedSetValue = (values, value, limit = 256) => {
  values.delete(value)
  values.add(value)
  while (values.size > limit) values.delete(values.values().next().value)
  return values
}
const rememberBoundedMapValue = (values, key, value, limit = 256) => {
  values.delete(key)
  values.set(key, value)
  while (values.size > limit) values.delete(values.keys().next().value)
  return values
}
const readFileText = (file) => new Promise((resolve) => {
  const r = new FileReader()
  r.onload = () => resolve({ name: file.name, size: file.size, content: String(r.result || '').slice(0, FILE_CHAR_CAP), truncated: (r.result || '').length > FILE_CHAR_CAP })
  r.onerror = () => resolve(null)
  r.readAsText(file)
})

export default function AssistantBar({ runId, hidden = false, onReady }) {
  const compactAssistant = useMediaQuery(`(max-width: ${ASSISTANT_OVERLAY_MAX_PX}px)`)
  const attentionIndicator = useAttentionIndicator()
  const runAccessKey = runId == null ? null : String(runId)
  const pendingRouteAccess = { readOnly: true, seq: null, mode: 'loading' }
  const [runAccessState, setRunAccessState] = useState(() => ({
    runId: runAccessKey,
    // RunView publishes authoritative access in a layout effect. Until that publication is
    // observed, the persistent Assistant must not reuse a previous route's live capability.
    access: runId ? pendingRouteAccess : getRunAccess(runId),
  }))
  const runAccess = runAccessState.runId === runAccessKey
    ? runAccessState.access : pendingRouteAccess
  const historical = !!runId && runAccess.readOnly
  const staleDiagnostic = historical && runAccess.mode === 'stale-link'
  const startOverLocked = historical && runAccess.mode === 'start-over'
  const reviewLocked = historical && runAccess.mode === 'review'
  const runLoadingLocked = historical && runAccess.mode === 'loading'
  const runUnavailableLocked = historical && runAccess.mode === 'unavailable'
  const readOnlyAction = runLoadingLocked
    ? 'Run access is still loading; Assistant actions stay paused until it is verified'
    : runUnavailableLocked
      ? 'This run is unavailable; return to the run list or retry before using Assistant actions'
      : startOverLocked
    ? 'Start over must be resolved before asking the Assistant to change this run'
    : reviewLocked ? 'This review link is read-only'
      : staleDiagnostic
        ? 'This diagnostic link targets an earlier run generation — open the current generation to act'
        : `History seq ${runAccess.seq} is read-only — return live to act`
  const readOnlyShort = runLoadingLocked
    ? 'Run loading · Assistant paused'
    : runUnavailableLocked
      ? 'Run unavailable · Assistant paused'
      : startOverLocked
    ? 'Start over unresolved · recover the exact request'
    : reviewLocked ? 'Read-only review'
      : staleDiagnostic
        ? 'Stale diagnostic link · open the current generation'
        : `History seq ${runAccess.seq} · return live`
  const [input, setInputState] = useState('')
  const [draftRunScope, setDraftRunScope] = useState(null)
  const [sid, setSid] = useState(null)
  const [msgs, setMsgs] = useState([])
  // Editable Genesis task/settings can be sensitive.  Retain them only in this mounted Assistant's
  // memory so side/full/bar remounts are lossless without writing payloads or validation tokens to web storage.
  const [launchDrafts, setLaunchDrafts] = useState({})
  const [launchDisclosures, setLaunchDisclosures] = useState({})
  const [launchRecoveries, setLaunchRecoveries] = useState(() => listLaunchTransports())
  const [busy, setBusy] = useState(false)
  const [turnStarting, setTurnStarting] = useState(false)
  const [openingSid, setOpeningSid] = useState(null)
  const sessionOpening = openingSid != null
  const [retryChecking, setRetryChecking] = useState(false)
  const [directStarting, setDirectStarting] = useState(false)
  const [directConfirm, setDirectConfirm] = useState(null)
  const [directPending, setDirectPending] = useState(null) // retained while an accepted direct command executes
  const [directFailure, setDirectFailure] = useState(null)
  const [runCommandLock, setRunCommandLock] = useState(() => loadRunCommandLock(runId))
  const externalCommandPending = runCommandLock?.source === 'dock' ? runCommandLock : null
  const ownStorageFailureLock = assistantStorageFailureOwnsLock(directFailure, runCommandLock)
  const commandBusy = directStarting || directPending != null
    || (runCommandLock != null && !ownStorageFailureLock)
  const [preview, setPreview] = useState('')      // beginning of the latest reply (collapsed bar)
  const [hasNew, setHasNew] = useState(false)     // highlight the bar until a view is opened
  const [replyAnnouncement, setReplyAnnouncement] = useState('')
  const [view, setView] = useState('bar')         // 'bar' | 'side' | 'full'
  const viewRef = useRef(view)
  viewRef.current = view
  const setAssistantView = React.useCallback(update => {
    const next = typeof update === 'function' ? update(viewRef.current) : update
    viewRef.current = next
    setView(next)
  }, [])
  const [mode, setMode] = useState('plan')
  const [toast, flash, clearToast] = useToast()   // shared timer discipline (doc 25 UI-13)
  const [commands, setCommands] = useState([])
  const [suggestionIndex, setSuggestionIndex] = useState(0)
  const [suggestionsDismissed, setSuggestionsDismissed] = useState(false)
  const [runs, setRuns] = useState([])
  const [pending, setPending] = useState([])      // live HITL confirm requests
  const [attentionPermissionFocus, setAttentionPermissionFocusState] = useState({
    session: '', id: '', token: 0, phase: 'idle',
  })
  const attentionPermissionFocusRef = useRef(attentionPermissionFocus)
  const setAttentionPermissionFocus = React.useCallback(update => {
    if (typeof update !== 'function') {
      attentionPermissionFocusRef.current = update
      setAttentionPermissionFocusState(update)
      return
    }
    setAttentionPermissionFocusState(current => {
      const next = update(current)
      attentionPermissionFocusRef.current = next
      return next
    })
  }, [])
  attentionPermissionFocusRef.current = attentionPermissionFocus
  const [resolvingPerms, setResolvingPerms] = useState(() => new Set())
  const resolvingPermsRef = useRef(new Set())
  // Ids of permission requests resolved LOCALLY this session. A permissions poll in flight when the user
  // clicks Approve/Reject can return its pre-resolution snapshot AFTER the card was optimistically removed,
  // re-adding a resolved card with its buttons re-enabled (a contradictory second decision). We filter
  // poll-sourced pending against per-session sets; each self-heals as the server drops its ids.
  const resolvedPermsRef = useRef(new Map())
  const [sessions, setSessions] = useState([])    // full-view session list
  const [sessionsStatus, setSessionsStatus] = useState('idle')
  const [deleteConfirm, setDeleteConfirm] = useState(null)
  const [deleteConfirmBusy, setDeleteConfirmBusy] = useState(false)
  const [deleteConfirmError, setDeleteConfirmError] = useState('')
  const [revertingChanges, setRevertingChanges] = useState(() => new Set())
  const [revertedChanges, setRevertedChanges] = useState(() => new Set())
  const sessionsLoadedRef = useRef(false)
  const sessionsRequestSeqRef = useRef(0)
  const activeSessionsRequestRef = useRef(null)
  const refreshSessionsRef = useRef(null)
  const sessionDeleteTombstonesRef = useRef(new Map())
  const shareMutationEpochRef = useRef(0)
  const shareMetaReadSeqRef = useRef(new Map())
  const shareVerificationRef = useRef({ seq: 0, sid: null, promise: null })
  const [shareVerifySid, setShareVerifySid] = useState(null)
  const beginShareMetaRead = React.useCallback(sessionId => {
    const id = String(sessionId || '')
    const seq = (shareMetaReadSeqRef.current.get(id) || 0) + 1
    shareMetaReadSeqRef.current.set(id, seq)
    return { id, seq, mutationEpoch: shareMutationEpochRef.current,
      listRequestSeq: sessionsRequestSeqRef.current }
  }, [])
  const shareMetaReadCurrent = React.useCallback(read => !!read?.id
    && shareMetaReadSeqRef.current.get(read.id) === read.seq
    && shareMutationEpochRef.current === read.mutationEpoch
    && sessionsRequestSeqRef.current === read.listRequestSeq, [])
  const mutateSessionsLocally = React.useCallback(update => {
    // A locally confirmed mutation is newer than every list read already in flight. Fence those
    // responses so a slow refresh cannot resurrect a deleted chat or undo share-state truth.
    sessionsRequestSeqRef.current += 1
    shareMutationEpochRef.current += 1
    activeSessionsRequestRef.current = null
    const verificationSeq = shareVerificationRef.current.seq + 1
    shareVerificationRef.current = { seq: verificationSeq, sid: null, promise: null }
    setShareVerifySid(null)
    setSessions(update)
    // assistantGet can already have supplied one authoritative session row while the complete list is
    // still loading. Apply exact share/unshare/delete receipts to that known row, but do not label the
    // partial array "ready" (or its empty remainder "No chats yet") until a list read has succeeded.
    if (sessionsLoadedRef.current) {
      setSessionsStatus(current => current === 'stale' ? current : 'ready')
    }
    return true
  }, [])
  const [shareUnknownSids, setShareUnknownSids] = useState(() => new Set())
  const [shareCopyFallbacks, setShareCopyFallbacks] = useState({})
  const [shareBusySid, setShareBusySid] = useState(null)
  const [forkBusySid, setForkBusySid] = useState(null)
  const [forkRecovery, setForkRecovery] = useState(null)
  const [shareAckNotice, setShareAckNotice] = useState(null)
  const [files, setFilesState] = useState([])     // attached text files [{name,size,content,truncated}]
  const [pendingFileReads, setPendingFileReads] = useState(0)
  const [sideW, setSideW] = useState(() => clampAssistantWidth(storageGet('ll.asstW', 440)))
  const autoRevealedPendingIdsRef = useRef(new Set())

  // A permission card cannot render in the collapsed composer bar. Reveal the side thread as soon as
  // a new one arrives so the safe Reject-first focus and assertive pending announcement are usable.
  // Mark each live request once: an intentional fold or route handoff must not be immediately undone
  // by the same still-pending card, while a genuinely new request can still reveal itself.
  useEffect(() => {
    const liveIds = new Set(pending.map(request => request?.id).filter(Boolean))
    for (const id of autoRevealedPendingIdsRef.current) {
      if (!liveIds.has(id)) autoRevealedPendingIdsRef.current.delete(id)
    }
    if (hidden || historical) return
    const newIds = [...liveIds].filter(id => !autoRevealedPendingIdsRef.current.has(id))
    if (!newIds.length) return
    for (const id of newIds) autoRevealedPendingIdsRef.current.add(id)
    if (view === 'bar') {
      setAssistantView('side'); setHasNew(false)
    }
  }, [hidden, historical, pending, view])

  const mountedRef = useRef(true)
  const historicalRef = useRef(historical)
  historicalRef.current = historical
  const readOnlyActionRef = useRef(readOnlyAction)
  readOnlyActionRef.current = readOnlyAction
  const currentRunIdRef = useRef(runId)
  currentRunIdRef.current = runId
  const abortRef = useRef(null)
  const runningRef = useRef(false)   // a turn is live (stream OR reattach poll); stop clears it to halt both
  const activeReplyAttemptRef = useRef(null)
  const turnCaptureRef = useRef(false) // closes session-create/send races before React can publish busy
  const openSessionSeqRef = useRef(0) // late reads must never replace a newer session choice
  const openSessionPendingRef = useRef(null) // blocks mutations while a chosen transcript/progress read resolves
  const deferredRecoverySessionRef = useRef(null) // read-only routes hydrate dangling turns without replaying them
  const directCaptureRef = useRef(false) // closes the pre-storage generation-read window to double clicks
  const directConfirmRef = useRef(null) // closes double-submit races before the confirmation card renders
  const cancelReqRef = useRef(null)  // in-flight server-cancel POST; the next send awaits it so a late
                                     // cancel can't land on (and instantly kill) the NEW turn's event
  const sidRef = useRef(null)   // session the stream callbacks belong to (guards cross-session bleed)
  const inputRef = useRef(null)
  const directConfirmStatusRef = useRef(null)
  const directConfirmCancelRef = useRef(null)
  const feedRef = useRef(null)
  const sessionsRef = useRef(null)
  const fullDialogRef = useRef(null)
  const sideDialogRef = useRef(null)
  const deleteDialogRef = useRef(null)
  const deleteErrorRef = useRef(null)
  const deleteTriggerRef = useRef(null)
  const deleteFocusAfterRef = useRef(null)
  const deleteConfirmBusyRef = useRef(false)
  const revertingChangesRef = useRef(new Set())
  const revertedChangesRef = useRef(new Set())
  const atBottomRef = useRef(true)     // is the feed scrolled to (near) the bottom? gates autoscroll
  const replyAnnouncementTimerRef = useRef(null)
  const announcedReplyTurnsRef = useRef(new Set())
  const announcedReplyAttemptsRef = useRef(new WeakSet())
  const fileRef = useRef(null)
  const commandStatusRef = useRef(null)
  const commandFocusRequestedRef = useRef(false)
  const collapseForNavigationRef = useRef(null)
  const pendingRouteHandoffRef = useRef(null)
  const openSessionRef = useRef(null)
  const attentionPermissionHandoffRef = useRef(0)
  const attentionPermissionReceiptRef = useRef(0)
  const permissionReadEpochRef = useRef(new Map())
  const permissionReadPendingRef = useRef(new Map())
  const toastRunIdRef = useRef(runId)
  const shareActionSessionRef = useRef(null)
  const forkActionSessionRef = useRef(null)
  const forkRecoveryRef = useRef(new Map())
  const deletingSessionsRef = useRef(new Set())
  const clearDirectConfirmation = React.useCallback(({ focus = false } = {}) => {
    try { directConfirmRef.current?.requestController?.abort() } catch { /* already settled */ }
    directConfirmRef.current = null
    setDirectConfirm(null)
    if (focus) requestAnimationFrame(() => inputRef.current?.isConnected
      && inputRef.current.focus({ preventScroll: true }))
  }, [])
  directConfirmRef.current = directConfirm
  const cancelAttentionPermissionHandoff = React.useCallback(() => {
    if (attentionPermissionFocusRef.current.phase === 'idle') return false
    const token = attentionPermissionHandoffRef.current + 1
    attentionPermissionHandoffRef.current = token
    setAttentionPermissionFocus({ session: '', id: '', token, phase: 'idle' })
    return true
  }, [setAttentionPermissionFocus])
  // Permissions are complete snapshots. Coalesce ordinary reads for one session and fence every
  // response against a newer read, so a late poll can never replace a fresher approval registry.
  // Attention requests `fresh` after selecting the owning chat because its global feed can be newer
  // than a session poll that was already in flight.
  const readPermissionSnapshot = React.useCallback((session, { fresh = false } = {}) => {
    const id = String(session || '')
    const current = permissionReadPendingRef.current.get(id)
    if (!fresh && current) return current.promise
    const seq = (permissionReadEpochRef.current.get(id) || 0) + 1
    permissionReadEpochRef.current.set(id, seq)
    const record = { session: id, seq, promise: null }
    record.promise = Promise.resolve()
      .then(() => boundedRequest(signal => assistantPermissions(id, { signal }), 8000))
      .then(payload => {
        if (permissionReadEpochRef.current.get(id) !== seq) {
          return { ok: false, reason: 'superseded', session: id, seq }
        }
        if (!Array.isArray(payload?.pending)) {
          return { ok: false, reason: 'invalid', session: id, seq }
        }
        const resolvedForSession = resolvedPermsRef.current.get(id)
        const pending = reconcilePendingPermissions(payload.pending,
          resolvedForSession || new Set())
        if (resolvedForSession && resolvedForSession.size === 0) {
          resolvedPermsRef.current.delete(id)
        }
        return { ok: true, session: id, seq, pending }
      }, error => permissionReadEpochRef.current.get(id) !== seq
        ? { ok: false, reason: 'superseded', session: id, seq }
        : { ok: false, reason: 'error', error, session: id, seq })
      .finally(() => {
        if (permissionReadPendingRef.current.get(id) === record) {
          permissionReadPendingRef.current.delete(id)
        }
      })
    permissionReadPendingRef.current.set(id, record)
    return record.promise
  }, [])
  const replyAttemptOwned = (attempt, id) => !!attempt
    && activeReplyAttemptRef.current === attempt
    && mountedRef.current && sidRef.current === id
  const replyAttemptCurrent = (attempt, id) => replyAttemptOwned(attempt, id) && runningRef.current
  const clearReplyAnnouncement = React.useCallback(() => {
    if (replyAnnouncementTimerRef.current) clearTimeout(replyAnnouncementTimerRef.current)
    replyAnnouncementTimerRef.current = null
    setReplyAnnouncement('')
  }, [])
  const announceReplyReady = React.useCallback((content, { sessionId = null, turn = null,
    attempt = null } = {}) => {
    const text = String(content || '').trim()
    if (!text || text === '(no reply)' || assistantErrorInfo(text)) {
      clearReplyAnnouncement()
      return false
    }
    if (sessionId != null && String(sidRef.current || '') !== String(sessionId)) return false
    const turnId = typeof turn?.turn_id === 'string' ? turn.turn_id.trim() : ''
    const turnKey = turnId ? `${String(sessionId || '')}:${turnId}` : ''
    if (turnKey && announcedReplyTurnsRef.current.has(turnKey)) return false
    if (!turnKey && attempt && typeof attempt === 'object'
        && announcedReplyAttemptsRef.current.has(attempt)) return false
    if (turnKey) rememberBoundedSetValue(announcedReplyTurnsRef.current, turnKey)
    else if (attempt && typeof attempt === 'object') announcedReplyAttemptsRef.current.add(attempt)
    if (replyAnnouncementTimerRef.current) clearTimeout(replyAnnouncementTimerRef.current)
    setReplyAnnouncement('Assistant response ready.')
    replyAnnouncementTimerRef.current = setTimeout(() => {
      replyAnnouncementTimerRef.current = null
      if (mountedRef.current) setReplyAnnouncement('')
    }, 2500)
    return true
  }, [clearReplyAnnouncement])
  useEffect(() => {
    if (!sid) { setForkRecovery(null); return }
    const recovery = forkRecoveryRef.current.get(sid) || loadAssistantForkRecovery(sid)
    if (recovery) forkRecoveryRef.current.set(sid, recovery)
    setForkRecovery(recovery ? { sid, ...recovery } : null)
  }, [sid])
  // A composer belongs to the chat it was written in. Keep text and attachments in memory per session
  // so selecting another transcript cannot silently send the previous chat's draft. The unsaved
  // new-chat composer has its own slot; "+ New" deliberately resets that slot.
  const composerDraftsRef = useRef(new Map([
    [NEW_CHAT_COMPOSER_KEY, newComposerDraft()],
  ]))
  const composerKeyRef = useRef(NEW_CHAT_COMPOSER_KEY)
  const composerDraftRef = useRef(composerDraftsRef.current.get(NEW_CHAT_COMPOSER_KEY))
  const setInput = React.useCallback(update => {
    const draft = composerDraftRef.current
    const usedRun = composerUsesRun(draft.input, draft.files, draft.pendingFileReads)
    const next = typeof update === 'function' ? update(draft.input) : update
    draft.input = next
    const usesRun = composerUsesRun(next, draft.files, draft.pendingFileReads)
    if (!usesRun) draft.runScope = null
    else if (!usedRun || draft.runScope == null) draft.runScope = composerRunKey(currentRunIdRef.current)
    setInputState(next)
    setDraftRunScope(draft.runScope)
  }, [])
  const updateComposerFiles = React.useCallback((draft, update, runScope = undefined) => {
    const usedRun = composerUsesRun(draft.input, draft.files, draft.pendingFileReads)
    const next = typeof update === 'function' ? update(draft.files) : update
    draft.files = next
    const usesRun = composerUsesRun(draft.input, next, draft.pendingFileReads)
    if (!usesRun) draft.runScope = null
    else if (!usedRun || draft.runScope == null) {
      draft.runScope = runScope === undefined
        ? composerRunKey(currentRunIdRef.current) : runScope
    }
    if (composerDraftRef.current === draft) {
      setFilesState(next)
      setDraftRunScope(draft.runScope)
    }
  }, [])
  const updatePendingFileReads = React.useCallback((draft, update) => {
    const current = Math.max(0, Number(draft.pendingFileReads) || 0)
    const nextValue = typeof update === 'function' ? update(current) : update
    const next = Math.max(0, Number(nextValue) || 0)
    draft.pendingFileReads = next
    if (!composerUsesRun(draft.input, draft.files, next)) draft.runScope = null
    if (composerDraftRef.current === draft) {
      setPendingFileReads(next)
      setDraftRunScope(draft.runScope)
    }
    return next
  }, [])
  const setFiles = React.useCallback(update => {
    updateComposerFiles(composerDraftRef.current, update)
  }, [updateComposerFiles])
  const setComposerMode = React.useCallback(value => {
    const next = normalizeComposerMode(value)
    composerDraftRef.current.mode = next
    setMode(next)
  }, [])
  const activateComposer = React.useCallback((key, { clear = false, seedMode = 'plan' } = {}) => {
    const draft = clear
      ? newComposerDraft()
      : composerDraftsRef.current.get(key) || newComposerDraft(seedMode)
    draft.mode = normalizeComposerMode(draft.mode ?? seedMode)
    draft.pendingFileReads = Math.max(0, Number(draft.pendingFileReads) || 0)
    composerDraftsRef.current.set(key, draft)
    composerKeyRef.current = key
    composerDraftRef.current = draft
    if (draft.runScope == null && composerUsesRun(
      draft.input, draft.files, draft.pendingFileReads,
    )) {
      draft.runScope = composerRunKey(currentRunIdRef.current)
    }
    setInputState(draft.input)
    setFilesState(draft.files)
    setPendingFileReads(draft.pendingFileReads)
    setDraftRunScope(draft.runScope)
    setMode(draft.mode)
    setSuggestionsDismissed(false)
    setSuggestionIndex(0)
    return draft
  }, [])
  const bindComposerToSession = React.useCallback(id => {
    const previousKey = composerKeyRef.current
    const draft = composerDraftRef.current
    composerDraftsRef.current.set(id, draft)
    if (previousKey === NEW_CHAT_COMPOSER_KEY) {
      composerDraftsRef.current.set(NEW_CHAT_COMPOSER_KEY, newComposerDraft())
    }
    composerKeyRef.current = id
  }, [])
  const setShareUnknown = React.useCallback((sessionId, unknown) => {
    setShareUnknownSids(current => {
      const next = new Set(current)
      if (unknown) next.add(sessionId)
      else next.delete(sessionId)
      return next
    })
  }, [])
  const retainShareCopy = React.useCallback((sessionId, fallback) => {
    setShareCopyFallbacks(current => ({ ...current, [sessionId]: fallback }))
  }, [])
  const clearShareCopy = React.useCallback(sessionId => {
    setShareCopyFallbacks(current => {
      if (!Object.prototype.hasOwnProperty.call(current, sessionId)) return current
      const next = { ...current }
      delete next[sessionId]
      return next
    })
  }, [])
  const applyAssistantShareMeta = React.useCallback((sessionId, meta, read) => {
    const id = String(sessionId || '')
    if (!id || sessionDeleteTombstonesRef.current.has(id) || !shareMetaReadCurrent(read)) return undefined
    // An exact per-session read supersedes every list read already in flight. A confirmed local
    // share/unshare receipt changes the mutation epoch instead, so a GET that started before that
    // receipt can never roll it back.
    const invalidatedListRequest = activeSessionsRequestRef.current
    sessionsRequestSeqRef.current += 1
    if (invalidatedListRequest != null) {
      activeSessionsRequestRef.current = null
      queueMicrotask(() => {
        if (mountedRef.current && activeSessionsRequestRef.current == null) {
          refreshSessionsRef.current?.()
        }
      })
    }
    shareMetaReadSeqRef.current.set(id, read.seq + 1)
    if (!meta || String(meta.id || '') !== id) {
      if (id && !sessionDeleteTombstonesRef.current.has(id)) setShareUnknown(id, true)
      return false
    }
    const shareMetaValid = validAssistantShareMeta(meta)
    setShareUnknown(id, !shareMetaValid)
    if (shareMetaValid) {
      setShareCopyFallbacks(current => {
        const fallback = current[id]
        if (!fallback || (validAssistantShareFallback(fallback)
            && meta.share_ids.includes(fallback.shareId))) return current
        const next = { ...current }
        delete next[id]
        return next
      })
    }
    const sessionMeta = shareMetaValid ? meta : {
      ...meta,
      // Keep the transcript readable, but never render malformed capability metadata as private.
      shared: true, share_count: 0, share_ids: [], live_share_ids: [],
      share_expires_at: null, share_live: false,
    }
    setSessions(current => {
      const index = current.findIndex(session => String(session.id || '') === id)
      if (index < 0) return [sessionMeta, ...current]
      const next = [...current]
      next[index] = { ...next[index], ...sessionMeta }
      return next
    })
    return shareMetaValid
  }, [setShareUnknown, shareMetaReadCurrent])
  const refreshSessionShareMeta = React.useCallback(async sessionId => {
    const read = beginShareMetaRead(sessionId)
    try {
      const session = await boundedRequest(signal => assistantGet(sessionId, { signal }))
      if (!mountedRef.current || !shareMetaReadCurrent(read)) return undefined
      const outcome = applyAssistantShareMeta(sessionId, session?.meta, read)
      return outcome === undefined ? undefined : outcome ? session : null
    } catch {
      if (!mountedRef.current || !shareMetaReadCurrent(read)) return undefined
      setShareUnknown(sessionId, true)
      return null
    }
  }, [applyAssistantShareMeta, beginShareMetaRead, setShareUnknown, shareMetaReadCurrent])
  useEffect(() => {
    const accessKey = runId == null ? null : String(runId)
    setRunAccessState({ runId: accessKey, access: getRunAccess(runId) })
    const onAccess = (e) => {
      if (String(e.detail?.runId) === String(runId)) {
        setRunAccessState({ runId: accessKey, access: getRunAccess(runId) })
      }
    }
    window.addEventListener('ll:run-access', onAccess)
    return () => window.removeEventListener('ll:run-access', onAccess)
  }, [runId])
  useEffect(() => {
    setRunCommandLock(loadRunCommandLock(runId))
    return subscribeRunCommandLock(runId, setRunCommandLock)
  }, [runId])
  useEffect(() => subscribeLaunchTransports(setLaunchRecoveries), [])
  useEffect(() => {
    // A run change makes the previous run's notice MISLEADING, not merely stale — retire it early
    // and cancel its timer so a notice for the NEW run is not cut short by the old one's window.
    if (assistantRunChanged(toastRunIdRef.current, runId)) clearToast()
    toastRunIdRef.current = runId
  }, [runId])
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      activeReplyAttemptRef.current = null
      openSessionPendingRef.current = null
      if (abortRef.current) abortRef.current.abort()
      if (replyAnnouncementTimerRef.current) clearTimeout(replyAnnouncementTimerRef.current)
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
  const feedOpen = view === 'side' || view === 'full'
  usePoll((alive) => get('/api/runs').then(r => alive() && setRuns(r || [])).catch(() => {}),
    6000, [feedOpen], { enabled: feedOpen })
  const runsById = React.useMemo(() => Object.fromEntries(runs.map(r => [r.run_id, r])), [runs])
  const refreshSessions = React.useCallback(async () => {
    const requestSeq = ++sessionsRequestSeqRef.current
    activeSessionsRequestRef.current = requestSeq
    const requestedSid = sidRef.current
    const mutationEpoch = shareMutationEpochRef.current
    setSessionsStatus(sessionsLoadedRef.current ? 'refreshing' : 'loading')
    try {
      const result = await boundedRequest(signal => assistantSessions({ signal }))
      if (!mountedRef.current || requestSeq !== sessionsRequestSeqRef.current
          || mutationEpoch !== shareMutationEpochRef.current) return null
      if (!Array.isArray(result?.sessions)) throw new Error('Invalid Assistant session list.')
      if (!result.sessions.every(session => validAssistantShareMeta(session))) {
        throw new Error('Invalid Assistant public-link state.')
      }
      const returnedIds = new Set(result.sessions.map(session => String(session?.id || '')))
      for (const [id, state] of sessionDeleteTombstonesRef.current) {
        if (state === 'confirmed' && !returnedIds.has(id)) {
          sessionDeleteTombstonesRef.current.delete(id)
        }
      }
      const visibleSessions = result.sessions.filter(
        session => !sessionDeleteTombstonesRef.current.has(String(session?.id || '')),
      )
      const visibleById = new Map(visibleSessions.map(session => [String(session?.id || ''), session]))
      // A successful list read is the authoritative share view. Resolve ambiguous timed-out share /
      // unshare actions, and discard an in-memory copy once the server says that capability is no
      // longer active (expired, revoked, or removed in another tab).
      setShareUnknownSids(current => {
        if (![...current].some(id => visibleById.has(String(id)))) return current
        const next = new Set(current)
        for (const id of current) if (visibleById.has(String(id))) next.delete(id)
        return next
      })
      setShareCopyFallbacks(current => {
        let next = current
        for (const id of Object.keys(current)) {
          const session = visibleById.get(String(id))
          const fallback = current[id]
          if (!validAssistantShareFallback(fallback)
              || !session?.share_ids.includes(fallback.shareId)) {
            if (next === current) next = { ...current }
            delete next[id]
          }
        }
        return next
      })
      sessionsLoadedRef.current = true
      setSessions(visibleSessions)
      setSessionsStatus('ready')
      if (activeSessionsRequestRef.current === requestSeq) activeSessionsRequestRef.current = null
      return visibleSessions
    } catch {
      if (!mountedRef.current || requestSeq !== sessionsRequestSeqRef.current
          || mutationEpoch !== shareMutationEpochRef.current) return null
      if (requestedSid && sidRef.current === requestedSid) setShareUnknown(requestedSid, true)
      setSessionsStatus(sessionsLoadedRef.current ? 'stale' : 'error')
      if (activeSessionsRequestRef.current === requestSeq) activeSessionsRequestRef.current = null
      return null
    }
  }, [setShareUnknown])
  refreshSessionsRef.current = refreshSessions
  // Share privacy terms matter even in the compact bar: a legacy/API-created live link follows new
  // replies. Re-check on session switches in every surface, and still load the list for New chat.
  useEffect(() => {
    if (sid || view === 'full') refreshSessions()
  }, [view, sid, refreshSessions])
  useEffect(() => {
    if (!sid) return undefined
    const refreshOnFocus = () => refreshSessions()
    window.addEventListener('focus', refreshOnFocus)
    return () => window.removeEventListener('focus', refreshOnFocus)
  }, [sid, refreshSessions])

  // Autoscroll ONLY when the user is already near the bottom — don't yank them back down while they've
  // scrolled up to read earlier turns during a streaming reply.
  const onFeedScroll = (e) => { const f = e.currentTarget; atBottomRef.current = f.scrollHeight - f.scrollTop - f.clientHeight < 80 }
  useEffect(() => { if (feedOpen && feedRef.current && atBottomRef.current) requestAnimationFrame(() => { feedRef.current.scrollTop = feedRef.current.scrollHeight }) }, [msgs, view, busy])

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
    setAssistantView(next); setHasNew(false)
    if (next === 'side') requestAnimationFrame(() => inputRef.current?.focus({ preventScroll: true }))
  }
  const openSide = () => openCommandView('side')   // openable any time (even empty)
  const openFull = () => openCommandView('full')
  useEffect(() => {
    if (view !== 'full' || !sid) return
    let frame = null
    const revealActiveSession = () => {
      if (frame != null) cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        sessionsRef.current?.querySelector('.asst-sess.active')
          ?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
      })
    }
    revealActiveSession()
    window.addEventListener('resize', revealActiveSession)
    return () => {
      window.removeEventListener('resize', revealActiveSession)
      if (frame != null) cancelAnimationFrame(frame)
    }
  }, [sessions.length, sid, view])
  const closeDeleteConfirm = React.useCallback(() => {
    if (deleteConfirmBusyRef.current) return
    const trigger = deleteTriggerRef.current
    const focusAfterDelete = deleteFocusAfterRef.current
    setDeleteConfirm(null)
    setDeleteConfirmError('')
    deleteTriggerRef.current = null
    deleteFocusAfterRef.current = null
    // useDialogFocus restores the original trigger in the normal case. If an optimistic attempt
    // removed and then restored that row, its original DOM node no longer exists, so keep a stable
    // neighbouring action as the recovery destination instead of dropping focus onto <body>.
    requestAnimationFrame(() => {
      const target = trigger?.isConnected
        ? trigger
        : focusAfterDelete?.isConnected
          ? focusAfterDelete
          : document.querySelector('.asst-sessions .asst-sess-open, .asst-side-h .btn.primary')
      target?.focus({ preventScroll: true })
    })
  }, [])
  const collapseToBar = () => {
    if (directConfirmRef.current) {
      clearDirectConfirmation({ focus: true })
      return
    }
    cancelAttentionPermissionHandoff()
    const returnSelector = view === 'side' ? '.cmdbar-drawer-btn' : '.cmdbar-ic'
    const la = lastAssistant()
    if (la && la.content) setPreview(previewText(la.content))
    // Folding a reply the user was already reading may reveal its preview, but it is not a new reply.
    setHasNew(false)
    if (commandBusy || directFailure) commandFocusRequestedRef.current = true
    setAssistantView('bar')
    requestAnimationFrame(() => document.querySelector(returnSelector)?.focus())
  }
  collapseForNavigationRef.current = () => {
    cancelAttentionPermissionHandoff()
    clearDirectConfirmation()
    const la = lastAssistant()
    if (la && la.content) setPreview(previewText(la.content))
    // Route navigation owns the next focus target. Do not let the ordinary fold-to-bar RAF steal it
    // or present a known Assistant reply as newly arrived merely because the route was revealed.
    commandFocusRequestedRef.current = false
    setHasNew(false)
    setAssistantView('bar')
  }
  useEffect(() => {
    if (hidden) return undefined
    const onCollapseForNavigation = () => collapseForNavigationRef.current?.()
    window.addEventListener('ll:collapse-assistant-for-navigation', onCollapseForNavigation)
    return () => window.removeEventListener('ll:collapse-assistant-for-navigation', onCollapseForNavigation)
  }, [hidden])
  useEffect(() => {
    if (view !== 'bar' || !pendingRouteHandoffRef.current) return
    const href = pendingRouteHandoffRef.current
    pendingRouteHandoffRef.current = null
    // Commit the route only after React has removed aria-modal/inert ownership from the Assistant.
    // RouteFocus can then name and focus loading, ready, or error content without racing the old modal.
    commitAssistantRoute(href)
  }, [view])
  const handoffAssistantRoute = (href, { collapse = false } = {}) => {
    cancelAttentionPermissionHandoff()
    if (view === 'bar') {
      pendingRouteHandoffRef.current = null
      clearDirectConfirmation()
      commitAssistantRoute(href)
      return
    }
    const modalAssistant = view === 'full' || (view === 'side' && compactAssistant)
    if (!collapse && !modalAssistant) {
      pendingRouteHandoffRef.current = null
      clearDirectConfirmation()
      commitAssistantRoute(href)
      return
    }
    // A pending approval that was already present belongs to the surface the user just left. Mark it
    // as revealed before folding so the auto-reveal effect cannot immediately cover the destination.
    for (const request of pending) {
      if (request?.id) autoRevealedPendingIdsRef.current.add(request.id)
    }
    pendingRouteHandoffRef.current = href
    collapseForNavigationRef.current?.()
  }
  const openRunFromAssistant = (event, href) => followClientRoute(event,
    () => handoffAssistantRoute(href))
  const toggleSide = () => (view === 'side' ? collapseToBar() : openSide())
  useDialogFocus(fullDialogRef, collapseToBar, view === 'full' && !hidden,
    { priority: DIALOG_PRIORITY.ASSISTANT_FULL })
  useDialogFocus(sideDialogRef, collapseToBar, view === 'side' && !hidden,
    { modal: compactAssistant, priority: DIALOG_PRIORITY.ASSISTANT_SIDE })
  useDialogFocus(deleteDialogRef, deleteConfirmBusy ? null : closeDeleteConfirm,
    !!deleteConfirm && !hidden, { priority: DIALOG_PRIORITY.ASSISTANT_DELETE })
  useEffect(() => {
    if (deleteConfirmError) deleteErrorRef.current?.focus({ preventScroll: true })
  }, [deleteConfirmError])

  // ── resizable side panel (drag its left edge) ──
  const resizeCleanupRef = useRef(null)
  const maxSideWidth = () => assistantMaxWidth(compactAssistant)
  const startResize = (e) => {
    if (e.button != null && e.button !== 0) return
    e.preventDefault()
    const x0 = e.clientX, w0 = sideW
    const onMove = (ev) => setSideW(Math.min(Math.max(w0 - (ev.clientX - x0), 320), maxSideWidth()))
    const cleanup = () => {
      window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', cleanup)
      window.removeEventListener('pointercancel', cleanup)
      document.body.style.cursor = ''; resizeCleanupRef.current = null
    }
    resizeCleanupRef.current = cleanup   // unmount mid-drag must also detach the window listeners
    document.body.style.cursor = 'col-resize'
    window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', cleanup)
    window.addEventListener('pointercancel', cleanup)
  }
  const resizeWithKeys = event => {
    const step = event.shiftKey ? 48 : 16
    let next = null
    if (event.key === 'ArrowLeft') next = sideW + step
    else if (event.key === 'ArrowRight') next = sideW - step
    else if (event.key === 'Home') next = 320
    else if (event.key === 'End') next = maxSideWidth()
    if (next == null) return
    event.preventDefault(); setSideW(Math.min(Math.max(next, 320), maxSideWidth()))
  }
  useEffect(() => () => resizeCleanupRef.current?.(), [])
  useEffect(() => {
    const clamp = () => setSideW(width => clampAssistantWidth(width, compactAssistant))
    clamp()
    window.addEventListener('resize', clamp)
    return () => window.removeEventListener('resize', clamp)
  }, [compactAssistant])

  // ── sessions (full view) ──
  const openSession = (id, options = {}) => {
    if (directConfirmRef.current) {
      if (options.observeOnly === true) {
        clearDirectConfirmation()
        flash('Run-command confirmation canceled · draft kept')
      } else {
        flash('Cancel or confirm the current run command before switching chats')
        return Promise.resolve({ ok: false, sessionId: id, reason: 'direct-confirmation' })
      }
    }
    if (options.attentionHandoff !== true) cancelAttentionPermissionHandoff()
    if (turnCaptureRef.current && !sidRef.current) {
      if (!options.observeOnly) flash('Wait for the new Assistant chat to finish starting')
      return Promise.resolve({ ok: false, sessionId: id, reason: 'turn-starting' })
    }
    const matchingRead = openSessionPendingRef.current
    if (matchingRead && String(matchingRead.id) !== String(id)
        && String(sidRef.current) === String(id)) {
      // Re-selecting current A (including an observational Attention handoff) owns the choice over
      // an older A -> B read. Without this, B can replace A after the user already chose to stay.
      ++openSessionSeqRef.current
      openSessionPendingRef.current = null
      setOpeningSid(null)
    }
    const requestedAllowsRecovery = options.observeOnly !== true
    if (matchingRead && String(matchingRead.id) === String(id)
        && (matchingRead.allowsRecovery || !requestedAllowsRecovery)) return matchingRead.promise
    let sessionRead = null
    const operation = (async () => {
    const { recover = false, observeOnly = false } = options
    // Re-opening the session that is ALREADY live-streaming would abort its own stream (and downgrade
    // to polling). An observational caller may still read a durable snapshot without replacing it.
    const observingLiveSession = observeOnly && id === sidRef.current && runningRef.current
    const currentNeedsRecovery = id === sidRef.current && !!danglingAssistantTurn(msgs)
    if (id === sidRef.current && !observeOnly && (!recover || runningRef.current)
        && !currentNeedsRecovery) {
      // Re-selecting current A while a slow switch to B is pending means "stay on A". Supersede B
      // without launching a redundant A GET that could later abort a new A send. Explicit idle
      // recovery is the one path that deliberately reloads the current transcript.
      if (openSessionPendingRef.current) {
        ++openSessionSeqRef.current
        openSessionPendingRef.current = null
        setOpeningSid(null)
      }
      return { ok: true, sessionId: id, messages: null, loaded: false }
    }
    const seq = observingLiveSession ? openSessionSeqRef.current : ++openSessionSeqRef.current
    sessionRead = observingLiveSession ? null
      : { seq, id, allowsRecovery: !observeOnly, promise: null }
    if (sessionRead) {
      clearReplyAnnouncement()
      openSessionPendingRef.current = sessionRead
      setOpeningSid(String(id))
    }
    const shareMetaRead = beginShareMetaRead(id)
    try {
      // Keep the current transcript intact until the target is known to exist. The sequence fence
      // rejects a slow A response after the user has already selected B (or started a new chat).
      const s = await boundedRequest(signal => assistantGet(id, { signal }))
      if (!mountedRef.current || seq !== openSessionSeqRef.current
          || sessionDeleteTombstonesRef.current.has(String(id))
          || (sessionRead && openSessionPendingRef.current !== sessionRead)) {
        return { ok: false, sessionId: id, reason: 'superseded' }
      }
      const arr = s.messages == null ? [] : s.messages
      if (!Array.isArray(arr)) throw new Error('Invalid Assistant session transcript')
      // assistant_get carries authoritative public-link terms, so compact views can warn about a
      // live link immediately instead of briefly treating an unloaded session list as private.
      applyAssistantShareMeta(id, s.meta, shareMetaRead)
      if (observingLiveSession) {
        if (sidRef.current !== id) return { ok: false, sessionId: id, reason: 'superseded' }
        return { ok: true, sessionId: id, messages: arr, loaded: true }
      }
      // Accepting a different session invalidates every callback owned by the departed attempt. SID
      // checks alone are insufficient for A -> B -> A or Stop -> Send races because the same id can
      // become current again while an older GET/finally is still resolving.
      activeReplyAttemptRef.current = null
      turnCaptureRef.current = false
      setTurnStarting(false)
      setRetryChecking(false)
      clearReplyAnnouncement()
      if (abortRef.current) { try { abortRef.current.abort() } catch { /* gone */ } abortRef.current = null }
      if (runningRef.current) { runningRef.current = false; setBusy(false); setPending([]) }
      activateComposer(id, { seedMode: s.meta?.mode })
      sidRef.current = id; setSid(id); setMsgs([]); setPreview('')
      setHasNew(false)
      storageSet('ll.asstSid', id)
      setMsgs(arr)
      const la = [...arr].reverse().find(m => m.role === 'assistant' && m.content)
      if (la) setPreview(previewText(la.content))
      // Reattach to a live worker after reload/session switching. If the durable transcript instead ends
      // in a staged user turn and this process has no worker, the guarded path below replays that exact
      // raw/display/mode identity once and polls until its reply lands. The placeholder is UI-only: it
      // never appends a second user turn or creates a fresh mutation namespace.
      const dangling = danglingAssistantTurn(arr)
      let prog = { active: false, steps: [] }
      let progressKnown = false
      if (dangling) {
        try {
          prog = await boundedRequest(signal => assistantProgress(id, { signal }), 8000)
          progressKnown = true
        } catch { /* offline: observe only */ }
      }
      if (!mountedRef.current || seq !== openSessionSeqRef.current || sidRef.current !== id
          || sessionDeleteTombstonesRef.current.has(String(id))
          || (sessionRead && openSessionPendingRef.current !== sessionRead)) {
        return { ok: false, sessionId: id, reason: 'superseded' }
      }
      // Reattach only when the durable transcript identifies the exact pending user turn. Progress
      // without that identity is observable server activity, not authority to bind an arbitrary reply.
      deferredRecoverySessionRef.current = dangling && !observeOnly && historicalRef.current
        ? String(id) : null
      const inFlight = !!dangling && !historicalRef.current && (prog.active || !observeOnly)
      if (inFlight && mountedRef.current && sidRef.current === id) {
        const reattachAttempt = {}
        activeReplyAttemptRef.current = reattachAttempt
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
          if (historicalRef.current || observeOnly || !dangling || exactState !== 'idle'
              || !replyAttemptCurrent(reattachAttempt, id)) return
          exactState = 'checking'
          let latest
          const latestShareMetaRead = beginShareMetaRead(id)
          try {
            latest = await boundedRequest(signal => assistantGet(id, { signal }))
          } catch {
            if (replyAttemptCurrent(reattachAttempt, id)) exactState = 'idle'
            return
          }
          if (historicalRef.current || !replyAttemptCurrent(reattachAttempt, id)) return
          const latestTurn = danglingAssistantTurn(latest.messages || [])
          if (!latestTurn) { exactState = 'settled'; return } // the original reply won the race
          if (latestTurn.turn_id !== dangling.turn_id) {
            exactState = 'settled'; exactFailure = unavailableAssistantRecovery; return
          }
          const recovery = assistantRecoveryPayload(latestTurn)
          if (!recovery) { exactState = 'settled'; exactFailure = unavailableAssistantRecovery; return }
          // The exact retry acknowledges only capabilities observed on this fresh assistant_get. A
          // missing/malformed list is not equivalent to "private": pause without issuing the POST.
          const shareMetaOutcome = applyAssistantShareMeta(id, latest.meta, latestShareMetaRead)
          if (shareMetaOutcome === undefined) { exactState = 'idle'; return }
          if (!shareMetaOutcome) {
            exactState = 'settled'
            exactFailure = assistantLiveShareRecoveryFailure
            setShareAckNotice({ sid: id,
              message: 'Saved turn not retried · live public-link status could not be verified.' })
            refreshSessions()
            return
          }
          const acknowledgedLiveShareIds = assistantLiveShareIds(latest.meta)
          exactState = 'posted'
          recoveryCtrl = new AbortController(); abortRef.current = recoveryCtrl
          // Re-read above before POST: if the old worker persisted its reply after our first GET, this
          // path observes it instead of accidentally appending a fresh duplicate turn.
          assistantMessageStream(id, recovery.instruction, recovery.mode, {},
            recoveryCtrl.signal, recovery.display, acknowledgedLiveShareIds).catch(error => {
            if (!replyAttemptCurrent(reattachAttempt, id)) return
            if (assistantLiveShareAckRequired(error)) {
              exactState = 'settled'
              exactFailure = assistantLiveShareRecoveryFailure
              if (replyAttemptCurrent(reattachAttempt, id)) {
                setShareUnknown(id, true)
                setShareAckNotice({ sid: id,
                  message: 'Saved turn not retried · live public-link state changed. Verify it, then retry.' })
                refreshSessions()
                refreshSessionShareMeta(id)
              }
              return
            }
            exactFailure = assistantRecoveryFailure(error)
          })
        }
        if (progressKnown && !prog.active) startExactRecovery()
        ;(async () => {
          while (polling && replyAttemptCurrent(reattachAttempt, id)) {
            await sleep(1200)
            if (!replyAttemptCurrent(reattachAttempt, id)) break
            try {
              const pp = await assistantProgress(id)
              if (!replyAttemptCurrent(reattachAttempt, id)) break
              if (!pp.active) {
                if (observeOnly) break
                // The server may have been offline for the first progress read, or an attached worker
                // may have died. Re-check the durable transcript before the one allowed recovery POST.
                if (dangling && exactState === 'idle') startExactRecovery()
                if (!dangling) break
                continue
              }
              if (replyAttemptCurrent(reattachAttempt, id) && ((pp.steps || []).length || pp.text))
                patchLast(prev => prev && prev.role === 'assistant' && prev.streaming   // only the live placeholder
                  ? { ...(pp.text ? { content: pp.text } : {}),
                      activity: (pp.steps || []).length ? [{ type: 'tools', labels: pp.steps }] : prev.activity } : prev)
              // A reattached turn may be PARKED on a HITL confirm — surface its card too (the send
              // path polls permissions; without this a reload hides the card until the 900s deny).
              const permissionSnapshot = await readPermissionSnapshot(id)
              if (permissionSnapshot.ok && replyAttemptCurrent(reattachAttempt, id)) {
                setPending(permissionSnapshot.pending)
              }
            } catch { /* transient */ }
          }
        })()
        recoverReply(id, arr.length + 1, dangling, reattachAttempt, () => exactFailure).then(ok => {
          // If recovery gives up (the worker died / no reply ever lands), end the placeholder — else it
          // renders a forever "thinking" spinner (the send path has the same fallback on failure).
          if (!ok && replyAttemptCurrent(reattachAttempt, id)) {
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
          if (activeReplyAttemptRef.current === reattachAttempt) {
            activeReplyAttemptRef.current = null
            turnCaptureRef.current = false
            runningRef.current = false
            if (mountedRef.current && sidRef.current === id) { setBusy(false); setPending([]) }
            if (abortRef.current === recoveryCtrl) abortRef.current = null
          }
        })
      }
      return { ok: true, sessionId: id, messages: arr, loaded: true }
    } catch (e) {
      if (!mountedRef.current || seq !== openSessionSeqRef.current
          || sessionDeleteTombstonesRef.current.has(String(id))
          || (sessionRead && openSessionPendingRef.current !== sessionRead)) {
        return { ok: false, sessionId: id, reason: 'superseded' }
      }
      // The stored/opened session no longer exists (deleted here or in another tab, run-root reset).
      // Don't leave the dead id in `sid`/localStorage — that wedges the chat (every send targets the
      // 404'd session). Drop it back to a fresh composer.
      if (e?.status === 404 && (!sidRef.current || sidRef.current === id)) {
        const preserveAttentionHandoff = options.attentionHandoff === true
        newChat({ preserveAttentionHandoff })
        if (!preserveAttentionHandoff) flash('This Assistant chat no longer exists')
      } else flash(e?.status === 404 ? 'This Assistant chat no longer exists' : 'Could not open this Assistant chat')
      return { ok: false, sessionId: id, status: e?.status || null }
    } finally {
      if (sessionRead && openSessionPendingRef.current === sessionRead) {
        openSessionPendingRef.current = null
        if (mountedRef.current) setOpeningSid(null)
      }
    }
    })()
    if (sessionRead) sessionRead.promise = operation
    return operation
  }
  openSessionRef.current = openSession
  const onAttentionPermissionFocused = React.useCallback((requestId, token) => {
    const focus = attentionPermissionFocusRef.current
    if (token !== attentionPermissionHandoffRef.current
        || attentionPermissionReceiptRef.current === token
        || focus.phase !== 'ready' || focus.id !== requestId || focus.token !== token) return
    attentionPermissionReceiptRef.current = token
    try {
      window.dispatchEvent(new CustomEvent('ll:assistant-permission-opened', {
        detail: { requestId },
      }))
    } catch { /* the actionable card is already visible; polling keeps Attention consistent */ }
    setAttentionPermissionFocus(current => current.token === token
      ? { session: '', id: '', token, phase: 'idle' } : current)
  }, [setAttentionPermissionFocus])
  const onAttentionPermissionFocusFailed = React.useCallback((requestId, token) => {
    const focus = attentionPermissionFocusRef.current
    if (token !== attentionPermissionHandoffRef.current
        || focus.phase !== 'ready' || focus.id !== requestId || focus.token !== token) return
    setAttentionPermissionFocus({ session: '', id: '', token, phase: 'idle' })
    flash('Could not focus this approval. It remains in Attention; reopen it to try again.')
  }, [flash, setAttentionPermissionFocus])
  // The Attention Center can reveal an existing permission card, but opening attention must remain
  // observational: never let this navigation gesture recover/replay a dangling Assistant turn.
  useEffect(() => {
    if (hidden) return undefined
    let active = true
    const handoffCurrent = (handoff, session, sessionChoiceSeq = null) => active
      && attentionPermissionHandoffRef.current === handoff
      && (session == null || sidRef.current === session)
      && (sessionChoiceSeq == null || openSessionSeqRef.current === sessionChoiceSeq)
    const clearHandoff = handoff => setAttentionPermissionFocus(current => current.token === handoff
      ? { session: '', id: '', token: handoff, phase: 'idle' } : current)
    const focusFallback = (handoff, session, sessionChoiceSeq, preferFeed = false,
      releaseHandoff = false) => {
      requestAnimationFrame(() => {
        if (!active || attentionPermissionHandoffRef.current !== handoff) return
        if (!handoffCurrent(handoff, session, sessionChoiceSeq)) {
          clearHandoff(handoff)
          return
        }
        if (releaseHandoff) clearHandoff(handoff)
        const composer = inputRef.current
        const target = !preferFeed && !historicalRef.current && composer && !composer.disabled
          ? composer : feedRef.current
        target?.focus({ preventScroll: true })
      })
    }
    const onOpenAttentionSession = async event => {
      const session = event?.detail?.session
      const requestId = event?.detail?.requestId
      if (typeof session !== 'string' || !/^[0-9a-f]{16}$/.test(session)
          || typeof requestId !== 'string' || !/^[0-9a-f]{16}$/.test(requestId)) {
        flash('The Assistant approval link is no longer valid; refresh the Attention Center')
        return
      }
      const handoff = attentionPermissionHandoffRef.current + 1
      attentionPermissionHandoffRef.current = handoff
      setAttentionPermissionFocus({ session, id: requestId, token: handoff, phase: 'loading' })
      setAssistantView('side'); setHasNew(false)
      // Always claim the session choice. Even when A is already current this invalidates an older
      // pending A -> B selection, while observeOnly forbids Assistant turn recovery/replay.
      const openResult = await openSessionRef.current?.(session, {
        observeOnly: true, attentionHandoff: true,
      })
      if (!active || attentionPermissionHandoffRef.current !== handoff) return
      const sessionChoiceSeq = openSessionSeqRef.current
      if (!openResult?.ok || sidRef.current !== session) {
        if (openResult?.reason === 'superseded') {
          clearHandoff(handoff)
          return
        }
        flash('Could not open the Assistant session that owns this approval')
        focusFallback(handoff, null, sessionChoiceSeq, true, true)
        return
      }
      const permissionResult = await readPermissionSnapshot(session, { fresh: true })
      if (!handoffCurrent(handoff, session, sessionChoiceSeq)) {
        clearHandoff(handoff)
        return
      }
      if (!permissionResult.ok) {
        if (permissionResult.reason === 'superseded') {
          clearHandoff(handoff)
          return
        }
        flash('Could not load this approval. It remains in Attention; try again.')
        focusFallback(handoff, session, sessionChoiceSeq, false, true)
        return
      }
      const nextPending = permissionResult.pending
      setPending(nextPending)
      const target = nextPending.find(request => request?.id === requestId)
      if (!target) {
        flash('This Assistant approval is no longer pending.')
        focusFallback(handoff, session, sessionChoiceSeq, false, true)
        return
      }
      if (historicalRef.current) {
        flash(`${readOnlyActionRef.current}. Return to live, then reopen this approval from Attention.`)
        focusFallback(handoff, session, sessionChoiceSeq, true, true)
        return
      }
      setAttentionPermissionFocus({ session, id: requestId, token: handoff, phase: 'ready' })
    }
    window.addEventListener('ll:open-assistant-session', onOpenAttentionSession)
    return () => {
      active = false
      const cancelledHandoff = attentionPermissionHandoffRef.current
      attentionPermissionHandoffRef.current = cancelledHandoff + 1
      clearHandoff(cancelledHandoff)
      window.removeEventListener('ll:open-assistant-session', onOpenAttentionSession)
    }
  }, [hidden, readPermissionSnapshot]) // openSessionRef always points at the current render
  const releaseOrphanedLaunchRecovery = (recovery, reason) => {
    openFull()
    const current = listLaunchTransports().find(item => item.identity === recovery.identity
      || (recovery.storageKey && item.storageKey === recovery.storageKey))
    if (!current) {
      flash('Startup recovery changed in another view and was retained. Open Check startup again if it remains visible.')
      return
    }
    const stored = current.invalid ? null : loadLaunchTransport(current.identity)
    if (!current.invalid && (!stored || stored.invalid)) {
      flash('Startup recovery changed while it was being inspected and was retained. Open it again.')
      return
    }
    const orphanDescription = reason === 'missing-session'
      ? 'The Assistant chat that owns this startup recovery no longer exists'
      : reason === 'missing-proposal'
        ? 'The exact Assistant proposal that owns this startup recovery no longer exists in the saved chat'
        : 'This startup recovery cannot be matched to an Assistant session'
    const recordDescription = current.invalid
      ? 'The current local recovery record is damaged and cannot be checked automatically.'
      : `The current local recovery is for "${stored.runId}".`
    if (typeof window === 'undefined') {
      flash('Startup recovery retained because explicit confirmation is unavailable.')
      return
    }
    const confirmed = window.confirm(`${orphanDescription}. ${recordDescription} Removing it only releases this tab's local Start fence; it does not prove that no provider work or cost occurred. Inspect the run list and provider activity before any new Start. Have you inspected them and want to remove this exact local recovery record?`)
    if (!confirmed) {
      flash('Startup recovery retained. Inspect the run list and provider activity before any new Start.')
      return
    }
    const cleared = current.invalid
      ? clearDamagedLaunchTransport(current.storageKey)
      : clearLaunchTransport(current.identity, undefined, stored)
    flash(cleared
      ? 'Local startup recovery released after explicit inspection acknowledgement. The original outcome is still not proven.'
      : 'Startup recovery changed while it was being reviewed and was retained. Open it again.')
  }
  const openLaunchRecovery = async () => {
    const recovery = launchRecoveries[0]
    if (!recovery) return
    const recoverySession = launchDraftSession(recovery.identity)
    if (!recoverySession) {
      releaseOrphanedLaunchRecovery(recovery, 'invalid-session')
      return
    }
    const opened = await openSession(recoverySession, { observeOnly: true })
    if (opened?.status === 404) {
      releaseOrphanedLaunchRecovery(recovery, 'missing-session')
      return
    }
    if (!opened?.ok) return
    if (!messagesOwnLaunchIdentity(opened.messages, opened.sessionId, recovery.identity)) {
      releaseOrphanedLaunchRecovery(recovery, 'missing-proposal')
      return
    }
    if (sidRef.current === recoverySession) {
      openSide()
      if (recovery.invalid) flash('This startup recovery record is damaged. Review the proposal recovery warning before any new Start.')
    } else flash('Could not reopen the Assistant session that owns this startup recovery.')
  }
  // Restore the last session on mount so a full page reload never loses the conversation — and if its
  // last turn has no reply yet, recover the in-flight answer (the fix for "typed in the bar, reloaded,
  // got no response"). The reply is persisted server-side only when the worker finishes (server.py).
  useEffect(() => {
    let last = null
    last = storageGet('ll.asstSid')
    if (last) openSession(last, { recover: true })
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (historical) return
    const deferred = deferredRecoverySessionRef.current
    if (!deferred || String(sidRef.current || '') !== deferred
        || runningRef.current || openSessionPendingRef.current) return
    deferredRecoverySessionRef.current = null
    openSession(deferred, { recover: true })
  }, [historical, sessionOpening])   // eslint-disable-line react-hooks/exhaustive-deps
  // Attach a node to the chat context from anywhere (a node card / the Inspector dispatches
  // `ll:attach-node`): append `#<id>` to the composer (deduped), reveal the assistant, and focus.
  useEffect(() => {
    const onAttach = (e) => {
      const id = e?.detail?.id
      if (id == null) return
      if (openSessionPendingRef.current) {
        flash('Wait for the selected Assistant chat to finish opening before attaching context')
        return
      }
      setInput(prev => new RegExp(`#(?:node-)?${id}\\b`, 'i').test(prev) ? prev
        : (prev.trim() ? prev.replace(/\s*$/, ' ') : '') + `#${id} `)
      setAssistantView(v => (v === 'bar' && hasChat) ? 'side' : v)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
    window.addEventListener('ll:attach-node', onAttach)
    return () => window.removeEventListener('ll:attach-node', onAttach)
  }, [hasChat])
  // Contextual empty/recovery states can reveal and prefill the persistent Assistant without
  // submitting anything. The user still reviews and explicitly sends the exact command.
  useEffect(() => {
    const onFocusAssistant = event => {
      if (openSessionPendingRef.current) {
        flash('Wait for the selected Assistant chat to finish opening before changing the draft')
        return
      }
      const text = String(event?.detail?.text || '').trim()
      const draft = input.trim()
      if (text && (!draft || draft === text)) setInput(text)
      else if (text) flash(`Draft preserved — clear it before inserting ${text}`)
      setAssistantView(value => value === 'bar' && hasChat ? 'side' : value)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
    window.addEventListener('ll:focus-assistant', onFocusAssistant)
    return () => window.removeEventListener('ll:focus-assistant', onFocusAssistant)
  }, [hasChat, input])
  const newChat = ({ preserveAttentionHandoff = false } = {}) => {
    if (directConfirmRef.current) {
      flash('Cancel or confirm the current run command before starting another chat')
      return
    }
    if (!preserveAttentionHandoff) cancelAttentionPermissionHandoff()
    ++openSessionSeqRef.current
    openSessionPendingRef.current = null
    deferredRecoverySessionRef.current = null
    setOpeningSid(null)
    activeReplyAttemptRef.current = null
    turnCaptureRef.current = false
    setTurnStarting(false)
    setRetryChecking(false)
    clearReplyAnnouncement()
    if (abortRef.current) { try { abortRef.current.abort() } catch { /* gone */ } abortRef.current = null }
    // the departing turn's finally is sid-guarded — reset the shared flags here (see openSession)
    runningRef.current = false; setBusy(false); setPending([])
    sidRef.current = null; setSid(null); setMsgs([])
    setPreview(''); setHasNew(false)
    activateComposer(NEW_CHAT_COMPOSER_KEY, { clear: true })
    storageRemove('ll.asstSid')
  }
  const deleteSessionBlock = (id) => {
    const unresolved = listLaunchTransports()
      .find(item => launchDraftSession(item.identity) === String(id))
    if (unresolved) {
      return {
        message: `Check or release startup recovery${unresolved.runId ? ` for “${unresolved.runId}”` : ''} before deleting this chat`,
        revealRecovery: sidRef.current === id,
      }
    }
    if (shareActionSessionRef.current === id) {
      return { message: 'Wait for the current share action before deleting this chat' }
    }
    if (forkActionSessionRef.current === id) {
      return { message: 'Wait for this chat to finish forking before deleting it' }
    }
    if (String(openSessionPendingRef.current?.id || '') === String(id)) {
      return { message: 'Wait for this chat to finish opening before deleting it' }
    }
    if (id === sidRef.current && (turnCaptureRef.current || runningRef.current
        || busy || pending.length > 0)) {
      return { message: 'Stop or finish the current Assistant turn before deleting this chat' }
    }
    const listedSession = sessions.find(session => session.id === id)
    if (listedSession?.shared || shareUnknownSids.has(id) || shareCopyFallbacks[id]) {
      return { message: 'Unshare this chat before deleting it so every public link is explicitly revoked' }
    }
    if (deletingSessionsRef.current.has(id)) return { duplicate: true }
    return null
  }
  const reportDeleteSessionBlock = block => {
    if (!block) return
    if (block.message) flash(block.message)
    if (block.revealRecovery) openSide()
  }
  const requestDeleteSession = (session, e) => {
    e?.stopPropagation()
    const row = e?.currentTarget?.closest('.asst-sess')
    const block = deleteSessionBlock(session.id)
    if (block) {
      reportDeleteSessionBlock(block)
      return
    }
    deleteTriggerRef.current = e?.currentTarget || null
    deleteFocusAfterRef.current = row?.nextElementSibling?.querySelector('.asst-sess-open:not(:disabled)')
      || row?.previousElementSibling?.querySelector('.asst-sess-open:not(:disabled)')
      || document.querySelector('.asst-side-h .btn.primary')
    setDeleteConfirm({ id: session.id, title: session.title || 'Chat' })
    setDeleteConfirmError('')
  }
  const confirmDeleteSession = async () => {
    const target = deleteConfirm
    if (!target || deleteConfirmBusyRef.current) return
    const { id } = target
    const block = deleteSessionBlock(id)
    if (block) {
      closeDeleteConfirm()
      reportDeleteSessionBlock(block)
      return
    }
    cancelAttentionPermissionHandoff()
    deleteConfirmBusyRef.current = true
    setDeleteConfirmBusy(true)
    setDeleteConfirmError('')
    deletingSessionsRef.current.add(id)
    rememberBoundedMapValue(sessionDeleteTombstonesRef.current, String(id), 'pending')
    const pendingOpen = openSessionPendingRef.current
    if (pendingOpen && String(pendingOpen.id) === String(id)) {
      ++openSessionSeqRef.current
      openSessionPendingRef.current = null
      setOpeningSid(null)
    }
    const deletedIndex = sessions.findIndex(session => session.id === id)
    const deletedSession = deletedIndex >= 0 ? sessions[deletedIndex] : null
    mutateSessionsLocally(ss => ss.filter(s => s.id !== id)) // optimistic: drop it now (geesefs list can lag)
    let deleted = false
    const acceptDeletedSession = () => {
      setLaunchDrafts(current => clearLaunchDraftSession(current, id))
      setLaunchDisclosures(current => clearLaunchDraftSession(current, id))
      composerDraftsRef.current.delete(id)
      setShareUnknown(id, false)
      clearShareCopy(id)
      if (id === storageGet('ll.asstSid')) storageRemove('ll.asstSid')
      if (id === sidRef.current) newChat()
      rememberBoundedMapValue(sessionDeleteTombstonesRef.current, String(id), 'confirmed')
      deleted = true
    }
    try {
      await boundedRequest(signal => assistantDelete(id, { signal }))
      acceptDeletedSession()
    } catch (error) {
      const requestWasAmbiguous = candidate => candidate?.name === 'TimeoutError'
        || candidate?.name === 'AbortError'
        || candidate?.status == null || Number(candidate.status) >= 500
      const ambiguous = requestWasAmbiguous(error)
      let finalError = error
      let finalAmbiguous = ambiguous
      // DELETE is idempotent for this exact session id. If the first response was lost, repeat the
      // same bounded operation: a successful second response proves the directory is now gone even
      // when the first request actually completed. A list omission cannot prove that — unreadable or
      // partially deleted session metadata is intentionally skipped by the list projection.
      if (ambiguous) {
        try {
          await boundedRequest(signal => assistantDelete(id, { signal }))
          acceptDeletedSession()
        } catch (retryError) {
          finalError = retryError
          finalAmbiguous = requestWasAmbiguous(retryError)
        }
      }
      let listedPresence = null
      if (finalAmbiguous && !deleted) {
        try {
          const payload = await boundedRequest(
            signal => assistantSessions({ signal, cache: 'no-store' }), 8000)
          listedPresence = Array.isArray(payload?.sessions)
            ? payload.sessions.some(session => session?.id === id) : null
        } catch { /* list truth is optional context; only DELETE can confirm removal */ }
      }
      if (!deleted) {
        sessionDeleteTombstonesRef.current.delete(String(id))
        if (deletedSession) mutateSessionsLocally(current => {
          if (current.some(session => session.id === id)) return current
          const restored = [...current]
          restored.splice(Math.min(deletedIndex, restored.length), 0, deletedSession)
          return restored
        })
        const shared = finalError?.code === 'assistant_delete_shared'
        const incomplete = finalError?.code === 'assistant_delete_incomplete'
        const stillStopping = finalError?.code === 'assistant_delete_busy'
        const forkInProgress = finalError?.code === 'assistant_delete_fork_in_progress'
        const message = shared
          ? 'This chat still has an active public link. Unshare it first, then delete the chat.'
          : forkInProgress
          ? 'This chat is still being forked. It is shown again; try deleting it after the fork finishes.'
          : stillStopping
          ? 'The live Assistant turn is still stopping. The chat is shown again; try deleting it again shortly.'
          : incomplete
          ? 'The server could not completely remove the chat. Close anything using its files and try again.'
          : finalAmbiguous && listedPresence === true
            ? 'Neither bounded deletion attempt was confirmed, and the chat still appears available. You can try again.'
            : finalAmbiguous
              ? 'Neither bounded deletion attempt was confirmed. The chat is shown again for safety; retry or refresh.'
              : 'The chat was not deleted. It is shown again; you can cancel or try again.'
        flash(shared ? 'Unshare this chat before deleting it'
          : forkInProgress ? 'Wait for this chat to finish forking before deleting it'
          : stillStopping ? 'Assistant work is still stopping'
          : incomplete ? 'Assistant chat was not completely removed' : 'Could not confirm chat deletion')
        if (mountedRef.current) setDeleteConfirmError(message)
      }
    }
    finally {
      deletingSessionsRef.current.delete(id)
      deleteConfirmBusyRef.current = false
      if (mountedRef.current) setDeleteConfirmBusy(false)
      // A pre-list local receipt fences the older list request. Start a fresh authoritative read so
      // deleting or restoring a known row cannot leave the session resource permanently "loading".
      if (mountedRef.current && !sessionsLoadedRef.current) refreshSessions()
      if (deleted && mountedRef.current) {
        const focusAfterDelete = deleteFocusAfterRef.current
        deleteTriggerRef.current = null
        deleteFocusAfterRef.current = null
        setDeleteConfirm(null)
        requestAnimationFrame(() => {
          const targetAfterDelete = focusAfterDelete?.isConnected
            ? focusAfterDelete
            : document.querySelector('.asst-sessions .asst-sess-open:not(:disabled), .asst-side-h .btn.primary')
          targetAfterDelete?.focus({ preventScroll: true })
        })
      }
    }
  }

  const resolvePerm = async (reqId, decision) => {
    if (historical) { flash(readOnlyAction); return }
    if (resolvingPermsRef.current.has(reqId)) return
    const requestOwner = pending.find(req => req.id === reqId)?.session
    const requestSession = typeof requestOwner === 'string' && /^[0-9a-f]{16}$/.test(requestOwner)
      ? requestOwner : sidRef.current
    if (!requestSession || requestSession !== sidRef.current) {
      setPending(current => current.filter(request => request?.id !== reqId))
      flash('This approval belongs to another Assistant chat; reopen it from Attention.')
      requestAnimationFrame(() => {
        const composer = inputRef.current
        const target = !historicalRef.current && composer && !composer.disabled
          ? composer : feedRef.current
        target?.focus({ preventScroll: true })
      })
      return
    }
    resolvingPermsRef.current.add(reqId)
    setResolvingPerms(current => new Set(current).add(reqId))
    try {
      await assistantResolve(reqId, decision)
      const resolvedSession = String(requestSession || '')
      const resolvedForSession = resolvedPermsRef.current.get(resolvedSession) || new Set()
      resolvedForSession.add(reqId)   // a lagging poll must not re-add this now-resolved card
      resolvedPermsRef.current.set(resolvedSession, resolvedForSession)
      if (sidRef.current === requestSession) setPending(current => {
        const next = current.filter(req => req.id !== reqId)
        requestAnimationFrame(() => {
          const target = next.length
            ? document.querySelector('.asst-perm-region .asst-perm-actions button:not([disabled])')
            : inputRef.current
          target?.focus({ preventScroll: true })
        })
        return next
      })
    } catch {
      flash('Could not confirm the permission decision; checking its current status')
      // The POST response may have been lost after the server committed the decision. Re-read the
      // registry before offering another click; on a true network outage the original card remains.
      try {
        const permissionChoiceSeq = openSessionSeqRef.current
        const latest = requestSession && sidRef.current === requestSession
          ? await readPermissionSnapshot(requestSession, { fresh: true }) : null
        if (latest?.ok && sidRef.current === requestSession
            && openSessionSeqRef.current === permissionChoiceSeq) setPending(latest.pending)
      } catch { /* retain the exact pending request for a later retry */ }
    } finally {
      resolvingPermsRef.current.delete(reqId)
      setResolvingPerms(current => {
        const next = new Set(current); next.delete(reqId); return next
      })
    }
  }
  const onRevert = async (change) => {
    if (historical) { flash(readOnlyAction); return }
    const key = assistantRevertKey(sidRef.current || sid, change)
    if (!key) { flash('This older file change has no exact undo receipt'); return }
    if (revertingChangesRef.current.has(key) || revertedChangesRef.current.has(key)) return
    revertingChangesRef.current.add(key)
    setRevertingChanges(current => new Set(current).add(key))
    try {
      await boundedRequest(signal => assistantRevert(change, { signal }))
      rememberBoundedSetValue(revertedChangesRef.current, key)
      if (mountedRef.current) {
        setRevertedChanges(new Set(revertedChangesRef.current))
        flash('Exact file change reverted')
      }
    } catch (error) {
      if (mountedRef.current) flash(error?.status === 409 || error?.code === 'assistant_revert_conflict'
          ? 'Undo stopped — the file changed or a newer recovery point now owns this path'
          : error?.name === 'TimeoutError' || error?.name === 'AbortError'
            ? 'Undo outcome was not confirmed — retrying the same receipt is safe'
            : 'Could not revert this file change')
    } finally {
      revertingChangesRef.current.delete(key)
      if (mountedRef.current) setRevertingChanges(current => {
          const next = new Set(current); next.delete(key); return next
        })
    }
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
    nodeGeneration: entry.nodeGeneration,
    expectedGeneration: entry.expectedGeneration,
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
      action: entry.name, expectedGeneration: entry.expectedGeneration, commandId: '',
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
    if (entry.protocolInvalid || !directSpec(name)) name = commandActionForEvent(record?.event_type)
    const spec = directSpec(name)
    if (!name || !spec || !commandRecordMatchesAction(record, name, 'assistant')) return null
    return { ...entry, name, spec, record, protocolInvalid: false,
      canResubmit: true, retrying: false }
  }
  const protocolDirect = (entry, record = entry.record, message = 'Invalid command response') => {
    const commandId = /^cmd_[0-9a-f]{32}$/.test(String(record?.id || '')) ? String(record.id) : ''
    const next = { ...entry, record: commandId ? { id: commandId, status: 'accepted' } : { status: 'submitting' },
      statusUnavailable: true, observationKind: 'protocol', checking: false, retrying: false,
      protocolInvalid: true, canResubmit: false, lastError: message }
    saveRunCommandLock(entry.runId, {
      source: 'assistant', action: next.name || 'unknown', idempotencyKey: next.idempotencyKey,
      expectedGeneration: next.expectedGeneration,
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
        action: entry.name || 'unknown', expectedGeneration: entry.expectedGeneration,
        commandId: entry.record?.id || '',
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
    if (recoveryRecord.id && !recoveryRecord.event_type && directSpec(entry.name)) {
      recoveryRecord = { ...recoveryRecord, event_type: commandEventForAction(entry.name, 'assistant') }
    }
    const next = { ...entry, record: recoveryRecord, statusUnavailable: true,
      observationKind: assistantDirectObservationKind(error), checking: false,
      lastError: 'Command status could not be verified.' }
    if (!persistDirect(next)) {
      saveRunCommandLock(entry.runId, { ...next, source: 'assistant' })
    }
    if (mountedRef.current) { setCurrentDirect(entry, next); setCurrentFailure(entry, null) }
    return next
  }
  const failDirectObservation = (entry, error) => {
    const record = commandFailureRecord(error, entry.record)
    if (record.error) { delete record.error.message; delete record.error.remediation }
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
  const executeDirect = async (entry, { recovery = false, onCommitted = null } = {}) => {
    let bound = entry
    if (!bound.expectedGeneration) {
      const error = new Error('The displayed run generation is unavailable.')
      error.code = 'run_generation_unavailable'
      error.remediation = 'Refresh the run before submitting another action.'
      failDirectObservation(bound, error)
      return
    }
    const submitting = { ...bound, record: { status: 'submitting' }, statusUnavailable: false,
      observationKind: null, checking: false, retrying: false, lastError: '' }
    if (!persistDirect(submitting)) { localStorageFailure(bound); return }
    try { onCommitted?.() } catch { /* durable intent still owns the action; presentation is best effort */ }
    if (mountedRef.current) { setCurrentDirect(bound, submitting); setCurrentFailure(bound, null) }
    try {
      const record = await submitAssistantDirect(bound.runId, bound.name, bound.arg,
        bound.idempotencyKey, {
          expectedGeneration: bound.expectedGeneration,
          nodeGeneration: bound.nodeGeneration,
          onRecord: next => {
            const verified = verifiedDirectEntry(bound, next)
            if (verified) persistDirect({ ...verified, statusUnavailable: false })
          },
        })
      acceptDirectRecord(bound, record, { announce: !recovery || COMMAND_SUCCEEDED.has(record.status) || COMMAND_FAILED.has(record.status) })
    } catch (error) {
      const record = error?.commandRecord || (error?.commandId
        ? { id: error.commandId, status: 'accepted' } : null)
      const kind = assistantDirectObservationKind(error)
      if (error?.commandUnknown || error?.submissionMayHaveSucceeded
          || (record?.id && ['transport', 'access', 'protocol'].includes(kind))) {
        unavailableDirect(bound, error, record)
        flashDirect(bound, `/${bound.name}: status unavailable; intent preserved`)
      } else if (error?.code === 'run_generation_changed'
          || error?.code === 'invalid_run_generation'
          || error?.code === 'run_generation_unavailable') {
        // This is an authoritative precondition failure, not an ambiguous transport result. Keep the
        // exact bound token in the settled envelope so reload cannot turn it into a fresh command.
        failDirectObservation(bound, error)
      } else {
        // A cross-action 409 may reference another command. It is remediation context only, never the
        // durable id of this requested action.
        clearAssistantRunTransport(bound.runId, undefined, { idempotencyKey: bound.idempotencyKey })
        if (mountedRef.current) setCurrentDirect(bound, null)
        const conflict = error?.existingCommandId ? ` (active command ${String(error.existingCommandId).slice(0, 12)}…)` : ''
        const record = commandFailureRecord(error)
        if (record.error) { delete record.error.message; delete record.error.remediation }
        const failure = { ...bound, record, retrying: false }
        if (mountedRef.current) setCurrentFailure(bound, failure)
        flashDirect(bound, `/${bound.name} failed${conflict}`)
      }
    }
  }
  const runDirect = async (d, binding = {}) => {
    const directRunId = binding.runId || runId
    if (!directRunId) { flash(`/${d.name} needs an open run`); return false }
    if (String(currentRunIdRef.current ?? '') !== String(directRunId)) {
      flash(`/${d.name} not sent because the open run changed`); return false
    }
    if (historicalRef.current) { flash(readOnlyAction); return false }
    if (openSessionPendingRef.current) {
      flash('Wait for the selected Assistant chat to finish opening'); return false
    }
    if (turnCaptureRef.current || runningRef.current) {
      flash('Assistant is already starting or responding'); return false
    }
    // Read synchronously as well as using React state: two rapid clicks in one batch must not create
    // competing intents before the lock event has caused a render.
    if (directCaptureRef.current || loadRunCommandLock(directRunId)
        || (loadAssistantRunTransport(directRunId) && !directFailure)) {
      flash('Recover the stored run command before starting another'); return false
    }
    const observedGeneration = normalizeRunGeneration(getObservedRunGeneration(directRunId))
    const expectedGeneration = binding.expectedGeneration || observedGeneration
    if (!expectedGeneration) {
      flash(`/${d.name} not sent because the run generation is not verified`); return false
    }
    if (binding.expectedGeneration && observedGeneration !== binding.expectedGeneration) {
      flash(`/${d.name} not sent because the run generation changed`); return false
    }
    if (directFailure) clearAssistantRunTransport(directFailure.runId, undefined, {
      idempotencyKey: directFailure.idempotencyKey,
    })
    directCaptureRef.current = true
    commandFocusRequestedRef.current = true
    setDirectStarting(true)
    try { binding.onClaimed?.() } catch { /* UI callback cannot widen or duplicate the command */ }
    try {
      let nodeGeneration = Number.isSafeInteger(binding.nodeGeneration)
        ? binding.nodeGeneration : null
      if (d.name === 'approve' && nodeGeneration == null) {
        const payload = await boundedRequest(
          signal => get(runApiPath(directRunId, '/state'), { signal }), 8000)
        if (String(currentRunIdRef.current ?? '') !== String(directRunId)) {
          flash(`/${d.name} not sent because the open run changed`)
          return
        }
        if (historicalRef.current) { flash(readOnlyAction); return }
        const target = pendingApprovalTarget(payload?.state)
        if (!target) throw new Error('Approval target changed; inspect Events')
        if (target.nodeId !== d.arg) {
          throw new Error(`The active approval is for experiment #${target.nodeId}, not #${d.arg}`)
        }
        const fetchedGeneration = normalizeRunGeneration(payload?.generation)
        if (!fetchedGeneration) throw new Error('Run generation unverified; refresh before approving')
        if (fetchedGeneration !== expectedGeneration) {
          throw new Error('Run generation changed before approval; inspect Events and retry')
        }
        nodeGeneration = target.nodeGeneration
      }
      const entry = { ...d, runId: directRunId, nodeGeneration,
        expectedGeneration,
        idempotencyKey: createIdempotencyKey(), record: { status: 'submitting' } }
      commandFocusRequestedRef.current = true
      setCurrentFailure(entry, null)
      setDirectPending(entry)
      await executeDirect(entry, { onCommitted: binding.onCommitted })
    } catch {
      flash(`/${d.name} could not start; refresh and retry`)
      requestAnimationFrame(() => inputRef.current?.isConnected
        && inputRef.current.focus({ preventScroll: true }))
    } finally {
      directCaptureRef.current = false
      if (mountedRef.current) setDirectStarting(false)
    }
    return true
  }
  const checkDirect = async () => {
    const entry = directPending
    if (!entry || entry.checking) return
    if (historicalRef.current) { flash(readOnlyAction); return }
    if (directCaptureRef.current || turnCaptureRef.current || runningRef.current
        || openSessionPendingRef.current) {
      flashDirect(entry, 'Wait for the current Assistant action before checking this command')
      return
    }
    directCaptureRef.current = true
    try {
    commandFocusRequestedRef.current = true
    const checking = { ...entry, checking: true }
    if (!entry.protocolInvalid) persistDirect(checking)
    setDirectPending(checking)
    if (!entry.record?.id) {
      if (entry.canResubmit === false || entry.protocolInvalid) {
        const next = { ...entry, checking: false, statusUnavailable: true, observationKind: 'protocol' }
        setDirectPending(next)
        flashDirect(entry, 'Stored command identity is invalid; dismiss it')
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
        protocolDirect(entry, entry.record, 'Stored command could not be verified')
      } else if (kind === 'transport' || kind === 'access' || kind === 'protocol') {
        unavailableDirect(entry, error, entry.record)
      } else failDirectObservation(entry, error)
    }
    } finally { directCaptureRef.current = false }
  }
  const retryDirect = async () => {
    const failure = directFailure
    if (!failure || directPending || !commandCanRetry(failure.record)) return
    if (historicalRef.current) { flash(readOnlyAction); return }
    if (directCaptureRef.current || turnCaptureRef.current || runningRef.current
        || openSessionPendingRef.current) {
      flashDirect(failure, 'Wait for the current Assistant action before retrying this command')
      return
    }
    if (loadRunCommandLock(failure.runId)) {
      flashDirect(failure, 'Another run command is pending; retry later')
      return
    }
    const retrying = { ...failure, retrying: true, checking: false, statusUnavailable: false }
    if (!persistDirect(retrying)) {
      flashDirect(failure, 'Retry not sent; recovery storage unavailable')
      return
    }
    commandFocusRequestedRef.current = true
    setCurrentFailure(failure, null); setDirectPending(retrying)
    directCaptureRef.current = true
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
        flashDirect(failure, `Retry failed.${conflict}`)
      }
    } finally {
      directCaptureRef.current = false
    }
  }
  const dismissDirectFailure = () => {
    const failure = directFailure
    if (!failure) return
    if (failure.record?.error?.code === 'command_storage_unavailable') {
      if (!clearUnsentDirectRecovery(failure)) {
        flashDirect(failure, 'Recovery storage unavailable; unsent command remains quarantined')
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
    if (directCaptureRef.current) {
      flashDirect(pending, 'Wait for the current command check before dismissing recovery')
      return
    }
    clearAssistantRunTransport(pending.runId, undefined, { idempotencyKey: pending.idempotencyKey })
    const identity = pending.lockIdentity || {
      source: 'assistant', idempotencyKey: pending.idempotencyKey,
      action: pending.name || 'unknown', expectedGeneration: pending.expectedGeneration,
      commandId: pending.record?.id || '',
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
        action: lock.action, expectedGeneration: lock.expectedGeneration, commandId: lock.commandId,
      })
      return
    }
    if (lock && lock.source !== 'assistant') {
      clearAssistantRunTransport(runId, undefined, { idempotencyKey: saved.idempotencyKey })
      return
    }
    const spec = directSpec(saved.action) || UNKNOWN_DIRECT_SPEC
    const lockMismatch = lock?.source === 'assistant' && (
      lock.idempotencyKey !== saved.idempotencyKey || lock.action !== saved.action
      || lock.expectedGeneration !== saved.expectedGeneration
      || (lock.commandId && saved.commandId && lock.commandId !== saved.commandId)
    )
    if (saved.protocolInvalid || lockMismatch) {
      const restored = restoreAssistantDirectEntry(saved, spec, runId, { record: saved.record,
        statusUnavailable: true, observationKind: 'protocol', checking: false, retrying: false,
        protocolInvalid: true, canResubmit: false,
        lockIdentity: lock?.source === 'assistant' ? lock : null })
      saveRunCommandLock(runId, { ...restored, source: 'assistant', action: restored.name })
      setDirectPending(restored)
      return
    }
    if (COMMAND_SUCCEEDED.has(saved.record?.status)) {
      clearAssistantRunTransport(runId, undefined, { idempotencyKey: saved.idempotencyKey })
      return
    }
    const restored = restoreAssistantDirectEntry(saved, spec, runId, {
      record: saved.record || (saved.commandId ? { id: saved.commandId, status: 'accepted' } : { status: 'submitting' }),
      statusUnavailable: !!saved.statusUnavailable, observationKind: saved.observationKind,
      checking: false, retrying: !!saved.retrying, lastError: '' })
    if (COMMAND_FAILED.has(saved.record?.status) && !saved.statusUnavailable
        && !saved.retrying && !saved.checking) {
      clearRunCommandLock(runId, {
        source: 'assistant', idempotencyKey: saved.idempotencyKey,
        action: saved.action, expectedGeneration: saved.expectedGeneration,
        commandId: saved.commandId,
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
    if (openSessionPendingRef.current) {
      flash('Wait for the selected Assistant chat to finish opening before attaching files')
      return
    }
    const ownerDraft = composerDraftRef.current
    const ownerSession = sidRef.current
    if (ownerSession && forkActionSessionRef.current === ownerSession) {
      flash('Wait for this Assistant chat to finish forking before attaching files')
      return
    }
    const ownerRunScope = composerRunKey(currentRunIdRef.current)
    const picked = [...(list || [])]
    const bad = picked.filter(f => !TEXT_EXT.test(f.name))
    if (bad.length) flash(`skipped non-text: ${bad.map(f => f.name).join(', ')}`)
    const txt = picked.filter(f => TEXT_EXT.test(f.name))
    const secret = txt.filter(f => SECRET_RE.test(f.name))          // don't slurp .env/keys into the prompt
    if (secret.length) flash(`skipped secret-looking: ${secret.map(f => f.name).join(', ')}`)
    const big = txt.filter(f => !SECRET_RE.test(f.name) && f.size > MAX_FILE_BYTES)   // OOM guard BEFORE read
    if (big.length) flash(`too large (>2MB): ${big.map(f => f.name).join(', ')}`)
    const ok = txt.filter(f => !SECRET_RE.test(f.name) && f.size <= MAX_FILE_BYTES)
    if (!ok.length) return
    const nextPending = Math.max(0, Number(ownerDraft.pendingFileReads) || 0) + 1
    if (ownerDraft.runScope == null && composerUsesRun(ownerDraft.input, ownerDraft.files, nextPending)) {
      ownerDraft.runScope = ownerRunScope
      if (composerDraftRef.current === ownerDraft) setDraftRunScope(ownerRunScope)
    }
    updatePendingFileReads(ownerDraft, nextPending)
    try {
      const read = (await Promise.all(ok.map(readFileText))).filter(Boolean)
      if (read.length) updateComposerFiles(ownerDraft, f => {   // dedup by name (React key + removeFile both key on name)
        const seen = new Set(f.map(x => x.name)); return [...f, ...read.filter(r => !seen.has(r.name))]
      }, ownerRunScope)
    } finally {
      updatePendingFileReads(ownerDraft, current => current - 1)
    }
  }
  const removeFile = (name) => {
    if (openSessionPendingRef.current) return
    setFiles(f => f.filter(x => x.name !== name))
  }
  const filePreamble = (fs) => fs.length
    ? '\n\n[Attached files — use their content as context]\n' + fs.map(f =>
        `--- ${f.name}${f.truncated ? ' (truncated)' : ''} ---\n${f.content}`).join('\n\n') + '\n'
    : ''

  // Recover a turn whose SSE stream dropped (a buffering proxy can kill a long-lived stream): the
  // background worker keeps running and persists the reply, so poll the session until the assistant
  // message lands, then surface it — instead of stranding the user on "could not reach".
  const completedAssistantReply = (messages, prior) => {
    if (!assistantReplyCompletesTurn(messages, prior)) return null
    const userIndex = assistantTurnIndex(messages, prior)
    return userIndex >= 0 ? messages[userIndex + 1] || null : null
  }
  const recoverReply = async (id, priorLen, prior, attempt, authoritativeFailure = null) => {
    if (!prior || !replyAttemptCurrent(attempt, id)) return false
    for (let i = 0; i < 180 && replyAttemptCurrent(attempt, id); i++) {   // ~6min > the 300s turn budget
      await sleep(2000)
      if (!replyAttemptCurrent(attempt, id)) return false
      if (authoritativeFailure?.()) return false
      try {
        const s = await boundedRequest(signal => assistantGet(id, { signal }), 8000)
        if (!replyAttemptCurrent(attempt, id)) return false
        const arr = s.messages || []
        const reply = arr.length >= priorLen ? completedAssistantReply(arr, prior) : null
        if (reply?.content) {
          setMsgs(arr)
          const ready = announceReplyReady(reply.content, { sessionId: id, turn: prior, attempt })
          setPreview(previewText(reply.content))
          if (ready) setHasNew(viewRef.current === 'bar')
          return true
        }
      } catch { /* keep polling */ }
    }
    return false
  }

  // Stream one instruction to the assistant. `userText` = the bubble shown; `instruction` = what the
  // model receives (run context + attached files appended, not shown in the bubble).
  const runLLM = async (instruction, { userText = null, ensureVisible = false, context = null,
    retryFiles = null, turnMode = null, clearComposer = false, acknowledgedShareMeta = null } = {}) => {
    if (historicalRef.current) { flash(readOnlyAction); return }
    if (openSessionPendingRef.current) {
      flash('Wait for the selected Assistant chat to finish opening')
      return
    }
    const guardedSid = sidRef.current || sid
    if (guardedSid && forkActionSessionRef.current === guardedSid) {
      flash('Wait for this Assistant chat to finish forking before sending')
      return
    }
    if (shareBusySid != null || shareActionSessionRef.current) {
      flash('Wait for the current public-link action before sending another turn')
      return
    }
    if (guardedSid && shareVerificationRef.current.sid === String(guardedSid)
        && shareVerificationRef.current.promise) {
      flash('Still checking public-link status · nothing sent')
      return
    }
    if (guardedSid && shareUnknownSids.has(guardedSid)) {
      flash('Nothing sent · checking public-link status')
      verifyShareStatus(guardedSid)
      return
    }
    if (turnCaptureRef.current || runningRef.current || directCaptureRef.current) {
      flash('Assistant or a run command is already starting'); return
    }
    let acknowledgedLiveShareIds = []
    if (guardedSid) {
      // A recovery check may have fetched newer metadata in this same event turn, before React can
      // publish setSessions. Prefer that exact GET receipt, but only for its matching session.
      const guardedSession = acknowledgedShareMeta != null
        ? (String(acknowledgedShareMeta?.id || '') === String(guardedSid)
            ? acknowledgedShareMeta : null)
        : sessions.find(session => String(session?.id || '') === String(guardedSid))
      if (!guardedSession || !validAssistantShareMeta(guardedSession)) {
        // Do not infer "no live links" from a missing/stale row. Verify the exact session and require
        // a deliberate second send after the capabilities are visible.
        setShareUnknown(guardedSid, true)
        setShareAckNotice({ sid: guardedSid,
          message: 'Nothing sent · live public-link status must be verified first. Your draft is preserved.' })
        verifyShareStatus(guardedSid)
        flash('Nothing sent · checking live public-link status')
        return
      }
      acknowledgedLiveShareIds = [...assistantLiveShareIds(guardedSession)]
      setShareAckNotice(current => current?.sid === guardedSid ? null : current)
    }
    cancelAttentionPermissionHandoff()
    turnCaptureRef.current = true
    setTurnStarting(true)
    const sessionSeq = ++openSessionSeqRef.current
    const localAttempt = {}
    activeReplyAttemptRef.current = localAttempt
    const draftAtSend = composerDraftRef.current
    const inputAtSend = draftAtSend.input
    const runScopeAtSend = draftAtSend.runScope
    const atts = retryFiles != null ? [...retryFiles] : [...draftAtSend.files]
    const effectiveMode = turnMode || normalizeComposerMode(draftAtSend.mode)
    // A just-fired Stop's cancel POST may still be in flight. Wait before consuming the composer or
    // publishing an optimistic turn, so a session choice made during this wait leaves the draft exact.
    if (cancelReqRef.current) { try { await cancelReqRef.current } catch { /* done */ } }
    const commandClaimedDuringCancel = !!runId && (!!loadRunCommandLock(runId)
      || (!!loadAssistantRunTransport(runId) && !directFailure))
    const accessBecameReadOnly = historicalRef.current
    if (!mountedRef.current || activeReplyAttemptRef.current !== localAttempt
        || sessionSeq !== openSessionSeqRef.current || openSessionPendingRef.current
        || directCaptureRef.current || commandClaimedDuringCancel || accessBecameReadOnly) {
      if (activeReplyAttemptRef.current === localAttempt) {
        activeReplyAttemptRef.current = null
        turnCaptureRef.current = false
        setTurnStarting(false)
      }
      if (commandClaimedDuringCancel) flash('Draft not sent · a run command claimed this run first')
      else if (accessBecameReadOnly) flash('Draft not sent · run access became read-only')
      return
    }
    if (ensureVisible && view === 'bar') setAssistantView('side')
    const previewAtSend = preview
    const hasNewAtSend = hasNew
    clearReplyAnnouncement()
    setPreview(''); setHasNew(false)
    let id = guardedSid
    if (!id) {
      // Create the session FIRST; only then clear the attached-file chips — else a create failure
      // strands the user with their files already gone.
      try {
        const m = await boundedRequest(signal => assistantCreate(
          (userText || instruction).slice(0, 60), effectiveMode, { signal }))
        if (!mountedRef.current || sessionSeq !== openSessionSeqRef.current
            || activeReplyAttemptRef.current !== localAttempt) {
          if (activeReplyAttemptRef.current === localAttempt) {
            activeReplyAttemptRef.current = null
            turnCaptureRef.current = false
            setTurnStarting(false)
          }
          return
        }
        id = m.id
        bindComposerToSession(id)
        sidRef.current = id; storageSet('ll.asstSid', id); setSid(id)
        if (historicalRef.current) {
          if (activeReplyAttemptRef.current === localAttempt) {
            activeReplyAttemptRef.current = null
            turnCaptureRef.current = false
            setTurnStarting(false)
          }
          refreshSessions()
          flash('Chat created, but the message was not sent because run access became read-only — your draft is preserved')
          return
        }
      } catch {
        if (activeReplyAttemptRef.current === localAttempt) {
          activeReplyAttemptRef.current = null
          turnCaptureRef.current = false
          setTurnStarting(false)
        }
        if (sessionSeq === openSessionSeqRef.current) flash('Could not start the chat — your draft is preserved')
        return
      }
    }
    // Keep the exact composer intact if creating the session failed. After durable creation, clear
    // only what this send captured: typing or file reads completed during the request remain as a draft.
    let inputCleared = false
    if (clearComposer && composerDraftRef.current === draftAtSend && draftAtSend.input === inputAtSend) {
      setInput('')
      inputCleared = true
    }
    if (retryFiles == null) {
      updateComposerFiles(draftAtSend, current => current.filter(file => !atts.includes(file)))
    }
    // What was silently attached to this turn (run, #experiments, files) — shown as a faint caption
    // ABOVE the user's bubble so the injected context is visible, not hidden inside the instruction.
    const ctxInfo = { run: context?.run || null, refs: context?.refs || [], files: atts.map(a => a.name) }
    const hasCtx = ctxInfo.run || ctxInfo.refs.length || ctxInfo.files.length
    const fullInstruction = instruction + filePreamble(atts)
    const localHistoryLength = msgs.length
    const localUserTurn = { role: 'user', content: userText || instruction,
      context: hasCtx ? ctxInfo : null,
      retryPayload: { instruction, raw: fullInstruction.trim(), userText: userText || instruction,
        context, files: atts, mode: effectiveMode, historyLength: localHistoryLength },
      localAttempt }
    atBottomRef.current = true          // sending my own message: always scroll it into view
    setMsgs(m => [...m, localUserTurn,
      { role: 'assistant', content: '', streaming: true, localAttempt }])
    const priorLen = localHistoryLength + 2
    setTurnStarting(false); setBusy(true); runningRef.current = true
    const ctrl = new AbortController(); abortRef.current = ctrl
    let polling = true
    // sid-guarded like every other callback: after a mid-turn session switch, a late poll result
    // must not surface the DEPARTED session's confirm-cards over the one the user switched to.
    ;(async () => {
      while (polling && replyAttemptCurrent(localAttempt, id)) {
        try {
          const permissionSnapshot = await readPermissionSnapshot(id)
          if (permissionSnapshot.ok && replyAttemptCurrent(localAttempt, id)) {
            setPending(permissionSnapshot.pending)
          }
        } catch { /* transient */ }
        await sleep(800)
      }
    })()
    let acc = ''
    let streamedFailure = ''
    // Concurrent PROGRESS poll — the SSE fallback. Behind a buffering proxy (jupyter-server-proxy /
    // nginx) the token/text/step SSE events arrive batched only at the END, leaving a dead "thinking"
    // bubble the whole time. So ALSO poll /progress: while the real SSE tokens haven't arrived yet
    // (acc still shorter than the server's mirrored answer-so-far), surface that live text + tool steps.
    // Once tokens actually flow, acc overtakes it and the authoritative SSE content wins — this only
    // fills the buffered gap, never fights a working stream.
    ;(async () => {
      while (polling && replyAttemptCurrent(localAttempt, id)) {
        await sleep(1000)
        if (!replyAttemptCurrent(localAttempt, id)) break
        try {
          const pp = await assistantProgress(id)
          if (!replyAttemptCurrent(localAttempt, id)) break
          if (!pp || !pp.active) continue
          if (replyAttemptCurrent(localAttempt, id) && acc.length < (pp.text || '').length)
            patchLast(prev => (prev && prev.role === 'assistant' && prev.streaming)
              ? { content: assistantErrorInfo(pp.text) ? normalizedFailureText(pp.text) : (pp.text || prev.content),
                  activity: (pp.steps || []).length ? [{ type: 'tools', labels: pp.steps }] : prev.activity }
              : prev)
        } catch { /* transient */ }
      }
    })()
    const safeAttempt = (fn) => (...a) => { if (replyAttemptCurrent(localAttempt, id)) fn(...a) }
    try {
      if (!replyAttemptCurrent(localAttempt, id)) return
      const res = await assistantMessageStream(id, fullInstruction, effectiveMode, {
        onToken: safeAttempt((tok) => {
          acc += tokText(tok)
          patchLast({ content: assistantErrorInfo(acc) ? normalizedFailureText(acc) : acc })
        }),
        onText: safeAttempt((txt) => patchLast(prev => ({ activity: [...(prev.activity || []), { type: 'text', content: txt }] }))),
        onStep: safeAttempt((s) => patchLast(prev => {
          const a = prev.activity || []; const last = a[a.length - 1]
          return last && last.type === 'tools'
            ? { activity: [...a.slice(0, -1), { ...last, labels: [...last.labels, s] }] }
            : { activity: [...a, { type: 'tools', labels: [s] }] }
        })),
        onTodos: safeAttempt((items) => patchLast({ todos: items })),
        onError: safeAttempt((e) => {
          streamedFailure = normalizedFailureText(e)
          patchLast({ content: streamedFailure })
          flash(safeErrorNotice(e))
        }),
      }, ctrl.signal, userText || instruction,
      acknowledgedLiveShareIds)   // persist the CLEAN bubble, not the ctx-augmented instruction
      if (!replyAttemptCurrent(localAttempt, id)) return
      // Stop may race with a terminal frame. Even if the stream resolved first, the turn is cancelled:
      // do not overwrite the "(stopped)" bubble that stop() already wrote.
      if (res?.ok === false) {
        // A terminal SSE error is not a completed assistant turn. In particular, the backend's final
        // live-share fence can reject the assistant append after the user turn was durably staged.
        // Reconcile by GET only; never turn this terminal frame into a new logical POST.
        const failureText = streamedFailure || normalizedFailureText(res.error || 'assistant reply not saved')
        try {
          const terminalShareMetaRead = beginShareMetaRead(id)
          const session = await boundedRequest(signal => assistantGet(id, { signal }))
          if (!replyAttemptCurrent(localAttempt, id)) return
          const durableMessages = session.messages
          if (!Array.isArray(durableMessages)) throw new Error('Invalid Assistant session transcript')
          const shareMetaValid = applyAssistantShareMeta(id, session.meta, terminalShareMetaRead)
          const exactTurnIndex = assistantTurnIndex(durableMessages, localUserTurn)
          const exactReply = durableMessages.length >= priorLen
            ? completedAssistantReply(durableMessages, localUserTurn) : null
          if (exactReply?.content) {
            setMsgs(durableMessages)
            const ready = announceReplyReady(exactReply.content,
              { sessionId: id, turn: localUserTurn, attempt: localAttempt })
            setPreview(previewText(exactReply.content))
            if (ready) setHasNew(viewRef.current === 'bar')
            return
          }
          if (exactTurnIndex >= 0 && exactTurnIndex === durableMessages.length - 1) {
            setMsgs([...durableMessages, { role: 'assistant', content: failureText,
              streaming: false, recovering: false, recoveryNeeded: true }])
            setPreview(previewText(failureText)); setHasNew(false)
            const acknowledged = new Set(acknowledgedLiveShareIds)
            const liveStateChanged = shareMetaValid
              && assistantLiveShareIds(session.meta).some(shareId => !acknowledged.has(shareId))
            if (liveStateChanged) {
              setShareAckNotice({ sid: id,
                message: 'Reply not saved · live public-link state changed. Verify it, then retry the original turn.' })
              flash('Reply not saved · retry the original turn after reviewing the live public link')
            } else flash('Assistant reply not saved · retry the original turn')
            return
          }
        } catch { /* retain the optimistic user turn as recoverable below */ }
        patchLast({ content: failureText, streaming: false, recovering: false, recoveryNeeded: true })
        flash('Assistant reply could not be confirmed · check the saved turn before retrying')
        return
      }
      const rawReply = streamedFailure || (res && res.reply) || acc || (res && res.ok === false && res.error ? `Assistant error: ${res.error}` : '(no reply)')
      const reply = assistantErrorInfo(rawReply) ? normalizedFailureText(rawReply) : rawReply
      patchLast({ content: reply, streaming: false, steps: res && res.steps, applied: res && res.applied,
                  proposals: res && res.proposals, todos: res && res.todos, tokens: res && res.tokens,
                  error_kind: res && res.error_kind })
      const ready = announceReplyReady(reply,
        { sessionId: id, turn: localUserTurn, attempt: localAttempt })
      setPreview(previewText(reply))
      if (ready) setHasNew(viewRef.current === 'bar')
    } catch (e) {
      if (!replyAttemptCurrent(localAttempt, id)) return
      const shareStateChanged = assistantLiveShareAckRequired(e)
      const forkTurnRejected = assistantForkTurnInProgress(e)
      if (shareStateChanged || forkTurnRejected) {
        // The backend rejected this before staging a turn. Remove only this optimistic pair, restore
        // what the send consumed, and never route the 409 through ambiguous-turn polling or auto-retry.
        if (replyAttemptCurrent(localAttempt, id)) {
          if (shareStateChanged) setShareUnknown(id, true)
          if (inputCleared && inputAtSend) {
            const currentInput = draftAtSend.input
            const restoredInput = !currentInput ? inputAtSend
              : currentInput === inputAtSend || currentInput.startsWith(`${inputAtSend}\n\n`)
                ? currentInput : `${inputAtSend}\n\n${currentInput}`
            draftAtSend.input = restoredInput
            if (composerDraftRef.current === draftAtSend) setInputState(restoredInput)
          }
          if (retryFiles == null && atts.length) {
            updateComposerFiles(draftAtSend, current => {
              const names = new Set(current.map(file => file.name))
              return [...current, ...atts.filter(file => !names.has(file.name))]
            })
          }
          // Restoring a rejected A-scoped turn after navigation to B must never silently bind its
          // command, node refs, or attachments to B. A mixed restored draft stays A-scoped and the
          // route-mismatch interlock below requires the user to separate, clear, or explicitly rebind it.
          draftAtSend.runScope = runScopeAtSend
          if (composerDraftRef.current === draftAtSend) setDraftRunScope(runScopeAtSend)
          if (sidRef.current === id) {
            setMsgs(current => current.filter(message => message.localAttempt !== localAttempt))
            setPreview(previewAtSend); setHasNew(hasNewAtSend && viewRef.current === 'bar')
          }
          const restored = inputCleared || (retryFiles == null && atts.length > 0)
          if (shareStateChanged) {
            setShareAckNotice({ sid: id, message: restored
              ? 'Nothing sent · live public-link state changed. Your draft was restored; verify the warning, then send again.'
              : 'Nothing sent · live public-link state changed. Verify the warning, then retry the original turn.' })
            flash(restored ? 'Nothing sent · draft restored after public-link state changed'
              : 'Nothing sent · public-link state changed')
            refreshSessions()
            refreshSessionShareMeta(id)
          } else {
            flash(restored ? 'Nothing sent · draft restored while this chat is being forked'
              : 'Nothing sent · this chat is being forked')
          }
        }
        return
      }
      // Only our own AbortController proves a quiet local stop/unmount. A transport/runtime can also
      // label a remote reset AbortError; that ambiguous accepted turn still requires reconciliation.
      if (!replyAttemptCurrent(localAttempt, id) || ctrl.signal.aborted) { /* handled in finally */ }
      else if (streamedFailure) {
        patchLast({ content: streamedFailure, streaming: false })
      }
      else {
        // A clean EOF is not proof that the turn finished. Keep any SSE or /progress text visibly live
        // while GET-only reconciliation waits for the durable assistant reply; never repeat the POST.
        patchLast(prev => {
          const observed = String(prev?.content || '')
          return { content: acc.length >= observed.length ? acc : observed,
            streaming: true, recovering: true }
        })
        flash(acc ? 'stream interrupted — recovering final reply…' : 'reconnecting…')
        const ok = await recoverReply(id, priorLen, localUserTurn, localAttempt)
        // `runningRef` guard: if the user hit Stop during recovery, keep the "(stopped)" bubble.
        if (replyAttemptCurrent(localAttempt, id) && !ok) patchLast({
          content: normalizedFailureText('connection unreachable'), streaming: false, recovering: false,
          recoveryNeeded: true,
        })
      }
    } finally {   // only the attempt that still owns this session may release its shared UI state
      polling = false
      if (activeReplyAttemptRef.current === localAttempt) {
        activeReplyAttemptRef.current = null
        turnCaptureRef.current = false
        setTurnStarting(false)
        runningRef.current = false
        if (abortRef.current === ctrl) abortRef.current = null
        if (mountedRef.current && sidRef.current === id) { setBusy(false); setPending([]) }
      }
    }
  }

  const requestNewRun = (goal = '', options = {}) => runLLM(
    goal ? `Plan a new run for this goal and show me a launch card to start it: ${goal}`
         : 'I want to start a new run. Propose a run spec (name, task, key settings) as a launch card I can start; ask me for anything you need first.',
    { userText: goal ? `/new ${goal}` : '/new', ensureVisible: true, ...options })

  const retryTurn = async (assistantIndex) => {
    if (openSessionPendingRef.current) {
      flash('Wait for the selected Assistant chat to finish opening')
      return
    }
    if (shareBusySid != null || shareActionSessionRef.current) {
      flash('Wait for the current public-link action before retrying this turn')
      return
    }
    const guardedSid = sidRef.current || sid
    if (guardedSid && forkActionSessionRef.current === guardedSid) {
      flash('Wait for this Assistant chat to finish forking before sending')
      return
    }
    if (guardedSid && shareVerificationRef.current.sid === String(guardedSid)
        && shareVerificationRef.current.promise) {
      flash('Retry paused · still checking public-link status')
      return
    }
    if (guardedSid && shareUnknownSids.has(guardedSid)) {
      flash('Turn not retried · checking public-link status')
      verifyShareStatus(guardedSid)
      return
    }
    if (turnCaptureRef.current || directCaptureRef.current || busy || commandBusy) {
      flash(commandBusy ? 'A run command is pending' : 'Assistant is busy'); return
    }
    if (historical) { flash(readOnlyAction); return }
    cancelAttentionPermissionHandoff()
    const failedTurn = msgs[assistantIndex]
    const prior = [...msgs.slice(0, assistantIndex)].reverse().find(m => m.role === 'user')
    if (!prior) { flash('The original message is no longer available'); return }
    if (failedTurn?.recoveryBlocked) {
      flash('This saved turn cannot be retried safely; start a new chat')
      return
    }
    let acknowledgedShareMeta = null
    if (failedTurn?.recoveryNeeded) {
      const id = sidRef.current || sid
      if (!id) { flash('The saved Assistant session is no longer available'); return }
      const retryCheckAttempt = {}
      const retrySessionSeq = ++openSessionSeqRef.current
      activeReplyAttemptRef.current = retryCheckAttempt
      turnCaptureRef.current = true
      clearReplyAnnouncement()
      setRetryChecking(true)
      try {
        // Refresh first: if the original POST was staged, openSession performs exact raw/mode recovery;
        // if its reply landed late, simply surface it. Only a genuinely unstaged request falls through
        // to the ordinary new-turn retry below.
        const retryShareMetaRead = beginShareMetaRead(id)
        const session = await boundedRequest(signal => assistantGet(id, { signal }))
        if (historicalRef.current || !replyAttemptOwned(retryCheckAttempt, id)
            || retrySessionSeq !== openSessionSeqRef.current || openSessionPendingRef.current) return
        const shareMetaOutcome = applyAssistantShareMeta(id, session.meta, retryShareMetaRead)
        if (shareMetaOutcome === undefined) {
          setShareUnknown(id, true)
          setShareAckNotice({ sid: id,
            message: 'Saved turn not retried · public-link status changed while checking.' })
          verifyShareStatus(id)
          flash('Retry paused · public-link status changed while checking')
          return
        }
        if (!shareMetaOutcome) {
          setShareAckNotice({ sid: id,
            message: 'Saved turn not retried · live public-link status could not be verified.' })
          refreshSessions()
          flash('Retry paused · verifying live public-link status')
          return
        }
        acknowledgedShareMeta = session.meta
        const durableMessages = session.messages || []
        const exactReply = completedAssistantReply(durableMessages, prior)
        if (exactReply?.content) {
          setMsgs(durableMessages)
          const ready = announceReplyReady(exactReply.content,
            { sessionId: id, turn: prior, attempt: retryCheckAttempt })
          setPreview(previewText(exactReply.content))
          if (ready) setHasNew(viewRef.current === 'bar')
          return
        }
        const exactTurnIndex = assistantTurnIndex(durableMessages, prior)
        if (exactTurnIndex === durableMessages.length - 1 && danglingAssistantTurn(durableMessages)) {
          if (activeReplyAttemptRef.current === retryCheckAttempt) {
            activeReplyAttemptRef.current = null
            turnCaptureRef.current = false
            setRetryChecking(false)
          }
          await openSession(id, { recover: true })
          return
        }
      } catch (error) {
        if (!replyAttemptOwned(retryCheckAttempt, id)
            || retrySessionSeq !== openSessionSeqRef.current || openSessionPendingRef.current) return
        flash(error?.status === 404 ? 'This Assistant session no longer exists' : 'Could not check the saved Assistant turn')
        return
      } finally {
        if (activeReplyAttemptRef.current === retryCheckAttempt) {
          activeReplyAttemptRef.current = null
          turnCaptureRef.current = false
          setRetryChecking(false)
        }
      }
    }
    if (prior.turn_id) {
      // A completed turn loaded from disk has no in-memory file objects/retryPayload, but it does retain
      // the canonical raw instruction and mode. Retrying is a NEW logical turn here, while its intent is
      // still exact: never rebuild a clean-content/current-mode approximation.
      const persisted = assistantRecoveryPayload(prior)
      if (!persisted) { flash('The saved Assistant turn cannot be retried safely'); return }
      runLLM(persisted.instruction, { userText: persisted.display, ensureVisible: true,
        turnMode: persisted.mode, acknowledgedShareMeta })
      return
    }
    if (prior.retryPayload) {
      const payload = prior.retryPayload
      runLLM(payload.instruction, { userText: payload.userText, ensureVisible: true,
        context: payload.context || null, retryFiles: payload.files || [], turnMode: payload.mode || null,
        acknowledgedShareMeta })
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
    const ctx = uiRunContext(contextRun, refs)
    runLLM(userText + ctx, { userText, ensureVisible: true, context: prior.context || null,
      acknowledgedShareMeta })
  }

  const openAssistantSettings = () => {
    handoffAssistantRoute('#/settings', { collapse: true })
  }

  const currentComposerRunKey = composerRunKey(runId)
  const draftRunMismatch = draftRunScope != null
    && draftRunScope !== currentComposerRunKey && composerUsesRun(input, files, pendingFileReads)
  const draftRunSource = draftRunScope ? `run “${draftRunScope}”` : 'the Runs overview'
  const draftRunDestination = runId ? `run “${runId}”` : 'the Runs overview'
  const draftRunMismatchMessage = `Draft belongs to ${draftRunSource} and will not be sent in ${draftRunDestination}.`
  const useDraftHere = () => {
    if (openSessionPendingRef.current) {
      flash('Wait for the selected Assistant chat to finish opening before rebinding this draft')
      return
    }
    if (!composerUsesRun(input, files, pendingFileReads)) return
    const draft = composerDraftRef.current
    draft.runScope = currentComposerRunKey
    setDraftRunScope(currentComposerRunKey)
    flash(runId ? `Draft now targets run “${runId}”` : 'Draft detached from its previous run')
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  const cancelDirectConfirmation = () => {
    if (!directConfirmRef.current) return
    clearDirectConfirmation({ focus: true })
  }
  const clearCommittedDirectDraft = (draft, sourceText) => {
    if (draft.input.trim() !== sourceText.trim()) return
    draft.input = ''
    if (!composerUsesRun('', draft.files, draft.pendingFileReads)) draft.runScope = null
    if (composerDraftRef.current === draft) {
      setInputState('')
      setDraftRunScope(draft.runScope)
    }
  }
  const revealDirectConfirmation = confirmation => {
    clearToast()
    directConfirmRef.current = confirmation
    setDirectConfirm(confirmation)
    setSuggestionsDismissed(true)
    setAssistantView(current => current === 'bar' ? 'side' : current)
    setHasNew(false)
  }
  const requestDirectCommand = async (command, sourceText) => {
    if (directConfirmRef.current) {
      flash('Resolve the current run-command confirmation first')
      return
    }
    const effectiveMode = normalizeComposerMode(composerDraftRef.current.mode)
    const decision = assistantDirectDecision(effectiveMode)
    if (decision === 'deny') {
      flash('Plan is read-only · switch to Ask, Auto-edit, or Auto to run a control command')
      return
    }
    if (decision === 'inline') {
      const owningDraft = composerDraftRef.current
      runDirect(command, {
        onCommitted: () => clearCommittedDirectDraft(owningDraft, sourceText),
      })
      return
    }
    const directRunId = String(runId || '')
    if (!directRunId) { flash(`/${command.name} needs an open run`); return }
    if (historicalRef.current) { flash(readOnlyAction); return }
    const expectedGeneration = normalizeRunGeneration(getObservedRunGeneration(directRunId))
    if (!expectedGeneration) {
      flash('Run generation is not verified · refresh the run before confirming a control command')
      return
    }
    const confirmationBase = {
      direct: command,
      sourceText,
      composerKey: composerKeyRef.current,
      draft: composerDraftRef.current,
      expectedGeneration,
      mode: effectiveMode,
      modeLabel: MODES.find(candidate => candidate.id === effectiveMode)?.label || effectiveMode,
      ...assistantDirectPresentation(command.name, command.arg, directRunId),
    }
    if (command.name === 'approve') {
      const approvalRead = deadlineRequest(
        signal => get(runApiPath(directRunId, '/state'), { signal }), 8000)
      const checking = {
        ...confirmationBase, phase: 'checking', requestController: approvalRead.controller,
      }
      revealDirectConfirmation(checking)
      try {
        const payload = await approvalRead.promise
        if (directConfirmRef.current !== checking) return
        const fetchedGeneration = normalizeRunGeneration(payload?.generation)
        const target = pendingApprovalTarget(payload?.state)
        if (fetchedGeneration !== expectedGeneration || !target
            || target.nodeId !== command.arg || !Number.isSafeInteger(target.nodeGeneration)) {
          throw new Error('approval target changed')
        }
        const pendingConfirmation = {
          ...confirmationBase,
          phase: 'ready',
          nodeGeneration: target.nodeGeneration,
        }
        directConfirmRef.current = pendingConfirmation
        setDirectConfirm(pendingConfirmation)
      } catch {
        if (directConfirmRef.current !== checking) return
        clearDirectConfirmation({ focus: true })
        flash('Nothing sent · the exact approval target could not be verified; refresh and inspect Events')
      }
      return
    }
    revealDirectConfirmation({ ...confirmationBase, phase: 'ready' })
  }
  const confirmDirectCommand = () => {
    const confirmation = directConfirmRef.current
    if (!confirmation || confirmation.phase !== 'ready') return
    const observedGeneration = normalizeRunGeneration(getObservedRunGeneration(confirmation.runId))
    if (confirmation.composerKey !== composerKeyRef.current
        || confirmation.draft !== composerDraftRef.current
        || confirmation.sourceText.trim() !== composerDraftRef.current.input.trim()
        || confirmation.mode !== normalizeComposerMode(composerDraftRef.current.mode)
        || String(currentRunIdRef.current || '') !== confirmation.runId
        || historicalRef.current || observedGeneration !== confirmation.expectedGeneration) {
      clearDirectConfirmation({ focus: true })
      flash('Nothing sent · the run, its version, or the Assistant draft changed')
      return
    }
    runDirect(confirmation.direct, {
      runId: confirmation.runId,
      expectedGeneration: confirmation.expectedGeneration,
      nodeGeneration: confirmation.nodeGeneration,
      onClaimed: () => clearDirectConfirmation(),
      onCommitted: () => clearCommittedDirectDraft(
        confirmation.draft, confirmation.sourceText),
    })
  }

  const send = () => {
    if (historical) { flash(readOnlyAction); return }
    if (openSessionPendingRef.current) {
      flash('Wait for the selected Assistant chat to finish opening')
      return
    }
    if (shareBusySid != null || shareActionSessionRef.current) {
      flash('Wait for the current public-link action before sending')
      return
    }
    const guardedSid = sidRef.current || sid
    if (guardedSid && shareVerificationRef.current.sid === String(guardedSid)
        && shareVerificationRef.current.promise) {
      flash('Still checking public-link status · nothing sent')
      return
    }
    if (guardedSid && shareUnknownSids.has(guardedSid)) {
      flash('Nothing sent · checking public-link status')
      setShareAckNotice({ sid: guardedSid,
        message: 'Nothing sent · public-link status was unknown. Your draft is preserved; send again after verification completes.' })
      verifyShareStatus(guardedSid)
      return
    }
    const liveDraft = composerDraftRef.current
    if (liveDraft.runScope != null && liveDraft.runScope !== composerRunKey(runId)
        && composerUsesRun(liveDraft.input, liveDraft.files, liveDraft.pendingFileReads)) {
      flash('Draft not sent · choose Use here or remove its run-specific context')
      return
    }
    if (liveDraft.pendingFileReads > 0) {
      flash('Wait for the selected attachment to finish reading')
      return
    }
    const t = input.trim()
    const storedCommand = !!runId && (!!loadRunCommandLock(runId)
      || (!!loadAssistantRunTransport(runId) && !directFailure))
    if ((!t && files.length === 0) || turnCaptureRef.current || directCaptureRef.current
        || busy || commandBusy || storedCommand) {
      if (storedCommand) flash('Recover the stored run command before continuing')
      else if (turnCaptureRef.current || directCaptureRef.current) flash('Another action is already starting')
      return
    }
    cancelAttentionPermissionHandoff()
    const mNew = /^\/(new|genesis|run)\b\s*([\s\S]*)$/i.exec(t)
    if (mNew) {
      const goal = mNew[2].trim()
      requestNewRun(goal, { clearComposer: true })
      return
    }
    const direct = parseDirect(t)
    if (direct?.invalid) { flash(direct.message); return }
    if (direct) { requestDirectCommand(direct, t); return }
    const pr = runId ? preRoute(t) : null
    if (pr) { requestDirectCommand(pr, t); return }
    const refs = runId ? refNodes(t) : []
    const ctx = uiRunContext(runId, refs)
    runLLM((t || 'See the attached file(s).') + ctx, { userText: t || '(attached files)',
      context: { run: runId || null, refs }, clearComposer: true })
  }
  useEffect(() => {
    const onNewRun = (event) => {
      event.preventDefault()
      if (openSessionPendingRef.current) {
        flash('Wait for the selected Assistant chat to finish opening before drafting a new run')
        return
      }
      if (busy || commandBusy || historical) { flash(historical ? readOnlyAction : commandBusy ? 'A run command is pending' : 'Assistant is busy'); return }
      const goal = String(event.detail?.goal || '').trim()
      const command = goal ? `/new ${goal}` : '/new '
      const existing = input.trim()
      if (!existing || existing === command.trim()) setInput(command)
      else flash('Draft preserved — choose Chat or clear the composer before drafting a new run')
      setAssistantView(current => current === 'bar' ? 'side' : current)
      setHasNew(false)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
    window.addEventListener('ll:new-run', onNewRun)
    onReady?.()
    return () => window.removeEventListener('ll:new-run', onNewRun)
  }, [busy, commandBusy, historical, readOnlyAction, input, onReady])

  useEffect(() => {
    if (!directConfirm) return undefined
    const frame = requestAnimationFrame(() => {
      const target = directConfirmCancelRef.current || directConfirmStatusRef.current
      target?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [directConfirm, view])
  useEffect(() => {
    if (!directConfirm) return
    const contextChanged = hidden || historical || view === 'bar'
      || directConfirm.runId !== String(runId || '')
      || directConfirm.composerKey !== composerKeyRef.current
      || directConfirm.draft !== composerDraftRef.current
      || directConfirm.sourceText.trim() !== input.trim()
      || directConfirm.mode !== mode
    if (!contextChanged) return
    clearDirectConfirmation()
    if (!hidden) flash('Command confirmation closed because its run or Assistant draft changed')
  }, [clearDirectConfirmation, directConfirm, hidden, historical, input, mode, runId, sid, view, flash])
  useEffect(() => {
    if (!directConfirm) return undefined
    const invalidateStaleGeneration = () => {
      if (directConfirmRef.current !== directConfirm) return
      const observedGeneration = normalizeRunGeneration(
        getObservedRunGeneration(directConfirm.runId))
      if (observedGeneration === directConfirm.expectedGeneration) return
      clearDirectConfirmation({ focus: true })
      flash('Command confirmation closed because the run version changed · draft kept')
    }
    const timer = setInterval(invalidateStaleGeneration, 250)
    invalidateStaleGeneration()
    return () => clearInterval(timer)
  }, [clearDirectConfirmation, directConfirm, flash])

  useEffect(() => {
    if (!Object.keys(launchDrafts).length && !launchRecoveries.length) return
    const warnBeforeUnload = event => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [launchDrafts, launchRecoveries.length])

  const stop = () => {
    cancelAttentionPermissionHandoff()
    activeReplyAttemptRef.current = null
    turnCaptureRef.current = false
    setTurnStarting(false)
    setRetryChecking(false)
    runningRef.current = false                              // halt reattach/recover polling immediately
    clearReplyAnnouncement()
    if (abortRef.current) { try { abortRef.current.abort() } catch { /* already gone */ } abortRef.current = null }
    const id = sidRef.current || sid                        // cancel server-side (works even post-reload)
    if (id) {
      const pr = boundedRequest(signal => assistantCancel(id, { signal }), 8000).catch(() => {})
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
    if (!historical) return
    cancelAttentionPermissionHandoff()
    clearReplyAnnouncement()
    if (runningRef.current) { stop(); return }
    // A first message may already be waiting for its session-create receipt. Let it bind that durable
    // identity, then runLLM stops before consuming the draft or posting the turn; invalidating here
    // would strand an empty server-side chat with no client receipt.
    if (turnCaptureRef.current && !sidRef.current && !retryChecking) return
    if (turnCaptureRef.current || retryChecking || turnStarting) {
      ++openSessionSeqRef.current
      activeReplyAttemptRef.current = null
      turnCaptureRef.current = false
      setTurnStarting(false)
      setRetryChecking(false)
    }
  }, [historical, retryChecking, turnStarting, clearReplyAnnouncement,
    cancelAttentionPermissionHandoff])

  const activeMode = MODES.find(x => x.id === mode) || MODES[0]
  const changeInput = e => {
    if (openSessionPendingRef.current) return
    setInput(e.target.value); setSuggestionsDismissed(false); setSuggestionIndex(0)
  }
  const pendingCommandText = directStarting
    ? 'Checking run command preconditions…'
    : directPending ? assistantDirectStatus(directPending)
    : externalCommandPending ? `/${externalCommandPending.action} is pending in the run timeline` : ''
  const canCheckDirect = !!directPending?.statusUnavailable && !directPending?.checking
  const canRetryDirect = !commandBusy && commandCanRetry(directFailure?.record)
  const directRecoveryPaused = historical || sessionOpening || turnStarting || retryChecking || busy
  const showDirectFailure = !!directFailure && !commandBusy
  const directFailureText = directFailure
    ? `/${directFailure.name} failed: ${commandErrorMessage(directFailure.record)}` : ''
  const directNeedsAlert = directPending?.observationKind === 'access' || showDirectFailure
  // Suppress an A-scoped toast during the very first render for B; the effect below then clears its
  // timer/state. This avoids a one-frame stale announcement between route commit and useEffect.
  const visibleToast = assistantRunChanged(toastRunIdRef.current, runId) ? null : toast
  const nextLaunchRecovery = launchRecoveries[0]
  const launchRecoveryLabel = nextLaunchRecovery?.invalid ? 'Review startup recovery' : 'Check startup'
  const launchRecoveryButton = launchRecoveries.length > 0
    ? <button type="button" className="btn sm asst-launch-global" onClick={openLaunchRecovery}
        aria-label={`${launchRecoveryLabel}${launchRecoveries.length > 1 ? ` (${launchRecoveries.length})` : ''}`}
        title={nextLaunchRecovery?.invalid
          ? 'Review a damaged durable startup recovery record'
          : 'Reopen the Assistant proposal that owns this durable startup identity'}>
        <OpIcon name="alert" size={13} />
        <span className="asst-launch-global-label">{launchRecoveryLabel}</span>
        {launchRecoveries.length > 1 && <span className="asst-launch-global-count">{launchRecoveries.length}</span>}
      </button>
    : null
  useEffect(() => {
    if (!commandFocusRequestedRef.current || (!directPending && !directFailure && !externalCommandPending)) return
    requestAnimationFrame(() => {
      if (!commandStatusRef.current) return
      commandFocusRequestedRef.current = false
      commandStatusRef.current.focus()
    })
  }, [directPending?.record?.id, directPending?.record?.status, directPending?.statusUnavailable,
    directPending?.checking, directPending?.retrying,
    directFailure?.record?.id, directFailure?.record?.status, directStarting, busy, view])
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
  const draftingNewRun = /^\/(?:new|genesis|run)\b/i.test(input.trim())
  const directModeDecision = assistantDirectDecision(mode)
  const directModeHint = directModeDecision === 'deny'
    ? 'run control · unavailable in Plan'
    : directModeDecision === 'ask'
      ? 'run control · confirmation required'
      : 'run control · executes immediately'
  const directNames = [
    { name: 'new', desc: 'draft a launch card — review before start' },
    ...(runId ? Object.keys(DIRECT).map(n => ({ name: n, desc: directModeHint })) : []),
  ]
  const suggestions = slashMatch
    ? [...directNames, ...commands.map(c => ({ name: c.name, desc: c.desc }))]
        .filter(c => c.name.startsWith(slashMatch[1].toLowerCase()))
        .filter((c, i, a) => a.findIndex(x => x.name === c.name) === i).slice(0, 6)
    : []
  // /command hints show in EVERY view (bar, side, full) — not just the docked bar (users compose in the
  // side + full composers too and need the same command discovery).
  const showSuggestions = !historical && !suggestionsDismissed && suggestions.length > 0
  const activeSuggestionIndex = showSuggestions ? Math.min(suggestionIndex, suggestions.length - 1) : -1
  const chooseSuggestion = (index = activeSuggestionIndex) => {
    if (openSessionPendingRef.current) return
    const choice = suggestions[index]
    if (!choice) return
    setInput(`/${choice.name} `)
    setSuggestionsDismissed(true)
    setSuggestionIndex(0)
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  const onKey = (e) => {
    // Ignore keys emitted while an IME is composing (CJK, accent/dead-key input): the Enter/Arrow
    // keys then belong to the candidate window, not to us. `isComposing` (keyCode 229 on legacy
    // engines) tells us to stand down so confirming a candidate never submits the message.
    if (e.isComposing || e.nativeEvent?.isComposing || e.keyCode === 229) return
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
  const currentSession = sessions.find(session => session.id === sid)
  const shareUnknown = !!sid && shareUnknownSids.has(sid)
  const shareCopy = sid ? shareCopyFallbacks[sid] || null : null
  const currentShareAckNotice = shareAckNotice?.sid === sid ? shareAckNotice : null
  const liveShareActive = currentSession?.share_live === true
  const shareBusy = shareBusySid != null
  const shareVerifying = !!sid && shareVerifySid === sid
  const sharePaused = shareBusy || shareUnknown || shareVerifying
  const deletingCurrentSession = !!sid && deletingSessionsRef.current.has(sid)
  const shareTurnIncomplete = sessionOpening || busy || commandBusy || pending.length > 0
    || runningRef.current || turnCaptureRef.current || msgs[msgs.length - 1]?.role === 'user'
  const forkBusy = forkBusySid != null
  const forkingCurrentSession = !!sid && forkBusySid === sid
  const currentForkRecovery = forkRecovery?.sid === sid ? forkRecovery : null
  const composerPaused = sessionOpening || turnStarting || retryChecking || sharePaused
    || forkingCurrentSession || !!directConfirm
  // Unknown public-link truth blocks every server mutation, but it must not destroy a local draft or
  // native focus. Keep editing controls available; Send remains focusable and performs a fail-closed
  // verification without staging a turn.
  const composerEditingPaused = sessionOpening || shareBusy || forkingCurrentSession || !!directConfirm
  const verifyShareStatus = (targetSid, retrySuperseded = true) => {
    const target = String(targetSid || '')
    if (!target) return Promise.resolve(null)
    const active = shareVerificationRef.current
    if (active.sid === target && active.promise) return active.promise
    const seq = active.seq + 1
    setShareVerifySid(target)
    let verification
    verification = refreshSessionShareMeta(target).then(session => {
      const latest = shareVerificationRef.current
      if (latest.seq !== seq || latest.sid !== target || latest.promise !== verification) return session
      shareVerificationRef.current = { seq, sid: null, promise: null }
      if (mountedRef.current) setShareVerifySid(current => current === target ? null : current)
      if (!mountedRef.current || sidRef.current !== target) return session
      if (session === undefined) {
        if (retrySuperseded) return verifyShareStatus(target, false)
        flash('Public-link status changed while checking · review the current status before sending')
      } else if (session) {
        setShareAckNotice(current => current?.sid === target ? null : current)
        flash('Public-link status verified · your draft is ready to send')
      } else {
        flash('Public-link status could not be verified · your draft is still preserved')
      }
      return session
    })
    shareVerificationRef.current = { seq, sid: target, promise: verification }
    return verification
  }
  const retryShareStatus = event => {
    const returnToDraft = document.activeElement === event.currentTarget
    const targetSid = sid
    verifyShareStatus(targetSid)
    if (returnToDraft) requestAnimationFrame(() => inputRef.current?.focus({ preventScroll: true }))
  }
  const shareStatusMessage = shareVerifying
    ? 'Checking public-link status. You can keep editing this draft; nothing has been sent.'
    : shareUnknown
      ? 'Public-link status unknown. You can keep editing this draft; sending is paused until the status is verified or public links are revoked.'
    : ''
  const rememberForkRecovery = (sourceSid, recovery) => {
    const stored = loadAssistantForkRecovery(sourceSid)
    const durable = stored?.actionId === recovery.actionId
      && stored.expectedMessages === recovery.expectedMessages
      ? true : saveAssistantForkRecovery(sourceSid, recovery)
    if (!durable) return false
    forkRecoveryRef.current.set(sourceSid, recovery)
    if (mountedRef.current && (sidRef.current || sid) === sourceSid) {
      setForkRecovery({ sid: sourceSid, ...recovery })
    }
    return true
  }
  const forgetForkRecovery = (sourceSid, actionId) => {
    if (forkRecoveryRef.current.get(sourceSid)?.actionId === actionId) {
      forkRecoveryRef.current.delete(sourceSid)
    }
    clearAssistantForkRecovery(sourceSid, actionId)
    if (mountedRef.current) {
      setForkRecovery(current => current?.sid === sourceSid && current.actionId === actionId
        ? null : current)
    }
  }
  const presentForkChild = async (child, sourceSid, recovery, sourceChoiceSeq) => {
    if (!mountedRef.current) return false
    const childId = typeof child?.id === 'string' && ASSISTANT_SESSION_RE.test(child.id)
      && child.parent === sourceSid && child.fork_action_id === recovery.actionId ? child.id : null
    if (!childId) return false
    const listed = await refreshSessions()
    if (!mountedRef.current) return false
    let confirmed = Array.isArray(listed) && listed.some(session => session?.id === childId)
    if (sidRef.current === sourceSid && openSessionSeqRef.current === sourceChoiceSeq) {
      const opened = await openSession(childId)
      confirmed = confirmed || !!opened?.ok
    } else if (confirmed) {
      flash('Fork created · kept your newer chat selection')
    }
    if (!confirmed) return false
    forgetForkRecovery(sourceSid, recovery.actionId)
    return true
  }
  const reconcileFork = async (sourceSid, recovery) => {
    const actionId = recovery.actionId
    let sawPending = false
    for (let attempt = 0; attempt < 5 && mountedRef.current; attempt++) {
      if (attempt > 0) await sleep(350 * attempt)
      try {
        const child = await boundedRequest(
          signal => assistantForkStatus(sourceSid, actionId, {
            expectedMessages: recovery.expectedMessages, signal,
          }), 4000)
        return { kind: 'created', child }
      } catch (error) {
        const reportedAction = String(error?.detail?.action_id || '')
        if (error?.code === 'assistant_fork_in_progress'
            && (!reportedAction || reportedAction === actionId)) {
          sawPending = true
          continue
        }
        if (error?.code === 'assistant_fork_in_progress') {
          return { kind: 'blocked', error }
        }
        if (error?.code === 'assistant_fork_deleted') return { kind: 'deleted', error }
        if (error?.code === 'assistant_fork_action_conflict') return { kind: 'conflict', error }
        if (error?.code === 'assistant_fork_deleting') return { kind: 'deleting', error }
        if (error?.status === 404) return { kind: 'absent', error }
        const status = Number(error?.status)
        const ambiguous = error?.name === 'TimeoutError' || error?.name === 'AbortError'
          || error?.status == null || status >= 500 || [408, 425, 429].includes(status)
        if (!ambiguous) return { kind: 'unknown', error }
      }
    }
    return { kind: sawPending ? 'pending' : 'unknown' }
  }
  const settleForkReconciliation = async (outcome, sourceSid, recovery, sourceChoiceSeq) => {
    if (!mountedRef.current) return
    if (outcome.kind === 'created') {
      if (await presentForkChild(outcome.child, sourceSid, recovery, sourceChoiceSeq)) return
      await refreshSessions()
      if (mountedRef.current) flash('Fork result is uncertain · check the chat list before retrying')
      return
    }
    if (outcome.kind === 'deleted') {
      forgetForkRecovery(sourceSid, recovery.actionId)
      await refreshSessions()
      if (mountedRef.current) flash('That fork was deleted · fork again to create a new copy')
      return
    }
    if (outcome.kind === 'conflict') {
      forgetForkRecovery(sourceSid, recovery.actionId)
      await refreshSessions()
      if (mountedRef.current) flash('This saved fork request no longer matches the chat · review it and fork again')
      return
    }
    if (outcome.kind === 'deleting') {
      await refreshSessions()
      if (mountedRef.current) flash('That fork is being deleted · check again before retrying')
      return
    }
    if (outcome.kind === 'absent') {
      flash('Fork is not published · check fork retries this exact request')
      return
    }
    if (outcome.kind === 'blocked') {
      flash('Another fork is in progress · check fork keeps this request safe')
      return
    }
    await refreshSessions()
    if (mountedRef.current) flash(outcome.kind === 'pending'
      ? 'Fork is still finishing · use check fork to recover this exact request'
      : 'Fork status is uncertain · use check fork before starting another')
  }
  const forkCurrentSession = async () => {
    const forkSid = sidRef.current || sid
    if (!forkSid) return
    if (forkActionSessionRef.current) {
      flash('Another Assistant fork is still in progress')
      return
    }
    const storedRecovery = forkRecoveryRef.current.get(forkSid) || loadAssistantForkRecovery(forkSid)
    if (!storedRecovery && (runningRef.current || turnCaptureRef.current || busy || commandBusy
        || pending.length > 0 || msgs[msgs.length - 1]?.role === 'user')) {
      flash('Wait for a complete Assistant reply before forking this chat')
      return
    }
    if (deletingSessionsRef.current.has(forkSid)) {
      flash('This chat is being deleted')
      return
    }
    if (shareActionSessionRef.current) {
      flash('Wait for the current share action before forking this chat')
      return
    }
    const recovery = storedRecovery || {
      actionId: createIdempotencyKey().toLowerCase(), expectedMessages: msgs.length,
    }
    if (!rememberForkRecovery(forkSid, recovery)) {
      flash('Browser recovery storage is unavailable · enable it before forking safely')
      return
    }
    const sourceChoiceSeq = openSessionSeqRef.current
    forkActionSessionRef.current = forkSid
    setForkBusySid(forkSid)
    try {
      const child = await boundedRequest(
        signal => assistantFork(forkSid, recovery, { signal }), 12000)
      if (!await presentForkChild(child, forkSid, recovery, sourceChoiceSeq)) {
        const outcome = await reconcileFork(forkSid, recovery)
        await settleForkReconciliation(outcome, forkSid, recovery, sourceChoiceSeq)
      }
    } catch (error) {
      if (!mountedRef.current) return
      if (error?.code === 'assistant_fork_session_deleting') {
        forgetForkRecovery(forkSid, recovery.actionId)
        flash('This chat is being deleted')
      } else if (error?.code === 'assistant_fork_deleted') {
        forgetForkRecovery(forkSid, recovery.actionId)
        refreshSessions()
        flash('That fork was deleted · fork again to create a new copy')
      } else if (error?.code === 'assistant_fork_action_conflict') {
        forgetForkRecovery(forkSid, recovery.actionId)
        refreshSessions()
        flash('This saved fork request no longer matches the chat · review it and fork again')
      } else if (error?.code === 'assistant_fork_source_changed') {
        forgetForkRecovery(forkSid, recovery.actionId)
        refreshSessions()
        flash('This chat changed before the fork started · review it and fork again')
      } else if (['assistant_fork_turn_active', 'assistant_fork_turn_incomplete'].includes(error?.code)) {
        forgetForkRecovery(forkSid, recovery.actionId)
        flash('Wait for the current Assistant reply to finish or recover it before forking')
      } else if (error?.status === 404) {
        forgetForkRecovery(forkSid, recovery.actionId)
        flash('This Assistant chat no longer exists')
      } else if (error?.code === 'assistant_fork_in_progress'
          && error?.detail?.action_id && error.detail.action_id !== recovery.actionId) {
        const activeActionId = String(error.detail.action_id).toLowerCase()
        const activeExpectedMessages = typeof error.detail.expected_messages === 'number'
          ? error.detail.expected_messages : Number.NaN
        const adopted = { actionId: activeActionId, expectedMessages: activeExpectedMessages }
        if (!ASSISTANT_FORK_ACTION_RE.test(activeActionId)
            || !Number.isSafeInteger(activeExpectedMessages) || activeExpectedMessages < 0
            || activeExpectedMessages !== recovery.expectedMessages) {
          flash('Another tab is forking a different chat snapshot · refresh before retrying')
        } else {
          forgetForkRecovery(forkSid, recovery.actionId)
          if (!rememberForkRecovery(forkSid, adopted)) {
            flash('Another fork is in progress · refresh the chat list before retrying')
          } else {
            const outcome = await reconcileFork(forkSid, adopted)
            await settleForkReconciliation(outcome, forkSid, adopted, sourceChoiceSeq)
          }
        }
      } else if (error?.code === 'assistant_fork_in_progress'
          || error?.code === 'assistant_fork_deleting'
          || error?.code === 'assistant_fork_child_deleting'
          || error?.name === 'TimeoutError' || error?.name === 'AbortError'
          || error?.status == null || Number(error.status) >= 500
          || [408, 425, 429].includes(Number(error.status))) {
        const outcome = await reconcileFork(forkSid, recovery)
        await settleForkReconciliation(outcome, forkSid, recovery, sourceChoiceSeq)
      } else {
        forgetForkRecovery(forkSid, recovery.actionId)
        flash('Could not fork this Assistant chat')
      }
    } finally {
      if (forkActionSessionRef.current === forkSid) forkActionSessionRef.current = null
      if (mountedRef.current) setForkBusySid(current => current === forkSid ? null : current)
    }
  }
  const revokeCurrentShares = async () => {
    const shareSid = sidRef.current || sid
    if (!shareSid) return
    if (shareActionSessionRef.current) { flash('Another share action is still in progress'); return }
    if (forkActionSessionRef.current === shareSid) {
      flash('Wait for this chat to finish forking before changing its public links')
      return
    }
    if (deletingSessionsRef.current.has(shareSid)) {
      flash('This chat is being deleted')
      return
    }
    shareActionSessionRef.current = shareSid
    setShareBusySid(shareSid)
    try {
      const result = await boundedRequest(signal => assistantUnshare(shareSid, { signal }))
      if (!mountedRef.current) return
      setShareUnknown(shareSid, false)
      clearShareCopy(shareSid)
      mutateSessionsLocally(current => current.map(session => session.id === shareSid
        ? { ...session, shared: false, share_count: 0,
            share_ids: [], live_share_ids: [], share_expires_at: null, share_live: false } : session))
      setShareAckNotice(current => current?.sid === shareSid ? null : current)
      refreshSessions()
      flash(result.revoked ? `Revoked ${result.revoked} link${result.revoked === 1 ? '' : 's'}.`
        : 'No active links.')
    } catch (error) {
      if (!mountedRef.current) return
      if (error?.status >= 400 && error.status < 500) flash('Revoke failed')
      else {
        setShareUnknown(shareSid, true)
        refreshSessions()
        flash('Revoke uncertain · retry to confirm')
      }
    } finally {
      if (shareActionSessionRef.current === shareSid) shareActionSessionRef.current = null
      if (mountedRef.current) setShareBusySid(current => current === shareSid ? null : current)
    }
  }
  const retryHandlerFor = (assistantIndex) => {
    if (historical) return null
    if (msgs[assistantIndex]?.recoveryBlocked) return null
    const prior = [...msgs.slice(0, assistantIndex)].reverse().find(x => x.role === 'user')
    if (prior?.context?.files?.length && !prior.retryPayload) return null
    if (shareUnknown || shareVerifying) {
      return () => verifyShareStatus(sidRef.current || sid)
    }
    if ((composerPaused && !retryChecking) || shareActionSessionRef.current
        || forkActionSessionRef.current === (sidRef.current || sid)) return null
    return () => retryTurn(assistantIndex)
  }
  // /api/start already knows how to seed a run with its creation conversation.  Scope it from the
  // latest explicit Genesis command through the owning proposal message; unrelated session history,
  // attachment payloads, system/tool records, and unfinished turns fail closed.
  const launchChatThrough = index => proposalLaunchChat(msgs, index)
  const revertState = change => {
    const key = assistantRevertKey(sid, change)
    return key ? { busy: revertingChanges.has(key), done: revertedChanges.has(key) } : {}
  }
  const attentionPermissionHandoffActive = attentionPermissionFocus.phase !== 'idle'
  const attentionPermissionTargetVisible = attentionPermissionFocus.phase === 'ready'
    && pending.some(request => request?.id === attentionPermissionFocus.id)
  const renderThread = () => <>
    {msgs.length === 0 && <div className="asst-empty">
      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
        Ask anything — inspect the code, read your runs, steer or create runs{runId ? ` · run “${runId}” is open` : ''}.
      </div>
      {!input.trim() && <div className="asst-hints">
        {HINTS.map(h => <button key={h.label} className="asst-hint"
          disabled={composerEditingPaused}
          onClick={() => {
            if (openSessionPendingRef.current) return
            setInput(current => current.trim() ? current : h.text)
            inputRef.current?.focus()
          }}>{h.label}</button>)}
      </div>}
    </div>}
    {msgs.map((m, i) => <React.Fragment key={i}>
      {m.role === 'user' && m.context && <div className="asst-ctx-cap" title="context attached to this message">
        {m.context.run && <span className="asst-ctx-i"><OpIcon name="folder" size={10} /> {m.context.run}</span>}
        {(m.context.refs || []).map(r => <span key={'r' + r} className="asst-ctx-i">#{r}</span>)}
        {(m.context.files || []).map(f => <span key={'f' + f} className="asst-ctx-i"><OpIcon name="clip" size={10} /> {f}</span>)}
      </div>}
      <Turn m={m} runsById={runsById} readOnly={historical} onRevert={historical ? null : onRevert}
        onRetry={retryHandlerFor(i)}
        retryLabel={shareUnknown || shareVerifying
          ? shareVerifying ? 'Checking status…' : 'Verify status' : 'Retry'}
        retryBusy={shareVerifying || retryChecking}
        onOpenSettings={openAssistantSettings} onRunOpen={openRunFromAssistant}
        launchChat={launchChatThrough(i)}
        revertState={revertState}
        launchSessionId={sid} launchMessageId={m.turn_id || m.id} launchMessageIndex={i}
        launchDrafts={launchDrafts}
        launchDisclosures={launchDisclosures}
        onLaunchDraft={(key, draft) => setLaunchDrafts(current => retainLaunchDraft(current, key, draft))}
        onLaunchDisclosure={(key, open) => setLaunchDisclosures(
          current => retainLaunchDisclosure(current, key, open))}
        onLaunchStarted={key => {
          setLaunchDrafts(current => removeLaunchDraft(current, key))
          setLaunchDisclosures(current => retainLaunchDisclosure(current, key, false))
        }} />
    </React.Fragment>)}
    {!historical && pending.length > 0 && <div className="asst-perm-region" role="region"
      aria-label={`${pending.length} pending Assistant approval${pending.length === 1 ? '' : 's'}`}
      aria-live="assertive" aria-atomic="false">
      {pending.map((req, index) => {
        const focusedFromAttention = attentionPermissionTargetVisible
          && req.id === attentionPermissionFocus.id
        return <PermCard key={req.id} req={req} onResolve={resolvePerm}
          busy={resolvingPerms.has(req.id)}
          autoFocus={index === pending.length - 1}
          suppressAutoFocus={attentionPermissionHandoffActive}
          focusRequest={focusedFromAttention ? attentionPermissionFocus.token : 0}
          onFocused={onAttentionPermissionFocused}
          onFocusFailed={onAttentionPermissionFocusFailed} />
      })}
    </div>}
    {/* The streaming placeholder is itself in `msgs`; its Turn renders the activity timeline +
        the "thinking" indicator — no separate block here (which would double the label). */}
  </>

  const fileChips = files.length > 0 && <div className="asst-files">
    {files.map(f => <span key={f.name} className="chip xs file" title={`${(f.size / 1024).toFixed(1)} KB${f.truncated ? ' · truncated' : ''}`}>
      <OpIcon name="doc" size={11} /> {f.name}
      <button className="chip-x" onClick={() => removeFile(f.name)}
        disabled={composerEditingPaused} aria-label={`Remove ${f.name}`}>✕</button></span>)}
  </div>

  const attachBtn = (cls) => <button className={cls} aria-label="Attach text files"
    title={historical ? readOnlyShort : sessionOpening ? 'Wait for the selected Assistant chat to finish opening'
      : shareUnknown ? 'Attach to this draft; sending is paused until public-link status is verified'
      : shareBusy ? 'Wait for the current public-link action'
        : forkingCurrentSession ? 'Wait for this chat to finish forking'
          : draftRunMismatch ? draftRunMismatchMessage
            : pendingFileReads > 0 ? 'Wait for the selected attachment to finish reading' : 'attach text file(s)'}
    disabled={historical || composerEditingPaused || draftRunMismatch || pendingFileReads > 0}
    onClick={() => fileRef.current?.click()}>
    <OpIcon name="clip" size={14} /></button>

  // mode selector row — placed BELOW the input in the side + full composers.
  const modeRow = <div className="asst-moderow">
    <div className="asst-modes">
      {MODES.map(x => <button key={x.id} aria-pressed={x.id === mode}
        className={'asst-mode' + (x.id === mode ? ' on' : '')}
        disabled={historical || composerEditingPaused} title={historical ? readOnlyShort
          : sessionOpening ? 'Wait for the selected Assistant chat to finish opening'
            : shareUnknown ? 'Choose a draft mode; sending is paused until public-link status is verified'
            : forkingCurrentSession ? 'Wait for this chat to finish forking' : x.hint}
        onClick={() => { if (!openSessionPendingRef.current) setComposerMode(x.id) }}>{x.label}</button>)}
    </div>
    <span className="asst-modehint muted">{activeMode.hint}</span>
  </div>

  // The /command hint listbox — one definition reused by the docked bar AND the side/full composers, so
  // command discovery is identical everywhere. Only one view renders at a time, so the shared id is unique.
  const suggestionPop = showSuggestions ? <div className="cmdbar-pop" id="assistant-command-listbox"
      role="listbox" aria-label="Assistant commands">
    {suggestions.map((c, index) => <button key={c.name} id={`assistant-command-option-${index}`}
      className="cmdbar-pop-item" role="option" tabIndex={-1} aria-selected={index === activeSuggestionIndex}
      disabled={composerEditingPaused}
      onMouseMove={() => setSuggestionIndex(index)}
      onMouseDown={(e) => { e.preventDefault(); chooseSuggestion(index) }}>
      <b>/{c.name}</b><span className="muted"> {c.desc}</span></button>)}
  </div> : null
  // Combobox ARIA shared by every composer input so the hint list is announced consistently.
  const comboAria = {
    role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': showSuggestions,
    'aria-controls': 'assistant-command-listbox',
    'aria-activedescendant': activeSuggestionIndex >= 0 ? `assistant-command-option-${activeSuggestionIndex}` : undefined,
  }

  // A full composer (textarea + attach + send/stop + mode row below) — reused by side + full views.
  const composer = (placeholder) => <div
    className={'chat-in asst-in' + (directConfirm ? ' direct-confirming' : '')}>
    {historical && <div className="assistant-history-lock">{runLoadingLocked
      ? 'Verifying this run · Assistant actions are paused.'
      : runUnavailableLocked
        ? 'Run unavailable · Assistant actions are paused. Retry the run or return to the run list.'
        : startOverLocked
      ? 'Start over unresolved · Assistant paused until the exact request is recovered.'
      : reviewLocked ? 'Read-only review · Assistant actions are unavailable.'
        : staleDiagnostic
          ? 'Diagnostic link generation mismatch · Assistant paused. Open the current generation to continue.'
          : `History seq ${runAccess.seq} · Assistant paused. Return live to ask about or change this run.`}</div>}
    {directConfirm?.phase === 'checking' && <div className="assistant-command-pending">
      <span ref={directConfirmStatusRef} tabIndex={-1} role="status"
        aria-live="polite" aria-atomic="true">
        Verifying the exact approval target · nothing sent…
      </span>
      <button ref={directConfirmCancelRef} type="button" className="btn sm ghost"
        onClick={cancelDirectConfirmation}>Cancel · keep draft</button>
    </div>}
    {directConfirm?.phase === 'ready' && <div
      className="asst-perm risk-consequential assistant-direct-confirm"
      role="alertdialog" aria-modal="false"
      aria-labelledby="assistant-direct-confirm-title"
      aria-describedby="assistant-direct-confirm-details">
      <div className="asst-perm-h">
        <span className="asst-perm-badge">confirmation required</span>
        <b id="assistant-direct-confirm-title">{directConfirm.title}</b>
        <span className="asst-perm-risk consequential">consequential</span>
      </div>
      <dl className="asst-perm-details" id="assistant-direct-confirm-details">
        <div><dt>Run</dt><dd><code>{directConfirm.runId}</code></dd></div>
        <div><dt>Version</dt><dd><code className="assistant-direct-identity">
          {directConfirm.expectedGeneration}</code></dd></div>
        {Number.isSafeInteger(directConfirm.nodeGeneration)
          && <div><dt>Attempt</dt><dd>experiment #{directConfirm.direct.arg}
            {' · generation '}{directConfirm.nodeGeneration}</dd></div>}
        <div><dt>Command</dt><dd><code>{directConfirm.commandText}</code></dd></div>
        <div><dt>Mode</dt><dd>{directConfirm.modeLabel}</dd></div>
        <div><dt>Consequence</dt><dd>{directConfirm.consequence}</dd></div>
      </dl>
      <div className="asst-perm-actions">
        <button ref={directConfirmCancelRef} type="button" className="btn sm ghost"
          onClick={cancelDirectConfirmation}>Cancel · keep draft</button>
        <button type="button"
          className={`btn sm ${directConfirm.danger ? 'danger' : 'primary'}`}
          onClick={confirmDirectCommand}>{directConfirm.actionLabel}</button>
      </div>
    </div>}
    {currentShareAckNotice && <div className="assistant-command-pending error" role="alert"
      aria-live="assertive" aria-atomic="true">
      <span>{currentShareAckNotice.message}</span>
      <button type="button" className="btn sm ghost"
        onClick={() => setShareAckNotice(current => current?.sid === sid ? null : current)}>
        Dismiss
      </button>
    </div>}
    {(shareUnknown || shareVerifying) && <div className="assistant-command-pending assistant-share-pending error">
      <span>{shareStatusMessage}</span>
      <button type="button" className="btn sm" onClick={retryShareStatus}
        disabled={shareBusy || shareVerifying || sessionsStatus === 'loading' || sessionsStatus === 'refreshing'}>
        {shareVerifying || sessionsStatus === 'loading' || sessionsStatus === 'refreshing'
          ? 'Checking status…' : 'Retry status'}
      </button>
      <button type="button" className="btn sm ghost" onClick={revokeCurrentShares}
        disabled={shareBusy || forkingCurrentSession}>Unshare</button>
    </div>}
    {sessionOpening && <div className="assistant-command-pending" role="status"
      aria-live="polite" aria-atomic="true">
      {attentionPermissionFocus.phase === 'loading'
        ? 'Opening the selected Assistant chat; the exact approval will load next…'
        : 'Opening the selected Assistant chat…'}
    </div>}
    {attentionPermissionFocus.phase === 'loading' && !sessionOpening
      && <div className="assistant-command-pending" role="status"
        aria-live="polite" aria-atomic="true">
        Loading the exact Assistant approval…
      </div>}
    {retryChecking && <div className="assistant-command-pending">
      Checking the saved Assistant turn…
    </div>}
    {(commandBusy || directFailure) && <div ref={commandStatusRef} tabIndex={-1}
      className={'assistant-command-pending' + (showDirectFailure ? ' error' : '')}
      role={directNeedsAlert ? 'alert' : 'status'} aria-live={directNeedsAlert ? 'assertive' : 'polite'} aria-atomic="true">
      <span>{showDirectFailure ? directFailureText : pendingCommandText}</span>
      {canCheckDirect && <button className="btn sm" disabled={directRecoveryPaused}
        onClick={checkDirect}>Check same command</button>}
      {directPending?.protocolInvalid && <button className="btn sm ghost"
        disabled={!!directPending.checking} onClick={dismissProtocolDirect}>Dismiss</button>}
      {canRetryDirect && <button className="btn sm" disabled={directRecoveryPaused}
        onClick={retryDirect}>Retry same command</button>}
      {showDirectFailure && <button className="btn sm ghost" onClick={dismissDirectFailure}>Dismiss</button>}
    </div>}
    {draftRunMismatch && <div className="assistant-command-pending error" role="alert"
      aria-live="assertive" aria-atomic="true">
      <span>{draftRunMismatchMessage}</span>
      <button type="button" className="btn sm ghost" disabled={sessionOpening} onClick={useDraftHere}>
        {runId ? 'Use in this run' : 'Use without a run'}
      </button>
    </div>}
    {pendingFileReads > 0 && <div className="assistant-command-pending" role="status"
      aria-live="polite" aria-atomic="true">
      Reading selected attachment…
    </div>}
    {runId && refNodes(input).length > 0 && <div className="cmdbar-ctx">
      {refNodes(input).map(id => <span key={id} className="chip xs">#{id}
        <button className="chip-x" aria-label={`Detach experiment ${id}`} disabled={composerEditingPaused}
          onClick={() => {
            if (openSessionPendingRef.current) return
            setInput(input.replace(new RegExp(`#(?:node-)?${id}\\b`, 'gi'), '').replace(/\s{2,}/g, ' ').trim())
          }}>✕</button></span>)}
    </div>}
    {fileChips}
    <div className="asst-inrow">
      {suggestionPop}
      {attachBtn('asst-attach')}
      <textarea className="text" ref={inputRef} value={input}
        aria-label="Assistant message" aria-describedby={[
          draftingNewRun ? 'assistant-new-run-hint' : '',
          shareUnknown || shareVerifying ? 'assistant-share-status' : '',
        ].filter(Boolean).join(' ') || undefined}
        {...comboAria}
        disabled={historical || commandBusy || composerEditingPaused} onChange={changeInput} onKeyDown={onKey}
        placeholder={historical ? readOnlyShort : shareUnknown
          ? 'Public-link status unknown · keep drafting; Send will verify it' : shareBusy
            ? 'Finishing public-link action…' : forkingCurrentSession
              ? 'Forking this chat…' : placeholder} />
      {busy
        ? <button className="btn sm" aria-label="Stop Assistant" title="stop" onClick={stop}>■</button>
        : <button className="btn sm primary"
            disabled={sessionOpening || retryChecking || turnStarting || historical || commandBusy || composerEditingPaused || draftRunMismatch || pendingFileReads > 0
              || (!input.trim() && files.length === 0)}
            onClick={send}>{sessionOpening ? 'Opening\u2026'
              : retryChecking ? 'Checking\u2026'
              : turnStarting ? 'Starting\u2026'
              : shareUnknown || shareVerifying ? shareVerifying ? 'Checking…' : 'Verify to send'
              : commandBusy || shareBusy ? 'Waiting…'
              : forkingCurrentSession ? 'Forking…'
                : pendingFileReads > 0 ? 'Reading…' : 'Send'}</button>}
    </div>
    {draftingNewRun && <div id="assistant-new-run-hint" className="asst-new-run-hint" role="note">
      Describe the goal after /new, then Send. Send uses the configured model to draft a launch card.
      Nothing starts until you review it and press Start run.
    </div>}
    {modeRow}
  </div>

  const hiddenFileInput = <input ref={fileRef} type="file" multiple style={{ display: 'none' }}
    disabled={historical || composerEditingPaused || draftRunMismatch || pendingFileReads > 0}
    onChange={e => { onFiles(e.target.files); e.target.value = '' }} />

  useEffect(() => {
    const expiryMs = Number(currentSession?.share_expires_at) * 1000
    if (!sid || !Number.isFinite(expiryMs) || expiryMs <= 0) return undefined
    let timer = null
    let cancelled = false
    const arm = () => {
      if (cancelled) return
      const remaining = expiryMs - Date.now()
      if (remaining <= 0) { refreshSessions(); return }
      // Browsers cap one timeout at roughly 24.8 days; re-arm for longer (up to 90-day) links.
      timer = window.setTimeout(arm, Math.min(remaining + 250, 2_147_000_000))
    }
    arm()
    return () => { cancelled = true; if (timer != null) window.clearTimeout(timer) }
  }, [sid, currentSession?.share_expires_at, refreshSessions])
  useEffect(() => {
    if (!sid || !shareCopy) return undefined
    if (!validAssistantShareFallback(shareCopy)) {
      clearShareCopy(sid)
      setShareUnknown(sid, true)
      refreshSessions()
      return undefined
    }
    const expiryMs = shareCopy.expiresAt * 1000
    let timer = null
    let cancelled = false
    const arm = () => {
      if (cancelled) return
      const remaining = expiryMs - Date.now()
      if (remaining <= 0) {
        // This exact capability is no longer copyable. Reconcile the session separately because a
        // different frozen or live link may still be active for the same chat.
        clearShareCopy(sid)
        setShareUnknown(sid, true)
        refreshSessions()
        return
      }
      timer = window.setTimeout(arm, Math.min(remaining + 250, 2_147_000_000))
    }
    arm()
    return () => { cancelled = true; if (timer != null) window.clearTimeout(timer) }
  }, [sid, shareCopy, clearShareCopy, setShareUnknown, refreshSessions])
  return <>
    {hiddenFileInput}
    <output className="sr-only" aria-live="polite" aria-atomic="true">
      {sessionOpening ? view === 'bar' ? 'Opening Assistant chat.' : ''
        : retryChecking ? 'Checking saved Assistant turn.'
          : turnStarting ? 'Starting Assistant response.'
            : busy ? 'Assistant is responding.' : replyAnnouncement}
    </output>
    <div id="assistant-share-status" className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {shareStatusMessage}
    </div>

    {/* ── bottom bar — ONLY in bar view (moves into the side panel otherwise) ── */}
    {view === 'bar' && <div className={'cmdbar-wrap'}><div className={'cmdbar-dock' + (sessionOpening || retryChecking || turnStarting || busy || commandBusy || forkingCurrentSession ? ' thinking' : '') + (hasNew ? ' fresh' : '')}>
      <button className="cmdbar-ic" aria-label="Open full Assistant" title="open the full assistant" onClick={openFull}>✦</button>
      {launchRecoveryButton}
      <button type="button" className={`cmdbar-mode mode-${mode}`}
        aria-label={`Assistant mode for the next message: ${activeMode.label}. ${activeMode.hint}. Open Assistant to inspect or change.`}
        title={`${activeMode.label} · ${activeMode.hint}`} onClick={openSide}>
        <span className="cmdbar-mode-prefix">Mode · </span><span>{activeMode.label}</span>
      </button>
      <div className="cmdbar-field">
        {(refNodes(input).length > 0 || files.length > 0) && <div className="cmdbar-ctx">
          {runId && refNodes(input).map(id => <span key={id} className="chip xs">#{id}</span>)}
          {files.map(f => <span key={f.name} className="chip xs file"><OpIcon name="doc" size={10} /> {f.name}
            <button className="chip-x" aria-label={`Remove ${f.name}`}
              disabled={composerEditingPaused} onClick={() => removeFile(f.name)}>✕</button></span>)}
        </div>}
        {suggestionPop}
        <input className="cmdbar-in" ref={inputRef} value={input}
          aria-label="Assistant command or question"
          aria-describedby={shareUnknown || shareVerifying ? 'assistant-share-status' : undefined}
          role="combobox" aria-autocomplete="list" aria-expanded={showSuggestions}
          aria-controls="assistant-command-listbox"
          aria-activedescendant={activeSuggestionIndex >= 0 ? `assistant-command-option-${activeSuggestionIndex}` : undefined}
          disabled={historical || commandBusy || composerEditingPaused} onChange={changeInput} onKeyDown={onKey}
          placeholder={historical ? readOnlyShort : shareUnknown
            ? 'Public-link status unknown · keep drafting; Send will verify it' : shareBusy
              ? 'Finishing public-link action…' : forkingCurrentSession
                ? 'Forking this chat…' : runId
                  ? 'Command or ask…  /stop · pause · #12 to attach an experiment · or describe what to do'
                  : 'Describe a run to start, or ask the assistant…  ( / for commands )'} />
      </div>
      {attachBtn('cmdbar-attach')}
      {shareUnknown || shareVerifying
        ? <span className="cmdbar-status thinking recovery error">
            <span><span className="cmdbar-who">{shareVerifying
              ? 'checking public-link status' : 'public-link status unknown'}</span>
              {' · '}draft available · nothing sent</span>
            <button type="button" className="btn sm" onClick={retryShareStatus}
              disabled={shareBusy || shareVerifying || sessionsStatus === 'loading' || sessionsStatus === 'refreshing'}>
              {shareVerifying ? 'Checking…' : 'Retry status'}
            </button>
            <button type="button" className="btn sm ghost" onClick={revokeCurrentShares}
              disabled={shareBusy}>Unshare</button>
          </span>
        : sessionOpening
          ? <span className="cmdbar-status thinking">
              <span className="cmdbar-pip" /> opening selected chat...
            </span>
        : retryChecking
          ? <span className="cmdbar-status thinking">
              <span className="cmdbar-pip" /> checking saved turn...
            </span>
        : turnStarting
          ? <span className="cmdbar-status thinking">
              <span className="cmdbar-pip" /> starting response...
            </span>
        : busy
        ? <span className={'cmdbar-status thinking' + (liveShareActive ? ' assistant-live-share' : '')}>
            <span className="cmdbar-pip" />
            {liveShareActive
              ? <><span className="cmdbar-who">live public link</span> this reply remains public · thinking…</>
              : ' thinking…'}
          </span>
        : commandBusy
          ? <span ref={commandStatusRef} tabIndex={-1}
              className={'cmdbar-status thinking' + (canCheckDirect ? ' recovery' : '')}
              role={directNeedsAlert ? 'alert' : 'status'}
              aria-live={directNeedsAlert ? 'assertive' : 'polite'} aria-atomic="true">
              <span className="cmdbar-pip" /> {pendingCommandText || 'Waiting for command…'}
              {canCheckDirect && <button className="btn sm" disabled={directRecoveryPaused}
                onClick={checkDirect}>Check</button>}
              {directPending?.protocolInvalid && <button className="btn sm ghost"
                disabled={!!directPending.checking} onClick={dismissProtocolDirect}>Dismiss</button>}
            </span>
          : directFailure
            ? <span ref={commandStatusRef} tabIndex={-1} className="cmdbar-status thinking recovery error"
                role="alert" aria-live="assertive" aria-atomic="true">
                <span>{directFailureText}</span>
                {canRetryDirect && <button className="btn sm" disabled={directRecoveryPaused}
                  onClick={retryDirect}>Retry same command</button>}
                <button className="btn sm ghost" onClick={dismissDirectFailure}>Dismiss</button>
              </span>
          : currentShareAckNotice
            ? <span className="cmdbar-status thinking recovery error" role="alert"
                aria-live="assertive" aria-atomic="true">
                <span><span className="cmdbar-who">nothing sent</span> · public-link state changed</span>
                <button type="button" className="btn sm ghost"
                  onClick={() => setShareAckNotice(current => current?.sid === sid ? null : current)}>
                  Dismiss
                </button>
              </span>
          : draftRunMismatch
            ? <span className="cmdbar-status thinking recovery error" role="alert"
                aria-live="assertive" aria-atomic="true" title={draftRunMismatchMessage}>
                <span><span className="cmdbar-who">draft held</span> · from {draftRunSource}</span>
                <button type="button" className="btn sm ghost" disabled={sessionOpening}
                  onClick={useDraftHere}>Use here</button>
              </span>
          : forkingCurrentSession
            ? <span className="cmdbar-status thinking" role="status"
                aria-live="polite" aria-atomic="true">
                <span className="cmdbar-pip" /> forking this chat…
              </span>
          : pendingFileReads > 0
            ? <span className="cmdbar-status thinking" role="status"
                aria-live="polite" aria-atomic="true">
                <span className="cmdbar-pip" /> reading selected attachment…
              </span>
          : liveShareActive
          ? <button className="cmdbar-status preview assistant-live-share" type="button"
              title="This chat has a live public link. Open the full Assistant to revoke it."
              onClick={openFull}>
              <span className="cmdbar-who">live public link</span> new messages remain public
              <span className="cmdbar-more"> ▸</span>
            </button>
          : preview
          ? <button className="cmdbar-status preview" title="open the conversation" onClick={openSide}>
              <span className="cmdbar-who">assistant</span> {preview}<span className="cmdbar-more"> ▸</span></button>
          : null}
      {/* send / stop share ONE slot (you can't send mid-turn) — kept separate from the side button
          so stopping never opens a view. */}
      {busy
        ? <button className="cmdbar-go stop" aria-label="Stop Assistant" title="stop the assistant" onClick={stop}>■</button>
        : <button className="cmdbar-go"
            aria-label={sessionOpening ? 'Opening selected Assistant chat'
              : retryChecking ? 'Checking saved Assistant turn'
                : turnStarting ? 'Starting Assistant response' : shareUnknown || shareVerifying ? shareVerifying
              ? 'Checking public-link status; nothing sent'
              : 'Verify public-link status before sending' : 'Send Assistant message'}
            title={sessionOpening ? 'Wait for the selected Assistant chat to finish opening'
              : retryChecking ? 'Wait for the saved Assistant turn check to finish'
                : turnStarting ? 'Wait for the Assistant response to start'
              : draftRunMismatch ? draftRunMismatchMessage
              : shareUnknown || shareVerifying ? shareVerifying
                ? 'Checking public-link status; your draft remains editable'
                : 'Messaging paused until public-link status is verified'
              : commandBusy ? 'Waiting for the current run command' : historical ? readOnlyShort
                : forkingCurrentSession ? 'Wait for this chat to finish forking'
                  : pendingFileReads > 0 ? 'Wait for the selected attachment to finish reading' : 'send (Enter)'}
            disabled={sessionOpening || retryChecking || turnStarting || historical || commandBusy || composerEditingPaused || draftRunMismatch || pendingFileReads > 0
              || (!input.trim() && files.length === 0)} onClick={send}>▶</button>}
      <button className="cmdbar-drawer-btn" aria-label="Open Assistant in side view"
        title="open chat on the right (side view)" onClick={openSide}><OpIcon name="chat" size={13} /></button>
      {visibleToast && <div className="cmdbar-toast" role="status" aria-live="polite" aria-atomic="true">{visibleToast}</div>}
    </div></div>}

    {/* ── right side panel (resizable) — the composer lives INSIDE it; no bottom bar while open ── */}
    {view === 'side' && compactAssistant && <div className="asst-side-backdrop" aria-hidden="true"
      onPointerDown={collapseToBar} />}
    {view === 'side' && <aside ref={sideDialogRef} className="asst-side-panel" aria-label="Assistant"
      role={compactAssistant ? 'dialog' : undefined} aria-modal={compactAssistant ? 'true' : undefined}
      aria-hidden={deleteConfirm ? 'true' : undefined} inert={deleteConfirm ? '' : undefined}
      tabIndex={compactAssistant ? -1 : undefined} style={{ width: sideW }}>
      {!compactAssistant && <div className="asst-resize" role="separator" aria-label="Resize Assistant panel"
        aria-orientation="vertical" aria-valuemin={320} aria-valuemax={maxSideWidth()}
        aria-valuenow={Math.round(sideW)} tabIndex={0} onPointerDown={startResize}
        onKeyDown={resizeWithKeys} title="Drag or use arrow keys to resize" />}
      <div className="asst-drawer-h">
        <b className="asst-drawer-ttl">Assistant</b>
        {ctxChip}
        {launchRecoveryButton}
        {sid && (currentSession?.shared || shareCopy || shareUnknown)
          && <button type="button" className={'btn sm assistant-share-state'
            + (shareUnknown || liveShareActive ? ' warn' : '')}
            title="Open the full Assistant to inspect or revoke public links"
            onClick={openFull}>
            {shareUnknown ? 'share status unknown · messaging paused' : liveShareActive
              ? 'LIVE · new replies public' : 'snapshot public'}
          </button>}
        <span className="spacer" style={{ flex: 1 }} />
        {compactAssistant && attentionIndicator.active && <AttentionLauncher
          indicator={attentionIndicator} embedded onClick={openAttentionCenter} />}
        <button className="btn sm ghost" aria-label="Start a new Assistant chat"
          title={turnStarting ? 'Wait for the new Assistant chat to finish starting'
            : retryChecking ? 'Wait for the saved turn check to finish'
              : directConfirm ? 'Resolve the run-command confirmation first' : 'new chat'}
          disabled={turnStarting || retryChecking || !!directConfirm} onClick={newChat}>＋ Chat</button>
        <button className="btn sm ghost" title="expand to the full view" onClick={openFull}>⤢ full</button>
        <button className="btn sm ghost"
          title={directConfirm ? 'Cancel the run-command confirmation and keep the draft' : 'collapse to the bar'}
          onClick={directConfirm ? cancelDirectConfirmation : collapseToBar}>
          {directConfirm ? 'Cancel command' : '▾ bar'}</button>
      </div>
      <div className="asst-drawer-feed" ref={feedRef} role="log" aria-label="Assistant transcript"
        aria-live="off" aria-busy={busy} tabIndex={0}
        onScroll={onFeedScroll}>{renderThread()}</div>
      {composer('Message the assistant…  (/ for commands · Enter to send)')}
      {visibleToast && <div className="cmdbar-toast side" role="status" aria-live="polite" aria-atomic="true">{visibleToast}</div>}
    </aside>}

    {/* ── full page — dedicated OPAQUE view (sessions · thread · composer) ── */}
    {view === 'full' && <div ref={fullDialogRef} className="asst-view asst-full" role="dialog"
      aria-modal="true" aria-label="Assistant" aria-hidden={deleteConfirm ? 'true' : undefined}
      inert={deleteConfirm ? '' : undefined} tabIndex={-1}>
      <div className="asst-side">
        <div className="asst-side-h">
          <button className="btn sm"
            title={directConfirm ? 'Cancel the run-command confirmation and keep the draft' : 'fold back to the bar'}
            onClick={directConfirm ? cancelDirectConfirmation : collapseToBar}>
            {directConfirm ? 'Cancel command' : '▾ bar'}</button>
          <span className="ttl" style={{ flex: 1 }}>Assistant</span>
          <button className="btn sm primary" aria-label="Start a new Assistant chat"
            title={turnStarting ? 'Wait for the new Assistant chat to finish starting'
              : retryChecking ? 'Wait for the saved turn check to finish'
                : directConfirm ? 'Resolve the run-command confirmation first' : undefined}
            disabled={turnStarting || retryChecking || !!directConfirm} onClick={newChat}>+ Chat</button>
        </div>
        <div ref={sessionsRef} className="asst-sessions"
          aria-busy={sessionsStatus === 'loading' || sessionsStatus === 'refreshing'}>
          {sessionsStatus === 'loading' && <div className="asst-session-resource" role="status">
            <span>Loading chats…</span>
          </div>}
          {sessionsStatus === 'refreshing' && <div className="asst-session-resource" role="status">
            <span>Refreshing chats…</span>
          </div>}
          {['error', 'stale'].includes(sessionsStatus) && <div
            className={`asst-session-resource ${sessionsStatus}`}
            role={sessionsStatus === 'error' ? 'alert' : 'status'}>
            <OpIcon name="alert" size={13} />
            <span>{sessionsStatus === 'stale'
              ? 'Showing the last loaded chat list. Refresh failed.'
              : 'Chats could not be loaded.'}</span>
            <button type="button" className="btn xs" onClick={refreshSessions}>Retry</button>
          </div>}
          {sessionsStatus === 'ready' && sessions.length === 0
            && <div className="muted asst-session-empty">No chats yet.</div>}
          {sessions.map(s => <div key={s.id} className={'asst-sess'
            + (s.id === sid ? ' active' : '') + (String(openingSid || '') === String(s.id) ? ' opening' : '')
            + (s.cleanup_required ? ' cleanup-required' : '')}>
            <button type="button" className="asst-sess-open"
              aria-current={s.id === sid ? 'page' : undefined}
              aria-busy={String(openingSid || '') === String(s.id) || undefined}
              aria-label={String(openingSid || '') === String(s.id)
                ? `Opening chat ${s.title || 'Chat'}` : s.cleanup_required
                ? 'Incomplete chat deletion; use Delete to retry cleanup'
                : undefined}
              title={String(openingSid || '') === String(s.id) ? 'Opening this Assistant chat…'
                : turnStarting ? 'Wait for the new Assistant chat to finish starting'
                  : retryChecking ? 'Wait for the saved turn check to finish'
                    : s.cleanup_required ? 'This partial chat cannot be opened. Delete it to retry cleanup.' : undefined}
              disabled={s.cleanup_required === true || turnStarting || retryChecking || !!directConfirm
                || String(openingSid || '') === String(s.id)}
              onClick={() => openSession(s.id)}>
              <span className="asst-sess-t">{s.title || 'Chat'}</span>
              <span className="asst-sess-m">{String(openingSid || '') === String(s.id)
                ? 'Opening…' : s.cleanup_required
                ? 'Cleanup required · delete to retry'
                : s.shared
                  ? `${Number(s.share_count) > 1 ? `${s.share_count} public links` : 'Public link'} ${s.share_live ? 'LIVE · new replies public' : 'active'}${s.share_expires_at ? ` · until ${fmtDate(s.share_expires_at)}` : ''}`
                  : fmtAgo(s.updated)}</span>
            </button>
            <button type="button" className="asst-sess-x" onClick={(e) => requestDeleteSession(s, e)}
              disabled={forkBusySid === s.id || turnStarting || !!directConfirm
                || String(openingSid || '') === String(s.id)
                || (s.id === sid && (retryChecking || busy || pending.length > 0))}
              title={forkBusySid === s.id ? 'Wait for this chat to finish forking'
                : turnStarting ? 'Wait for the new Assistant chat to finish starting'
                  : String(openingSid || '') === String(s.id) ? 'Wait for this chat to finish opening'
                    : s.id === sid && retryChecking ? 'Wait for the saved Assistant turn check to finish'
                      : s.id === sid && (busy || pending.length > 0)
                        ? 'Stop or finish this Assistant turn before deleting the chat' : undefined}
              aria-label={s.cleanup_required
                ? `Retry cleanup for ${s.title || 'incomplete chat deletion'}`
                : `Delete chat ${s.title || 'Chat'}`}>✕</button>
          </div>)}
        </div>
      </div>
      <div className="asst-main">
        <div className="asst-main-h">
          <span className="ttl" style={{ flex: 1 }}>{currentSession?.title || 'New chat'}</span>
          {attentionIndicator.active && <AttentionLauncher
            indicator={attentionIndicator} embedded onClick={openAttentionCenter} />}
          {ctxChip}
          {launchRecoveryButton}
          {sid && <button className="btn sm ghost" type="button"
            title={forkBusySid === sid ? 'Forking this chat…'
              : forkBusy ? 'Another Assistant fork is still in progress'
              : deletingCurrentSession ? 'This chat is being deleted'
                : currentForkRecovery
                  ? 'Check the exact pending fork before starting another'
                  : shareTurnIncomplete
                  ? 'Wait for the current Assistant reply to finish or recover it before forking'
                  : shareBusy ? 'Wait for the current share action before forking'
                    : 'Fork this complete chat into a new session'}
            disabled={forkBusy || shareBusy || deletingCurrentSession || !!directConfirm
              || (shareTurnIncomplete && !currentForkRecovery)}
            onClick={forkCurrentSession}>{forkBusySid === sid ? 'forking…'
              : currentForkRecovery ? '⑂ check fork' : '⑂ fork'}</button>}
          {sid && (currentSession?.shared || shareCopy || shareUnknown)
            && <span className={'pill assistant-share-state'
              + (shareUnknown || liveShareActive ? ' warn' : '')}>
              {shareUnknown ? 'share state unknown' : liveShareActive
                ? 'LIVE public link · new replies public' : 'public snapshot active'}
            </span>}
          {shareUnknown && <button className="btn sm" type="button" onClick={retryShareStatus}
            disabled={shareBusy || shareVerifying || sessionsStatus === 'loading' || sessionsStatus === 'refreshing'}
            title="Verify the exact active public-link capabilities before messaging">
            {shareVerifying || sessionsStatus === 'loading' || sessionsStatus === 'refreshing'
              ? 'checking share…' : 'retry share status'}
          </button>}
          {sid && !currentSession && !shareCopy && !shareUnknown
            && <button className="btn sm ghost" type="button"
              disabled={sessionsStatus === 'loading' || sessionsStatus === 'refreshing'}
              title="Verify that this chat has no existing public links before creating another"
              onClick={refreshSessions}>
              {sessionsStatus === 'loading' || sessionsStatus === 'refreshing'
                ? 'checking share…' : 'retry share status'}
            </button>}
          {/* A share link is a separate secret with an expiry — not this chat's id — and it is frozen
              at the turns that exist right now, so anything said afterwards stays private. "unshare"
              revokes every link for the chat without deleting the conversation. */}
          {sid && currentSession && !deletingCurrentSession && !currentSession.shared
            && !shareUnknown && !shareCopy
            && <button className="btn sm ghost"
              title={forkingCurrentSession
                ? 'Wait for this chat to finish forking before creating a snapshot'
                : shareTurnIncomplete
                ? 'Wait for the current Assistant turn to finish before freezing a complete snapshot'
                : 'Create and copy a frozen read-only snapshot'}
              disabled={shareBusy || forkingCurrentSession || shareTurnIncomplete || !!directConfirm}
              onClick={async () => {
            const shareSid = sid
            if (shareActionSessionRef.current) { flash('Another share action is still in progress'); return }
            if (forkActionSessionRef.current === shareSid) {
              flash('Wait for this chat to finish forking before creating a snapshot')
              return
            }
            if (runningRef.current || turnCaptureRef.current || busy || commandBusy || pending.length > 0
                || msgs[msgs.length - 1]?.role === 'user') {
              flash('Wait for the Assistant reply to finish before creating a snapshot')
              return
            }
            if (deletingSessionsRef.current.has(shareSid)) {
              flash('This chat is being deleted')
              return
            }
            shareActionSessionRef.current = shareSid
            setShareBusySid(shareSid)
            try {
              const r = await boundedRequest(signal => assistantShare(shareSid, false, { signal }))
              if (!mountedRef.current) return
              const receipt = assistantShareReceipt(r, shareSid)
              if (!receipt) throw new Error('Invalid Assistant share receipt')
              const url = location.origin + location.pathname + receipt.relativeUrl
              setShareUnknown(shareSid, false)
              mutateSessionsLocally(current => current.map(session => session.id === shareSid
                ? { ...session, shared: true, share_count: 1,
                    share_ids: [receipt.shareId], live_share_ids: [], share_expires_at: receipt.expiresAt,
                    share_live: false } : session))
              retainShareCopy(shareSid, {
                url, expiresAt: receipt.expiresAt, shareId: receipt.shareId,
              })
              refreshSessions()
              if (sidRef.current !== shareSid) {
                flash('Snapshot created for the previous chat · reopen it to copy or revoke')
                return
              }
              try {
                if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
                await navigator.clipboard.writeText(url)
              } catch {
                if (!mountedRef.current) return
                flash('Clipboard blocked · select the visible snapshot link and copy it manually')
                return
              }
              if (!mountedRef.current || sidRef.current !== shareSid) {
                if (mountedRef.current) flash('Snapshot link copied for the previous chat')
                return
              }
              flash(`Snapshot link copied · expires ${fmtDate(receipt.expiresAt)}.`)
            } catch (error) {
              if (!mountedRef.current) return
              if (error?.status >= 400 && error.status < 500) {
                flash(error?.code === 'assistant_share_snapshot_too_large'
                  ? 'This chat is too large for a complete snapshot · fork or share a shorter chat'
                  : ['assistant_share_in_progress', 'assistant_share_incomplete',
                    'assistant_share_turn_active', 'assistant_share_turn_incomplete'].includes(error?.code)
                    ? 'Wait for a complete Assistant reply before sharing' : 'Share failed')
              } else {
                setShareUnknown(shareSid, true)
                refreshSessions()
                flash('Share uncertain · revoke before retrying')
              }
            } finally {
              if (shareActionSessionRef.current === shareSid) shareActionSessionRef.current = null
              if (mountedRef.current) setShareBusySid(current => current === shareSid ? null : current)
            }
          }}>{shareBusySid === sid ? 'working…' : '⤴ create snapshot'}</button>}
          {sid && !deletingCurrentSession && (currentSession?.shared || shareUnknown || shareCopy)
            && <button className="btn sm ghost"
              title={forkingCurrentSession
                ? 'Wait for this chat to finish forking before revoking links'
                : 'Revoke every share link'}
              disabled={shareBusy || forkingCurrentSession}
              onClick={revokeCurrentShares}>{shareBusySid === sid ? 'working…'
                : `⤫ ${shareUnknown ? 'revoke pending' : 'unshare'}`}</button>}
          <button className="btn sm ghost" title="dock to the right" onClick={openSide}>▧ side</button>
          <button className="btn sm ghost"
            title={directConfirm ? 'Cancel the run-command confirmation and keep the draft' : 'fold to the bar'}
            onClick={directConfirm ? cancelDirectConfirmation : collapseToBar}>
            {directConfirm ? 'Cancel command' : '▾ bar'}</button>
        </div>
        {shareCopy && <div className="copy-link-fallback" role="status">
          <label htmlFor={`assistant-share-fallback-${sid}`}>
            Frozen snapshot · expires {fmtDate(shareCopy.expiresAt)} · link remains visible in this tab
          </label>
          <input id={`assistant-share-fallback-${sid}`} readOnly value={shareCopy.url}
            onFocus={event => event.currentTarget.select()} />
          <button type="button" className="btn sm" onClick={async () => {
            try {
              if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
              await navigator.clipboard.writeText(shareCopy.url)
              flash('Snapshot link copied')
            } catch { flash('Clipboard blocked · select the snapshot link and copy it manually') }
          }}>Copy link</button>
          <a className="btn sm" href={shareCopy.url} target="_blank" rel="noreferrer noopener">
            Open snapshot
          </a>
        </div>}
        <div className="asst-feed" ref={feedRef} role="log" aria-label="Assistant transcript"
          aria-live="off" aria-busy={busy} tabIndex={0}
          onScroll={onFeedScroll}>{renderThread()}</div>
        {composer('Message the assistant…  (/ for commands · Enter to send)')}
        {visibleToast && <div className="cmdbar-toast side" role="status" aria-live="polite" aria-atomic="true">{visibleToast}</div>}
      </div>
    </div>}
    {deleteConfirm && <div className="overlay assistant-delete-overlay"
      onPointerDown={event => {
        if (!deleteConfirmBusy && event.target === event.currentTarget) closeDeleteConfirm()
      }}>
      <section ref={deleteDialogRef} className="modal assistant-delete-dialog" role="alertdialog"
        aria-modal="true" aria-labelledby="assistant-delete-title"
        aria-describedby="assistant-delete-description assistant-delete-warning"
        aria-busy={deleteConfirmBusy} tabIndex={-1}>
        <div className="modal-h">
          <b id="assistant-delete-title">Delete this Assistant chat?</b>
        </div>
        <div className="modal-b">
          <p id="assistant-delete-description" className="assistant-delete-copy">
            You are deleting <strong>“{deleteConfirm.title}”</strong>.
          </p>
          <p id="assistant-delete-warning" className="assistant-delete-warning">
            Its transcript will be permanently deleted, and any live Assistant work in this chat will be cancelled. This cannot be undone.
          </p>
          {deleteConfirmError && <div ref={deleteErrorRef} className="flag assistant-delete-error"
            role="alert" tabIndex={-1}>{deleteConfirmError}</div>}
          {deleteConfirmBusy && <div className="assistant-delete-progress" role="status" aria-live="polite">
            Deleting chat and cancelling live work…
          </div>}
          <div className="modal-actions">
            <button type="button" className="btn sm" data-dialog-initial-focus
              disabled={deleteConfirmBusy} onClick={closeDeleteConfirm}>Cancel</button>
            <button type="button" className="btn sm danger" disabled={deleteConfirmBusy}
              onClick={confirmDeleteSession}>{deleteConfirmBusy ? 'Deleting chat…' : 'Delete chat'}</button>
          </div>
        </div>
      </section>
    </div>}
  </>
}
