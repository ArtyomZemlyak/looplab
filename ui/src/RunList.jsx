import React, { lazy, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, fmtDate, fmtAgo, listProjects, createProject, patchProject, deleteProject, assignRun, renameRun,
  createIdempotencyKey, submitRunDeletion,
  listSupertasks, createSupertask, renameSupertask, deleteSupertask, assignSupertask,
  storageGet, storageSet } from './util.js'
import { useMediaQuery, usePoll } from './hooks.js'
import LazyBoundary from './LazyBoundary.jsx'
import ThemeSwitcher from './ThemeSwitcher.jsx'
import EnergyToggle from './EnergyToggle.jsx'
import DensityToggle from './DensityToggle.jsx'
import { OpIcon } from './icons.jsx'
import {
  ALL_RUNS as ALL, UNASSIGNED_RUNS as UNASSIGNED, filterRuns, indexProjects,
  effectiveRunStatus, metricComparable, projectRunCounts, scopeRuns, sortRuns,
} from './runIndex.js'
import { defaultCollapsedClusters } from './runMapModel.js'
import { useDialogFocus } from './useDialogFocus.js'
import { followClientRoute, nextRovingIndex } from './accessibility.jsx'
import {
  decodePortfolioViews, normalizeCompareColumns, portfolioViewSignature, upsertPortfolioView,
} from './portfolioModel.js'
import { deadlineRequest } from './requestDeadline.js'
import { getRunAccess, listStartOverRunAccesses } from './runMode.js'
import { listRunStartOverRecoveries } from './runStartOverRecovery.js'
import {
  clearRunDeletionIntent, createRunDeletionIntent, listRunDeletionRecoveries,
  loadRunDeletionIntent, saveRunDeletionIntent,
} from './runDeletionRecovery.js'

const MapView = lazy(() => import('./MapView.jsx'))
const ScopeReport = lazy(() => import('./ScopeReport.jsx'))
const RunCompare = lazy(() => import('./RunCompare.jsx'))
const SAVED_VIEWS_KEY = 'll.portfolioViews'
const COMPARE_COLUMNS_KEY = 'll.compareColumns'
const LIST_READ_TIMEOUT_MS = 12_000
const LIST_PAGE_SIZE = 200
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
const RUN_DELETION_TIMEOUT_MS = 12_000
const RUN_DELETION_POLL_MS = 2_500

export function useResource(read, initial) {
  const [data, setData] = useState(initial)
  const [state, setState] = useState('loading')
  const version = useRef(0)
  useEffect(() => () => { version.current += 1 }, [])
  const load = () => {
    const owner = ++version.current
    // Poll, visibility and post-mutation reads can overlap. Only the newest request
    // owns the resource; cleanup invalidates every late settlement after this component unmounts.
    const timed = deadlineRequest(signal => read(signal), LIST_READ_TIMEOUT_MS)
    return timed.promise
      .then(value => {
        if (version.current !== owner) return { ok: false, superseded: true }
        setData(value); setState('ready')
        return { ok: true, value }
      })
      .catch(error => {
        if (version.current !== owner) return { ok: false, superseded: true, error }
        setState(current => ['ready', 'stale'].includes(current) ? 'stale' : 'error')
        return { ok: false, error }
      })
  }
  return [data, state, load]
}

function ResourceNotice({ state, label, retry }) {
  if (state === 'ready') return null
  if (state === 'loading') return <div className="notice" role="status">{label} loading…</div>
  const stale = state === 'stale'
  return <div className={'notice ' + (stale ? 'resource-warning' : 'resource-error')}
    role={stale ? 'status' : 'alert'}>
    {label}: {stale ? 'Last loaded data; refresh failed.' : 'Unavailable.'} <button className="btn sm" onClick={retry}>Retry</button>
  </div>
}

const mutationMessage = (error, timedOut = false) => timedOut
  ? 'Save timed out; its outcome is unknown.'
  : error?.viewName
    ? 'Use a view name of 1–48 characters without control characters.'
  : error?.storage
    ? 'Browser storage is blocked or full; this view was not saved.'
  : error?.code === 'run_generation_changed' || error?.code === 'run_generation_unavailable'
      || error?.code === 'invalid_run_generation' || error?.code === 'run_organization_changed'
      || error?.code === 'run_organization_unavailable'
      || error?.code === 'invalid_expected_organization'
    ? [error.message, error.remediation].filter(Boolean).join(' ')
  : error?.status === 409
    ? 'Conflict; current input or selection kept.'
    : error?.status === 503
      ? 'Unavailable; current input or selection kept.'
      : 'Save failed; current input or selection kept.'

// Resource reads intentionally resolve an explicit result so callers can keep their last good data.
// A resolved reconciliation is therefore authoritative only when none of those results reports that
// it failed or was superseded by a newer owner.
const reconciliationStatus = settlement => {
  if (settlement?.timeout) return 'timeout'
  if (!settlement?.ok) return 'failed'
  const results = Array.isArray(settlement.value) ? settlement.value : [settlement.value]
  const failures = results.filter(result => result === false
    || (result && typeof result === 'object' && result.ok === false))
  if (!failures.length) return 'ok'
  return failures.every(result => result?.superseded === true) ? 'superseded' : 'failed'
}

const mutationReconcileSuffix = status => status === 'ok'
  ? ' Current data was refreshed; verify the current value before retrying.'
  : status === 'timeout'
    ? ' The follow-up refresh timed out; reload before retrying.'
    : status === 'superseded'
      ? ' The follow-up refresh was superseded before it could be verified; reload before retrying.'
      : ' The follow-up refresh failed; reload before retrying.'

// Prompt-local input and menu selection survive a failed mutation until the operator retries.

function useMutation() {
  const lock = useRef(false)
  const [state, setState] = useState(null)
  const run = async (action, reconcile) => {
    if (lock.current) return false
    lock.current = true; setState(true)
    try {
      // The transport is intentionally not replayed: after the local deadline its write outcome is
      // ambiguous. Releasing the page-wide interaction lock with explicit unknown-outcome copy is
      // safer than freezing every navigation surface until a hung proxy eventually settles.
      const settlement = await settleWithin(action, LIST_WRITE_TIMEOUT_MS)
      if (!settlement.ok) {
        let message = mutationMessage(settlement.error, settlement.timeout)
        if (settlement.error?.storage || settlement.error?.viewName) {
          setState(message); return false
        }
        if (typeof reconcile === 'function') {
          const check = await settleWithin(reconcile, LIST_RECONCILE_TIMEOUT_MS)
          message += mutationReconcileSuffix(reconciliationStatus(check))
        } else {
          message += ' Reload this view before retrying.'
        }
        setState(message); return false
      }
      const outcome = settlement.value
      // A caller may own a stronger reconciliation contract (for example useListMutation below).
      // Keep its explicit unknown/failure result authoritative instead of closing the menu as success.
      if (outcome === false) { setState(null); return false }
      setState(null); return true
    }
    catch (error) {
      let message = mutationMessage(error)
      if (error?.storage || error?.viewName) {
        setState(message); return false
      }
      if (typeof reconcile === 'function') {
        const check = await settleWithin(reconcile, LIST_RECONCILE_TIMEOUT_MS)
        message += mutationReconcileSuffix(reconciliationStatus(check))
      } else {
        message += ' Reload this view before retrying.'
      }
      setState(message); return false
    }
    finally { lock.current = false }
  }
  return [state === true, typeof state === 'string' ? state : '', run, setState]
}

const focusSoon = target => requestAnimationFrame(() => target?.isConnected && target.focus({ preventScroll: true }))

const listMutationMessage = (kind, error) => {
  if (error?.code === 'run_generation_changed' || error?.code === 'run_generation_unavailable'
      || error?.code === 'invalid_run_generation' || error?.code === 'run_organization_changed'
      || error?.code === 'run_organization_unavailable'
      || error?.code === 'invalid_expected_organization') {
    return [error.message, error.remediation].filter(Boolean).join(' ')
  }
  if (kind === 'delete-run') return error?.status === 409
    ? 'This run is still live. Pause or stop it before deleting.'
    : 'Run deletion was not confirmed.'
  if (kind === 'delete-project') return error?.status === 409
    ? 'This project changed elsewhere and was not deleted.'
    : 'Project deletion was not confirmed.'
  return error?.status === 409
    ? 'This assignment changed elsewhere and the move was not applied.'
    : 'The move was not confirmed.'
}

const listReconcileSuffix = status => status === 'ok'
  ? ' Current list was refreshed; verify it before retrying.'
  : status === 'timeout'
    ? ' The follow-up list check timed out; reload before retrying.'
    : status === 'superseded'
      ? ' The follow-up list check was superseded before it could be verified; reload before retrying.'
      : ' The follow-up list check failed; reload before retrying.'

const acknowledgedListMutationMessage = (kind, status) => {
  const acknowledged = kind === 'delete-run'
    ? 'Run deletion was confirmed'
    : kind === 'delete-project'
      ? 'Project deletion was confirmed'
      : 'The move was confirmed'
  const refresh = status === 'timeout'
    ? 'the current-list refresh timed out.'
    : status === 'superseded'
      ? 'the follow-up refresh was superseded before it could be verified.'
      : 'the current list could not be refreshed.'
  return `${acknowledged}, but ${refresh} Reload before relying on the visible list.`
}

const LIST_WRITE_TIMEOUT_MS = 12_000
const LIST_RECONCILE_TIMEOUT_MS = 8_000

// Resolve exactly once even when unabortable transport settles after the local deadline. Attaching
// both handlers up front also prevents a late rejection from becoming an unhandled promise.
const settleWithin = (work, timeout) => new Promise(resolve => {
  let settled = false
  const finish = result => {
    if (settled) return
    settled = true; clearTimeout(timer); resolve(result)
  }
  const timer = setTimeout(() => finish({ timeout: true }), timeout)
  Promise.resolve().then(work).then(
    value => finish({ ok: true, value }),
    error => finish({ error }),
  )
})

// Destructive list actions and drag/drop share one lock. A failed transport can have an ambiguous
// outcome, so this guard never replays a write; callers reconcile with a read before the operator can
// explicitly try again.
export function useListMutation({ actionTimeout = LIST_WRITE_TIMEOUT_MS, reconcileTimeout = LIST_RECONCILE_TIMEOUT_MS } = {}) {
  const lock = useRef(false)
  const version = useRef(0)
  const [state, setState] = useState(null)
  useEffect(() => () => { version.current += 1 }, [])
  const run = async (kind, label, action, reconcile) => {
    if (lock.current) return false
    const token = ++version.current
    const update = value => { if (version.current === token) setState(value) }
    lock.current = true; update({ busy: true, label })
    try {
      const outcome = await settleWithin(action, actionTimeout)
      let check = null
      if (typeof reconcile === 'function') {
        update({ busy: true, label: outcome.ok
          ? 'Refreshing current list…'
          : 'Checking the current list before retry…' })
        check = await settleWithin(reconcile, reconcileTimeout)
      }
      const checkStatus = check ? reconciliationStatus(check) : null
      if (outcome.ok) {
        if (check && checkStatus !== 'ok') {
          update({ busy: false, error: acknowledgedListMutationMessage(kind, checkStatus) })
        } else {
          update(null)
        }
        return true
      }
      let message = listMutationMessage(kind, outcome.error)
      message += check ? listReconcileSuffix(checkStatus) : ' Reload before retrying.'
      update({ busy: false, error: message }); return false
    }
    finally { lock.current = false }
  }
  const clear = () => { version.current += 1; setState(null) }
  return [state, run, clear]
}

// Module-scope so its identity is stable across re-renders (the runs list polls every 2.5s). Defined
// inside RunList it got a fresh identity each render, so React remounted the whole project subtree and
// the uncontrolled inline-rename <input> lost its text/focus mid-edit. All render state is threaded
// through `ctx`.
export function TreeNode({ p, depth, ctx }) {
  const { byParent, expanded, sel, setSel, onDrop, toggle, renaming, finishProjectRename, startProjectRename,
          projectBusy, projectError, count, addProject, removeProject } = ctx
  const kids = byParent[p.id] || []
  const open = expanded.has(p.id)
  const commitRename = async (input, value, restoreFocus) => {
    if (projectBusy || input.dataset.pending) return
    input.dataset.pending = 'true'
    const finished = await finishProjectRename(p.id, value, restoreFocus)
    if (!finished && input.isConnected) delete input.dataset.pending
  }
  return <div className="ptree-node">
    <div className={'ptree-row' + (sel === p.id ? ' sel' : '')} style={{ paddingLeft: 6 + depth * 14 }}
         aria-busy={renaming === p.id && projectBusy}
         onDragOver={e => { if (!projectBusy) e.preventDefault() }} onDrop={() => { if (!projectBusy) onDrop(p.id) }}>
      {kids.length
        ? <button type="button" className="ptw" disabled={projectBusy} aria-label={`${open ? 'Collapse' : 'Expand'} ${p.name}`}
            aria-expanded={open} onClick={() => toggle(p.id)}>{open ? '▾' : '▸'}</button>
        : <span className="ptw" aria-hidden="true">·</span>}
      {renaming === p.id
        ? <input className="text ptree-rename" autoFocus readOnly={projectBusy} defaultValue={p.name}
                 aria-label={`Rename project ${p.name}`}
                 onBlur={e => { if (!e.currentTarget.dataset.pending) commitRename(e.currentTarget, e.currentTarget.value, false) }}
                 onKeyDown={e => {
                   if (e.key === 'Enter') {
                     e.preventDefault(); commitRename(e.currentTarget, e.currentTarget.value, true)
                   }
                   if (e.key === 'Escape') {
                     // Cancel: a blank name skips the PATCH (the guard in finishProjectRename), so
                     // Escape reverts to the current name without a redundant server write + reload.
                     e.preventDefault(); commitRename(e.currentTarget, '', true)
                   }
                 }} />
        : <button type="button" className="pname project-choice" disabled={projectBusy} aria-pressed={sel === p.id}
            onClick={() => setSel(p.id)}><OpIcon name="folder" className="t-ic" /> {p.name}</button>}
      <span className="pcount">{count(p.id)}</span>
      <span className="pacts">
        <button className="ic" disabled={projectBusy} aria-label={`Add sub-project inside ${p.name}`}
          onClick={event => addProject(p.id, event.currentTarget)}>＋</button>
        <button className="ic" disabled={projectBusy} aria-label={`Rename project ${p.name}`} onClick={event => startProjectRename(p.id, event.currentTarget)}><OpIcon name="pencil" size={12} /></button>
        <button className="ic" disabled={projectBusy} aria-label={`Delete project ${p.name}`} onClick={event => removeProject(p.id, event.currentTarget)}>✕</button>
      </span>
    </div>
    {renaming === p.id && projectBusy && <div className="muted" role="status">Saving project name…</div>}
    {renaming === p.id && projectError && <div className="flag" role="alert">{projectError}</div>}
    {open && <div className="ptree-children">{kids.map(k => <TreeNode key={k.id} p={k} depth={depth + 1} ctx={ctx} />)}</div>}
  </div>
}

// Small centered popup (replaces window.prompt for project create / run rename).
function Modal({ title, onClose, children, busy = false }) {
  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, busy ? null : onClose)
  return <div className="overlay" onMouseDown={event => { if (!busy && event.target === event.currentTarget) onClose?.() }}>
    <div ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-label={title} aria-busy={busy} tabIndex={-1}>
      <div className="modal-h"><b>{title}</b><span style={{ flex: 1 }} />
        <button className="btn sm ghost" disabled={busy} onClick={onClose} aria-label={`Close ${title}`}>✕</button></div>
      <div className="modal-b">{children}</div>
    </div>
  </div>
}

function PromptModal({ title, label, placeholder, initial = '', confirm = 'Create', allowEmpty = false,
  maxLength, blocked = false, blockedMessage = '',
  onSubmit, onReconcile, onClose }) {
  const [v, setV] = useState(initial)
  const [busy, error, mutate] = useMutation()
  const ok = !blocked && (allowEmpty || !!v.trim())
  const go = async () => {
    if (ok && await mutate(() => onSubmit(v.trim()), onReconcile)) onClose()
  }
  return <Modal title={title} onClose={onClose} busy={busy}>
    {label && <div className="muted" style={{ marginBottom: 8 }}>{label}</div>}
    <input className="text" autoFocus readOnly={busy || blocked} aria-label={label || title}
           placeholder={placeholder} maxLength={maxLength} value={v} onChange={e => setV(e.target.value)}
           onKeyDown={e => { if (e.key === 'Enter') go(); if (e.key === 'Escape' && !busy) onClose() }} />
    {blocked && blockedMessage && <div className="flag" role="alert">{blockedMessage}</div>}
    {error && <div className="flag" role="alert">{error}</div>}
    <div className="modal-actions">
      <button className="btn sm ghost" disabled={busy} onClick={onClose}>Cancel</button>
      <button className="btn sm primary" disabled={!ok || busy} onClick={go}>{busy ? 'Saving…' : confirm}</button>
    </div>
  </Modal>
}

const RUN_DELETION_STATUSES = new Set(['pending', 'succeeded'])
const RUN_DELETION_PHASES = new Set([
  'prepared', 'fenced', 'quarantining', 'quarantine_ambiguous', 'quarantined', 'metadata_committed',
  'purging', 'retiring_fence', 'succeeded',
])
const deletionGenerationOf = run => String(run?.deletion_generation || run?.generation || '')

const exactRunDeletionReceipt = (value, intent) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || typeof value.ok !== 'boolean'
      || !RUN_DELETION_STATUSES.has(value.status)
      || !RUN_DELETION_PHASES.has(value.phase)
      || value.operation_id !== intent.operationId
      || value.run_id !== intent.runId
      || value.expected_generation !== intent.expectedGeneration
      || value.expected_seq !== intent.expectedSeq) return null
  if (value.status === 'pending' && (value.ok !== false || value.phase === 'succeeded')) return null
  if (value.status === 'succeeded' && (value.ok !== true || value.phase !== 'succeeded')) return null
  return {
    ok: value.ok,
    status: value.status,
    phase: value.phase,
    operationId: value.operation_id,
    expectedGeneration: value.expected_generation,
    expectedSeq: value.expected_seq,
  }
}

const runDeletionErrorMessage = error => {
  const code = String(error?.code || '')
  if (['engine_running', 'engine_launching', 'run_command_active'].includes(code)) {
    return 'This run is still active. Pause or finish it, then reopen Delete from the refreshed card.'
  }
  if (['run_generation_changed', 'run_seq_changed'].includes(code)) {
    return 'This run changed after the menu was opened. The inspected generation was not deleted.'
  }
  if (code === 'run_reset_in_progress') {
    return 'Finish the existing Start over recovery before deleting this run.'
  }
  if (code === 'run_finalization_incomplete') {
    return 'This run is still finishing its terminal records. Refresh it before deleting.'
  }
  if (code === 'delete_cost_pending') {
    return 'Run cost records are still being finalized. Wait for them to settle, then reopen Delete.'
  }
  if (code === 'delete_operation_conflict') {
    return 'Another deletion already owns this run. This new request was not submitted.'
  }
  if (error?.status === 401 || error?.status === 403) {
    return 'Owner access is required. Restore access before checking or deleting this run.'
  }
  if (error?.status === 404 || error?.status === 410) {
    return 'The inspected run is no longer available. Refresh the list before deciding what to do next.'
  }
  if (error?.status === 422 || error?.status === 428) {
    return 'The server rejected this deletion identity. Refresh the run before trying again.'
  }
  return 'The deletion request was rejected. The run remains unchanged; refresh before trying again.'
}

const runDeletionProgress = recovery => {
  if (recovery.kind === 'corrupt') {
    return 'Saved deletion recovery is invalid. This run stays locked because the exact earlier operation cannot be identified safely.'
  }
  const intent = recovery.intent
  if (intent.phase === 'unknown') {
    return 'Deletion outcome is not confirmed. Retry only this exact saved operation before changing the run.'
  }
  if (intent.phase === 'submitting') {
    return 'The exact deletion request is being submitted. No result is assumed yet.'
  }
  const byServerPhase = {
    prepared: 'The server preserved this deletion request and is preparing the run.',
    fenced: 'The run is locked against new changes while deletion continues.',
    quarantining: 'The run is being removed from the live workspace.',
    quarantine_ambiguous: 'The exact run is isolated, but storage could not confirm the move. Manual repair is required before recovery can continue.',
    quarantined: 'The run left the live workspace; metadata cleanup is continuing.',
    metadata_committed: 'Run metadata is removed; final file cleanup is continuing.',
    purging: 'The server is finishing permanent file cleanup.',
    retiring_fence: 'Run files are gone; the server is retiring the final deletion lock.',
    succeeded: 'Deletion is confirmed; refreshing the run list before releasing recovery.',
  }
  return byServerPhase[intent.serverPhase]
    || 'The server preserved this exact deletion request. Checking its current phase automatically.'
}

function RunDeleteDialog({ target, currentRun, busy, error, onClose, onConfirm }) {
  const dialogRef = useRef(null)
  const errorRef = useRef(null)
  useDialogFocus(dialogRef, busy ? null : onClose)
  useEffect(() => {
    if (error) errorRef.current?.focus({ preventScroll: true })
  }, [error])
  const targetValid = RUN_GENERATION_RE.test(target.expectedGeneration || '')
    && Number.isSafeInteger(target.expectedSeq) && target.expectedSeq >= -1
  const identityChanged = !currentRun
    || deletionGenerationOf(currentRun) !== target.expectedGeneration
    || currentRun.seq !== target.expectedSeq
  const blocked = busy || target.invalidated === true || !targetValid || identityChanged
  return <div className="overlay run-delete-overlay"
    onMouseDown={event => { if (!busy && event.target === event.currentTarget) onClose() }}>
    <section ref={dialogRef} className="modal run-delete-dialog" role="alertdialog"
      aria-modal="true" aria-labelledby="run-delete-title"
      aria-describedby="run-delete-description run-delete-warning"
      aria-busy={busy} tabIndex={-1}>
      <div className="modal-h"><b id="run-delete-title">Delete this run permanently?</b></div>
      <div className="modal-b">
        <p id="run-delete-description" className="run-delete-copy">
          This removes the run-owned events, experiments, traces, chat, reports, and review access.
        </p>
        <dl className="run-delete-identity">
          <div><dt>Label</dt><dd>{target.label || '(no display label)'}</dd></div>
          <div><dt>Run ID</dt><dd><code>{target.runId}</code></dd></div>
          <div><dt>Deletion identity</dt><dd><code>{target.expectedGeneration || 'unavailable'}</code></dd></div>
          <div><dt>Sequence</dt><dd><code>{Number.isSafeInteger(target.expectedSeq) ? target.expectedSeq : 'unavailable'}</code></dd></div>
        </dl>
        <p id="run-delete-warning" className="run-delete-warning">This cannot be undone.</p>
        {(!targetValid || identityChanged) && <div className="flag run-delete-error" role="alert">
          {!targetValid
            ? 'The exact generation or sequence is unavailable. Refresh the list before deleting.'
            : 'This run changed while the dialog was open. Cancel and reopen Delete from the refreshed card.'}
        </div>}
        {error && <div ref={errorRef} className="flag run-delete-error" role="alert" tabIndex={-1}>{error}</div>}
        <div className="modal-actions">
          <button type="button" className="btn sm" data-dialog-initial-focus disabled={busy}
            onClick={onClose}>Cancel</button>
          <button type="button" className="btn sm danger" disabled={blocked}
            onClick={onConfirm}>{busy ? 'Submitting exact request…' : 'Delete permanently'}</button>
        </div>
      </div>
    </section>
  </div>
}

function RunDeletionCard({ run, recovery, busy, onRetry, setRef }) {
  const active = recovery.kind === 'active'
  const urgent = !active || recovery.intent.phase === 'unknown' || recovery.storageUnavailable
  return <div ref={setRef} className={`run-card run-deletion-card${urgent ? ' attention' : ''}`}
    data-run-id={recovery.runId} tabIndex={-1}
    role={urgent ? 'alert' : 'status'} aria-live={urgent ? 'assertive' : 'polite'}
    aria-atomic="true" aria-busy={busy}>
    <OpIcon name="alert" size={16} />
    <div className="run-deletion-copy">
      <div><b>{run?.label || recovery.runId}</b> <span className="pill warn">Deletion recovery</span></div>
      <div>{runDeletionProgress(recovery)}</div>
      {active && <div className="muted"><code>{recovery.runId}</code> · generation <code>{recovery.intent.expectedGeneration.slice(0, 12)}…</code></div>}
      {recovery.storageUnavailable && <div className="flag">This tab could not update its saved recovery record.</div>}
    </div>
    {active && <button type="button" className="btn sm" disabled={busy}
      onClick={() => onRetry(recovery)}>{busy ? 'Checking…'
        : recovery.intent.phase === 'unknown' ? 'Retry exact deletion' : 'Check exact deletion'}</button>}
  </div>
}

// Per-run "⋮" dropdown: open / rename / move (project) / assign (super-task) / delete.
function RunMenu({ r, projects, supertasks, onOpen, onMove, onSetSuper, onManageSupers, onRename,
  onDelete, onReconcile, onClose, onBusyChange, mutationLocked = false,
  mutationLockReason = 'Resolve the existing run operation before changing this run', anchor = null,
  deleteLocked = mutationLocked, deleteLockReason = mutationLockReason }) {
  const menuRef = useRef(null)
  const [menuStyle, setMenuStyle] = useState(null)
  const [busy, error, mutate] = useMutation()
  useLayoutEffect(() => {
    const positionMenu = () => {
      const menu = menuRef.current
      if (!menu || !anchor?.isConnected) return
      if (window.matchMedia('(max-width: 900px)').matches) {
        setMenuStyle(null)
        return
      }
      const trigger = anchor.getBoundingClientRect()
      const shell = document.querySelector('.app-shell-main')?.getBoundingClientRect()
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth
      const margin = 8
      const topLimit = Math.max(margin, (shell?.top || 0) + margin)
      const bottomLimit = Math.min(window.innerHeight, shell?.bottom || window.innerHeight) - margin
      const width = Math.min(menu.offsetWidth, viewportWidth - margin * 2)
      const maxHeight = Math.max(80, bottomLimit - topLimit)
      const height = Math.min(menu.scrollHeight, maxHeight)
      const top = Math.max(topLimit, Math.min(trigger.bottom + 4, bottomLimit - height))
      const left = Math.max(margin, Math.min(
        trigger.right - width, viewportWidth - margin - width))
      setMenuStyle({ position: 'fixed', top, left, right: 'auto', maxHeight,
        overflowY: 'auto' })
    }
    positionMenu()
    window.addEventListener('resize', positionMenu)
    window.addEventListener('scroll', positionMenu, true)
    return () => {
      window.removeEventListener('resize', positionMenu)
      window.removeEventListener('scroll', positionMenu, true)
    }
  }, [anchor, projects.length, supertasks.length, mutationLocked, deleteLocked, busy, error])
  useEffect(() => {
    menuRef.current?.querySelector('[role="menuitem"]:not(:disabled)')?.focus()
  }, [])
  useEffect(() => {
    onBusyChange?.(busy)
    return () => onBusyChange?.(false)
  }, [busy, onBusyChange])
  const close = (restore = false) => { if (!busy) onClose(restore) }
  const act = async action => { if (await mutate(action, onReconcile)) onClose(true) }
  const onKeyDown = event => {
    if (busy && event.key === 'Tab') { event.preventDefault(); return }
    const items = [...(menuRef.current?.querySelectorAll(
      '[role="menuitem"]:not(:disabled)') || [])]
    if (event.key === 'Escape') { event.preventDefault(); close(true); return }
    const current = Math.max(0, items.indexOf(document.activeElement))
    const next = nextRovingIndex(event.key, current, items.length)
    if (next == null) return
    event.preventDefault(); items[next]?.focus()
  }
  return <>
    <div className="menu-backdrop" onClick={() => close(true)} onDragStart={() => close(true)} />
    <div ref={menuRef} className="run-menu" role="menu" aria-label={`Actions for ${r.label || r.run_id}`}
      style={menuStyle || undefined}
      aria-busy={busy} aria-disabled={busy}
      onClick={e => e.stopPropagation()} onClickCapture={e => { if (busy) { e.preventDefault(); e.stopPropagation() } }} onKeyDown={onKeyDown}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(false) }}>
      <button type="button" role="menuitem" tabIndex={-1} className="mi" onClick={() => { close(false); onOpen(r.run_id) }}>↗ Open</button>
      {mutationLocked && <div className="mi-label" role="status">
        {mutationLockReason}
      </div>}
      <button type="button" role="menuitem" tabIndex={-1} className="mi"
        disabled={mutationLocked} title={mutationLocked ? mutationLockReason : undefined}
        onClick={() => { close(false); onRename(r) }}><OpIcon name="pencil" size={12} /> Rename</button>
      <div className="mi-sep" />
      <div className="mi-label">Move to project</div>
      <div className="mi-scroll">
        <button type="button" role="menuitem" tabIndex={-1} disabled={mutationLocked}
          className={'mi' + (!r.project_id ? ' on' : '')} onClick={() => act(() => onMove(r, UNASSIGNED))}>○ — unassigned —</button>
        {projects.map(p => <button type="button" role="menuitem" tabIndex={-1} key={p.id} className={'mi' + (r.project_id === p.id ? ' on' : '')}
          disabled={mutationLocked}
          onClick={() => act(() => onMove(r, p.id))}><OpIcon name="folder" className="t-ic" /> {p.name}</button>)}
        {!projects.length && <div className="mi-empty">no projects yet</div>}
      </div>
      <div className="mi-sep" />
      <div className="mi-label">Super-task</div>
      <div className="mi-scroll">
        <button type="button" role="menuitem" tabIndex={-1} disabled={mutationLocked}
          className={'mi' + (!r.supertask_id ? ' on' : '')} onClick={() => act(() => onSetSuper(r, UNASSIGNED))}>○ — none —</button>
        {supertasks.map(s => <button type="button" role="menuitem" tabIndex={-1} key={s.id} className={'mi' + (r.supertask_id === s.id ? ' on' : '')}
          disabled={mutationLocked}
          onClick={() => act(() => onSetSuper(r, s.id))}><OpIcon name="target" className="t-ic" /> {s.name}</button>)}
        <button type="button" role="menuitem" tabIndex={-1} className="mi accent" onClick={() => { close(false); onManageSupers() }}>＋ New / manage…</button>
      </div>
      {busy && <div className="muted" role="status">Saving…</div>}
      {error && <div className="flag" role="alert">{error}</div>}
      <div className="mi-sep" />
      {deleteLocked && (!mutationLocked || deleteLockReason !== mutationLockReason)
        && <div className="mi-label" role="status">{deleteLockReason}</div>}
      <button type="button" role="menuitem" tabIndex={-1} className="mi danger"
        disabled={deleteLocked} title={deleteLocked ? deleteLockReason : undefined}
        onClick={() => onDelete(r)}>✕ Delete run…</button>
    </div>
  </>
}

// Manage super-tasks in one popup: create, rename (inline), delete. Assignment happens per-run via
// the ⋮ menu / drag; this is just the CRUD over the buckets themselves.
function SuperTaskModal({ supertasks, state, onRetry, onCreate, onRename, onDelete,
  onReconcile, onClose }) {
  const [name, setName] = useState('')
  const newTaskRef = useRef(null)
  const [busy, error, mutate] = useMutation()
  const add = async () => {
    const v = name.trim()
    if (v && await mutate(() => onCreate(v), onReconcile)) setName('')
  }
  const edit = (task, input) => {
    const v = input.value.trim()
    if (v && v !== task.name) mutate(() => onRename(task.id, v), onReconcile)
  }
  const remove = async (task, event) => {
    const row = event.currentTarget.closest('.st-row')
    const fallback = row?.nextElementSibling?.querySelector('.st-rename')
      || row?.previousElementSibling?.querySelector('.st-rename') || newTaskRef.current
    let removed
    const saved = await mutate(async () => { removed = await onDelete(task) }, onReconcile)
    if (saved && removed) requestAnimationFrame(() => fallback?.isConnected
      ? fallback.focus({ preventScroll: true }) : newTaskRef.current?.focus({ preventScroll: true }))
  }
  return <Modal title="Super-tasks" onClose={onClose} busy={busy}>
    <div className="muted" style={{ marginBottom: 8 }}>A super-task groups runs that attack the same global task (across many runs). Assign runs from a run’s ⋮ menu.</div>
    <ResourceNotice state={state} label="Super-tasks" retry={onRetry} />
    {busy && <div className="muted" role="status">Saving super-task changes…</div>}
    {error && <div className="flag" role="alert">{error}</div>}
    <div className="st-new">
      <input ref={newTaskRef} className="text" autoFocus readOnly={busy} aria-label="New super-task name" placeholder="New super-task name (e.g. nomad2018)" value={name}
             onChange={e => setName(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') add() }} />
      <button className="btn sm primary" disabled={busy || !name.trim()} onClick={add}>＋ Create</button>
    </div>
    <div className="st-list">
      {supertasks.map(s => <div key={s.id} className="st-row">
        <span className="st-ic"><OpIcon name="target" className="t-ic" /></span>
        <input className="text st-rename" readOnly={busy} defaultValue={s.name} aria-label={`Rename super-task ${s.name}`}
               onBlur={e => edit(s, e.currentTarget)}
               onKeyDown={e => {
                 if (e.key === 'Enter') {
                   e.preventDefault(); edit(s, e.currentTarget)
                 }
               }} />
        <button className="ic" disabled={busy} aria-label={`Delete super-task ${s.name}`} onClick={event => remove(s, event)}>✕</button>
      </div>)}
      {state === 'ready' && !supertasks.length && <div className="muted" style={{ padding: '8px 2px', fontSize: 12 }}>No super-tasks yet.</div>}
    </div>
  </Modal>
}

export default function RunList({ onOpen, onSettings, onResearchAtlas }) {
  const compactNav = useMediaQuery('(max-width: 900px)')
  const [runs, runsState, loadRuns] = useResource(
    signal => get('/api/runs', { signal, cache: 'no-store' }), null)
  const [proj, projectsState, loadProjects] = useResource(
    signal => listProjects({ signal }), { projects: [], assignments: {} })
  const [sel, setSel] = useState(ALL)
  const [expanded, setExpanded] = useState(() => new Set())
  const [renaming, setRenaming] = useState(null)   // project id being renamed (inline)
  const [projectBusy, projectError, saveProjectRename, clearProjectError] = useMutation()
  const [listMutation, mutateList, clearListMutation] = useListMutation()
  const [menuBusy, setMenuBusy] = useState(false)
  const projectRenameReturnRef = useRef(null)
  const runsMainRef = useRef(null)
  const projectsAllRef = useRef(null)
  const [dragRun, setDragRun] = useState(null)
  const compareDragGuardRef = useRef(null)
  const [view, setView] = useState('list')
  const [mapCollapseOverrides, setMapCollapseOverrides] = useState(() => new Map())
  const [projModal, setProjModal] = useState(null) // {parent_id} → show create-project popup
  const projectModalReturnRef = useRef(null)
  const [runMenu, setRunMenu] = useState(null)     // exact run-list snapshot whose ⋮ menu is open
  const runMenuTriggerRef = useRef(null)
  const runModalReturnFocusRef = useRef(null)
  const [runRename, setRunRename] = useState(null) // run object being renamed (popup)
  const [deleteDialog, setDeleteDialog] = useState(null)
  const [deleteDialogBusy, setDeleteDialogBusy] = useState(false)
  const [deleteDialogError, setDeleteDialogError] = useState('')
  const deleteDialogFocusRef = useRef(null)
  const deleteConfirmLockRef = useRef(false)
  const deletionFallbacksRef = useRef(new Map())
  const deletionRequestRef = useRef(new Map())
  const deletionRecoveryRefs = useRef(new Map())
  const deletionMountedRef = useRef(true)
  const [deletionBusy, setDeletionBusy] = useState(() => new Set())
  const [deletionNotice, setDeletionNotice] = useState(null)
  const [deletionRecoveries, setDeletionRecoveries] = useState(() => new Map(
    listRunDeletionRecoveries().map(item => [item.runId, item])))
  // Sort + filter of the run list (client-side over the loaded summaries).
  const [sortKey, setSortKey] = useState('time')
  const [sortDir, setSortDir] = useState('desc')
  const [query, setQuery] = useState('')
  const [taskFilter, setTaskFilter] = useState(ALL)
  const [statusFilter, setStatusFilter] = useState('all')
  const [stFilter, setStFilter] = useState(ALL)
  const [superdata, superState, loadSupers] = useResource(
    signal => listSupertasks({ signal }), { supertasks: [], assignments: {} })
  const [stModal, setStModal] = useState(false)             // manage-super-tasks popup open?
  const [showReport, setShowReport] = useState(false)       // cross-run scope-report panel open?
  const [compareIds, setCompareIds] = useState(() => new Set())
  const [compareColumns, setCompareColumns] = useState(
    () => normalizeCompareColumns(storageGet(COMPARE_COLUMNS_KEY)))
  const [savedViews, setSavedViews] = useState(
    () => decodePortfolioViews(storageGet(SAVED_VIEWS_KEY, '[]')))
  const [activeSavedView, setActiveSavedView] = useState('')
  const [viewModal, setViewModal] = useState(false)
  const [viewMessage, setViewMessage] = useState('')
  const [listLimit, setListLimit] = useState(LIST_PAGE_SIZE)
  const savedViewReturnRef = useRef(null)
  const [projectsOpen, setProjectsOpen] = useState(false)   // compact-screen Projects drawer
  const projectsToggleRef = useRef(null)
  const projectsCloseRef = useRef(null)
  const projectsDialogRef = useRef(null)
  const listBusy = !!listMutation?.busy
  const navigationBusy = listBusy || projectBusy || menuBusy
  const startOverRecoveryByRun = new Map([
    ...listStartOverRunAccesses(), ...listRunStartOverRecoveries(),
  ].map(item => [item.runId, item]))
  const runListAuthoritative = runsState === 'ready' || runsState === 'stale'
  const missingStartOverRecoveries = [...startOverRecoveryByRun.values()]
    .filter(item => !runListAuthoritative
      || !(runs || []).some(run => run.run_id === item.runId))
  const deleteDialogCurrentRun = deleteDialog
    ? (runs || []).find(run => run.run_id === deleteDialog.runId) || null
    : null
  const runRenameCurrentRun = runRename
    ? (runs || []).find(run => run.run_id === runRename.run_id) || null
    : null
  const runRenameBlocked = !!runRename && (!runRenameCurrentRun
    || runRenameCurrentRun.generation !== runRename.generation)
  const closeRunMenu = (restore = false) => {
    setRunMenu(null)
    if (restore) focusSoon(runMenuTriggerRef.current)
  }
  const restoreRunModalFocus = () => requestAnimationFrame(() => {
    const target = runModalReturnFocusRef.current
    if (target?.isConnected) target.focus({ preventScroll: true })
    runModalReturnFocusRef.current = null
  })
  const openRunRename = run => {
    runModalReturnFocusRef.current = runMenuTriggerRef.current
    setRunRename(run)
  }
  const openSuperTasks = returnFocus => {
    runModalReturnFocusRef.current = returnFocus || runMenuTriggerRef.current
    setStModal(true)
  }
  const closeRunRename = () => { setRunRename(null); restoreRunModalFocus() }
  const closeSuperTasks = () => { setStModal(false); restoreRunModalFocus() }
  useEffect(() => {
    deletionMountedRef.current = true
    return () => {
      deletionMountedRef.current = false
      for (const request of deletionRequestRef.current.values()) request.controller.abort()
      deletionRequestRef.current.clear()
    }
  }, [])
  const updateDeletionRecovery = recovery => {
    if (!deletionMountedRef.current) return
    setDeletionRecoveries(current => {
      const next = new Map(current)
      next.set(recovery.runId, recovery)
      return next
    })
  }
  const removeDeletionRecovery = runId => {
    if (!deletionMountedRef.current) return
    setDeletionRecoveries(current => {
      if (!current.has(runId)) return current
      const next = new Map(current); next.delete(runId); return next
    })
  }
  const setDeletionBusyFor = (runId, busy) => {
    if (!deletionMountedRef.current) return
    setDeletionBusy(current => {
      const next = new Set(current)
      busy ? next.add(runId) : next.delete(runId)
      return next
    })
  }
  const persistDeletionPhase = (intent, phase, serverPhase = intent.serverPhase) => {
    const next = createRunDeletionIntent(
      intent.runId, intent.expectedGeneration, intent.expectedSeq, intent.operationId,
      phase, serverPhase)
    if (!next) return null
    const saved = saveRunDeletionIntent(next, undefined, intent.operationId)
    if (!saved) {
      const restored = loadRunDeletionIntent(intent.runId)
      if (restored.kind === 'active' || restored.kind === 'corrupt') {
        updateDeletionRecovery({ runId: intent.runId, ...restored, storageUnavailable: true })
        return restored.kind === 'active' ? restored.intent : null
      }
    }
    updateDeletionRecovery({ runId: intent.runId, kind: 'active', intent: next,
      storageUnavailable: !saved })
    return next
  }
  const clearDeletionRecovery = intent => {
    if (clearRunDeletionIntent(intent.runId, undefined, intent.operationId)) {
      removeDeletionRecovery(intent.runId)
      return true
    }
    const restored = loadRunDeletionIntent(intent.runId)
    updateDeletionRecovery(restored.kind === 'active' || restored.kind === 'corrupt'
      ? { runId: intent.runId, ...restored, storageUnavailable: true }
      : { runId: intent.runId, kind: 'active', intent, storageUnavailable: true })
    return false
  }
  const focusDeletionRecovery = runId => requestAnimationFrame(() => {
    ;(deletionRecoveryRefs.current.get(runId) || runsMainRef.current)
      ?.focus?.({ preventScroll: true })
  })
  const focusAfterDeletion = (intent) => requestAnimationFrame(() => {
    if (!deletionMountedRef.current) return
    const fallback = deletionFallbacksRef.current.get(intent.operationId)
    deletionFallbacksRef.current.delete(intent.operationId)
    const currentCard = [...(runsMainRef.current?.querySelectorAll('.run-card') || [])]
      .find(card => card.dataset.runId === intent.runId && !card.classList.contains('run-deletion-card'))
    const target = currentCard?.querySelector('.run-card-main')
      || (fallback?.next?.isConnected ? fallback.next : null)
      || (fallback?.previous?.isConnected ? fallback.previous : null)
      || runsMainRef.current
    target?.focus?.({ preventScroll: true })
  })
  const closeDeleteDialog = (restore = true) => {
    if (deleteDialogBusy) return
    const fallback = deleteDialogFocusRef.current
    deleteDialogFocusRef.current = null
    setDeleteDialog(null); setDeleteDialogError('')
    if (restore) requestAnimationFrame(() => {
      const target = fallback?.returnFocus?.isConnected ? fallback.returnFocus
        : fallback?.next?.isConnected ? fallback.next
          : fallback?.previous?.isConnected ? fallback.previous : runsMainRef.current
      target?.focus?.({ preventScroll: true })
    })
  }
  // Keep the compact drawer active while any list or menu mutation is pending.
  useDialogFocus(projectsDialogRef, navigationBusy ? null : () => setProjectsOpen(false), compactNav && projectsOpen)
  // Run creation is no longer a modal here — it happens INSIDE the assistant (the bottom command bar's
  // "/new" asks the assistant to propose a run, which renders as an inline launch card in the chat).

  // Poll the runs list every 2.5s, but skip the request while the tab is hidden (no point refreshing a
  // list nobody's looking at) — and refresh once immediately when it becomes visible again.
  usePoll(loadRuns, 2500, [], { pauseHidden: true })
  usePoll(() => Promise.all([loadProjects(), loadSupers()]), 15_000, [], { pauseHidden: true })

  const stName = useMemo(() => Object.fromEntries(superdata.supertasks.map(s => [s.id, s.name])), [superdata])
  const assignToSuper = async (run, sid) => {
    await assignSupertask(
      run.run_id, sid === UNASSIGNED ? null : sid, run.generation,
      run.supertask_id ?? null)
    await loadRuns()
  }

  const { byParent, subtree } = useMemo(() => indexProjects(proj.projects), [proj.projects])
  const projName = useMemo(() => Object.fromEntries(proj.projects.map(p => [p.id, p.name])), [proj.projects])

  const scoped = useMemo(() => scopeRuns(runs || [], sel, proj.projects),
    [runs, sel, proj.projects])
  const projectCounts = useMemo(() => projectRunCounts(runs || [], proj.projects),
    [runs, proj.projects])
  const count = id => projectCounts.get(id) || 0

  // Distinct task ids across all loaded runs — populates the task filter dropdown.
  const tasks = useMemo(
    () => Array.from(new Set((runs || []).map(r => r.task_id).filter(Boolean))).sort(),
    [runs])

  // List and Map consume this exact same derived result set.  Map no longer performs an independent
  // fetch, so switching representation cannot silently reset scope or show stale assignments.
  const filtered = useMemo(() => filterRuns(runs || [], {
    project: sel, projects: proj.projects, query, task: taskFilter,
    supertask: stFilter, status: statusFilter,
  }), [runs, sel, proj.projects, query, taskFilter, stFilter, statusFilter])
  const visible = useMemo(() => sortRuns(filtered, sortKey, sortDir), [filtered, sortKey, sortDir])
  const displayedRuns = visible.slice(0, listLimit)
  const renderedDeletionIds = new Set(view === 'list'
    ? displayedRuns.filter(run => deletionRecoveries.has(run.run_id)).map(run => run.run_id)
    : [])
  const missingDeletionRecoveries = [...deletionRecoveries.values()]
    .filter(recovery => !renderedDeletionIds.has(recovery.runId))
  const mapRuns = useMemo(() => visible.filter(run => !deletionRecoveries.has(run.run_id)),
    [visible, deletionRecoveries])
  const compareRuns = useMemo(() => {
    const byId = new Map((runs || []).map(run => [run.run_id, run]))
    return [...compareIds].filter(id => !deletionRecoveries.has(id))
      .map(id => byId.get(id)).filter(Boolean)
  }, [runs, compareIds, deletionRecoveries])
  const metricSortAvailable = taskFilter !== ALL && metricComparable(filtered)
  const hasActiveFilters = !!query.trim() || taskFilter !== ALL || statusFilter !== 'all' || stFilter !== ALL
  const clearFilters = () => {
    setQuery(''); setTaskFilter(ALL); setStatusFilter('all'); setStFilter(ALL)
  }
  useEffect(() => {
    if (sortKey === 'metric' && filtered.length > 0 && !metricSortAvailable) setSortKey('time')
  }, [sortKey, metricSortAvailable, filtered.length])
  useEffect(() => { storageSet(COMPARE_COLUMNS_KEY, JSON.stringify(compareColumns)) }, [compareColumns])
  useEffect(() => {
    if (!runs) return
    const liveIds = new Set(runs.map(run => run.run_id))
    setCompareIds(current => {
      const next = new Set([...current].filter(id => liveIds.has(id)))
      return next.size === current.size ? current : next
    })
  }, [runs])
  useEffect(() => {
    if (view === 'compare' && compareRuns.length < 2) setView('list')
  }, [view, compareRuns.length])
  useEffect(() => {
    if (projectsState === 'ready' && sel !== ALL && sel !== UNASSIGNED
        && !proj.projects.some(project => project.id === sel)) setSel(ALL)
    if (superState === 'ready' && stFilter !== ALL && stFilter !== UNASSIGNED
        && !superdata.supertasks.some(task => task.id === stFilter)) setStFilter(ALL)
  }, [projectsState, proj.projects, sel, superState, superdata.supertasks, stFilter])
  useEffect(() => setListLimit(LIST_PAGE_SIZE),
    [sel, query, taskFilter, statusFilter, stFilter, sortKey, sortDir])

  const portfolioState = {
    project: sel, query, task: taskFilter, status: statusFilter, supertask: stFilter,
    sort: sortKey, direction: sortDir, view, compare: [...compareIds], columns: compareColumns,
  }
  const activeView = savedViews.find(item => item.name === activeSavedView)
  const activeViewDirty = !!activeView
    && portfolioViewSignature(activeView) !== portfolioViewSignature(portfolioState)
  const applySavedView = name => {
    const saved = savedViews.find(item => item.name === name)
    if (!saved) { setActiveSavedView(''); return }
    setSel(saved.project); setQuery(saved.query); setTaskFilter(saved.task)
    setStatusFilter(saved.status); setStFilter(saved.supertask)
    setSortKey(saved.sort); setSortDir(saved.direction); setView(saved.view)
    setCompareIds(new Set(saved.compare)); setCompareColumns(saved.columns)
    setActiveSavedView(saved.name); setViewMessage('')
  }
  const savePortfolioView = async name => {
    const next = upsertPortfolioView(savedViews, name, portfolioState)
    if (!next.some(item => item.name === name)) {
      throw Object.assign(new Error('invalid portfolio view name'), { viewName: true })
    }
    if (!storageSet(SAVED_VIEWS_KEY, JSON.stringify(next))) {
      throw Object.assign(new Error('portfolio storage unavailable'), { storage: true })
    }
    setSavedViews(next); setActiveSavedView(name); setViewMessage(''); return true
  }
  const closeViewModal = () => {
    setViewModal(false); focusSoon(savedViewReturnRef.current)
  }
  const deleteSavedView = () => {
    if (!activeView || !confirm(`Delete saved view "${activeView.name}"?`)) return
    const next = savedViews.filter(item => item.name !== activeView.name)
    if (!storageSet(SAVED_VIEWS_KEY, JSON.stringify(next))) {
      setViewMessage('This browser could not delete the saved view. Storage may be blocked or full.')
      return
    }
    setSavedViews(next); setActiveSavedView(''); setViewMessage('')
  }
  const toggleCompare = runId => {
    const next = new Set(compareIds)
    if (next.delete(runId)) {
      setCompareIds(next); return
    }
    if (next.size >= 8) {
      setViewMessage('Compare is limited to 8 runs. Remove one before adding another.')
      return
    }
    next.add(runId); setViewMessage(''); setCompareIds(next)
  }

  const autoMapCollapsed = useMemo(
    () => defaultCollapsedClusters(proj.projects, mapRuns, subtree),
    [proj.projects, mapRuns, subtree])
  const mapCollapsed = useMemo(() => {
    const next = new Set(autoMapCollapsed)
    mapCollapseOverrides.forEach((collapsed, id) => collapsed ? next.add(id) : next.delete(id))
    return next
  }, [autoMapCollapsed, mapCollapseOverrides])
  const toggleMapCluster = (id) => setMapCollapseOverrides(current => {
    const next = new Map(current); next.set(id, !mapCollapsed.has(id)); return next
  })

  const breadcrumb = useMemo(() => {
    if (sel === ALL || sel === UNASSIGNED) return []
    const path = []; let cur = proj.projects.find(p => p.id === sel)
    while (cur) { path.unshift(cur); cur = proj.projects.find(p => p.id === cur.parent_id) }
    return path
  }, [sel, proj.projects])

  // The scope a cross-run report would cover, by the CURRENT view: an explicit super-task / task filter
  // is the user's intent to scope by it; otherwise the open folder. null = nothing reportable (All runs,
  // no filter) → hide the Report button.
  const scope = useMemo(() => {
    if (stFilter !== ALL && stFilter !== UNASSIGNED) return { type: 'supertask', id: stFilter, label: (stName[stFilter] || stFilter) }
    if (taskFilter !== ALL) return { type: 'task', id: taskFilter, label: 'task ' + taskFilter }
    if (sel !== ALL && sel !== UNASSIGNED) return { type: 'project', id: sel, label: (projName[sel] || sel) }
    return null
  }, [stFilter, taskFilter, sel, stName, projName])

  const toggle = (id) => setExpanded(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const refresh = () => Promise.all([loadProjects(), loadRuns()])
  const reconcileAll = () => Promise.all([loadProjects(), loadRuns(), loadSupers()])

  const restoreProjectModalFocus = () => {
    const target = projectModalReturnRef.current
    projectModalReturnRef.current = null
    focusSoon(target)
  }
  const addProject = (parent_id, returnFocus) => {
    projectModalReturnRef.current = returnFocus || document.activeElement
    setProjModal({ parent_id })
  }
  const closeProjectModal = () => { setProjModal(null); restoreProjectModalFocus() }
  const submitProject = async (name) => {
    const parent_id = projModal?.parent_id
    const p = await createProject(name, parent_id)
    if (parent_id) setExpanded(s => new Set(s).add(parent_id))
    await loadProjects(); setSel(p.id)
  }
  const startProjectRename = (id, returnFocus) => {
    clearProjectError(null)
    projectRenameReturnRef.current = returnFocus
    setRenaming(id)
  }
  const finishProjectRename = async (id, name, restoreFocus = false) => {
    const value = name?.trim()
    if (value && !await saveProjectRename(
      () => patchProject(id, { name: value }), loadProjects)) return false
    if (value) await loadProjects()
    else clearProjectError(null)
    setRenaming(null)
    if (restoreFocus) requestAnimationFrame(() => projectRenameReturnRef.current?.isConnected
      && projectRenameReturnRef.current.focus({ preventScroll: true }))
    return true
  }
  const removeProject = async (id, returnFocus) => {
    if (!confirm(`Delete project "${projName[id]}"? Sub-projects and runs move up to its parent.`)) return
    const removed = await mutateList('delete-project', `Deleting project “${projName[id]}”…`,
      () => deleteProject(id), refresh)
    if (removed && sel === id) setSel(ALL)
    if (removed) requestAnimationFrame(() => projectsAllRef.current?.focus({ preventScroll: true }))
    else focusSoon(returnFocus)
  }
  const moveRun = async (run, project_id) => {
    const target = project_id === UNASSIGNED ? 'Unassigned' : (projName[project_id] || 'project')
    return mutateList('move-run', `Moving run to “${target}”…`,
      () => assignRun(
        run.run_id, project_id === UNASSIGNED ? null : project_id, run.generation,
        run.project_id ?? null), refresh)
  }
  const onDrop = async (project_id) => {
    const run = dragRun
    if (!run || listBusy) return
    setDragRun(null)
    await moveRun(run, project_id)
  }
  const submitRunRename = async (label) => {
    await renameRun(
      runRename.run_id, label, runRename.generation, runRename.label ?? null)
    await loadRuns()
  }
  const openRunDeletion = (r) => {
    const returnFocus = runMenuTriggerRef.current
    const card = returnFocus?.closest('.run-card')
    deleteDialogFocusRef.current = {
      returnFocus,
      next: card?.nextElementSibling?.querySelector('.run-card-main'),
      previous: card?.previousElementSibling?.querySelector('.run-card-main'),
    }
    setRunMenu(null)
    setDeleteDialogError(''); setDeleteDialogBusy(false)
    setDeleteDialog({
      runId: String(r.run_id), label: String(r.label || ''),
      expectedGeneration: deletionGenerationOf(r), expectedSeq: r.seq,
      operationId: null, invalidated: false,
    })
  }
  const leaveDeleteDialogForRecovery = (intent) => {
    setDeleteDialog(current => current?.runId === intent.runId ? null : current)
    setDeleteDialogBusy(false); setDeleteDialogError('')
    deleteDialogFocusRef.current = null
    focusDeletionRecovery(intent.runId)
  }
  const finishRunDeletionReceipt = async (intent, receipt, { fromDialog = false } = {}) => {
    if (receipt.status === 'pending') {
      persistDeletionPhase(intent, 'pending', receipt.phase)
      if (fromDialog) leaveDeleteDialogForRecovery(intent)
      return
    }
    const listRead = await loadRuns()
    if (!listRead?.ok) {
      persistDeletionPhase(intent, 'pending', receipt.phase)
      setDeletionNotice({ kind: 'error', text: receipt.status === 'succeeded'
        ? 'Deletion is confirmed, but the current run list could not be refreshed. The exact recovery remains locked.'
        : 'The deletion operation is resolved, but the current run list could not be refreshed.' })
      if (fromDialog) leaveDeleteDialogForRecovery(intent)
      return
    }
    const exactTargetStillVisible = (listRead.value || []).some(run =>
      run.run_id === intent.runId
      && deletionGenerationOf(run) === intent.expectedGeneration
      && run.seq === intent.expectedSeq)
    if (receipt.status === 'succeeded' && exactTargetStillVisible) {
      persistDeletionPhase(intent, 'pending', receipt.phase)
      setDeletionNotice({ kind: 'error', text:
        'Deletion is confirmed, but the deleted generation is still present in the refreshed list. Recovery remains locked.' })
      if (fromDialog) leaveDeleteDialogForRecovery(intent)
      return
    }
    if (receipt.status === 'succeeded') {
      setCompareIds(current => {
        if (!current.has(intent.runId)) return current
        const next = new Set(current); next.delete(intent.runId); return next
      })
    }
    const cleared = clearDeletionRecovery(intent)
    setDeletionNotice(cleared
      ? { kind: 'status', text: `Run “${intent.runId}” was permanently deleted.` }
      : { kind: 'error', text:
          'The deletion operation is resolved, but this tab could not clear its recovery record safely.' })
    if (fromDialog) {
      setDeleteDialog(null); setDeleteDialogBusy(false); setDeleteDialogError('')
      deleteDialogFocusRef.current = null
    }
    if (cleared) focusAfterDeletion(intent)
    else focusDeletionRecovery(intent.runId)
  }
  const runDeletionRequest = async (intent, {
    initialRequest = false, fromDialog = false,
  } = {}) => {
    if (!intent || deletionRequestRef.current.has(intent.operationId)) return
    // GET observes a receipt but cannot roll a pending transaction forward. Every continuation uses
    // the exact persisted POST identity, which is idempotent and resumes the server state machine.
    const timed = deadlineRequest(signal => submitRunDeletion(
      intent.runId, intent.expectedGeneration, intent.expectedSeq,
      intent.operationId, { signal }), RUN_DELETION_TIMEOUT_MS)
    const request = { controller: timed.controller, intent }
    deletionRequestRef.current.set(intent.operationId, request)
    setDeletionBusyFor(intent.runId, true)
    if (fromDialog) setDeleteDialogBusy(true)
    try {
      const rawReceipt = await timed.promise
      if (deletionRequestRef.current.get(intent.operationId) !== request) return
      const receipt = exactRunDeletionReceipt(rawReceipt, intent)
      if (!receipt) {
        const protocol = new Error('Invalid deletion receipt')
        protocol.code = 'delete_protocol_error'
        throw protocol
      }
      await finishRunDeletionReceipt(intent, receipt, { fromDialog })
    } catch (error) {
      if (deletionRequestRef.current.get(intent.operationId) !== request
          || error?.name === 'AbortError') return
      const status = Number(error?.status)
      // Every continuation is the same idempotent POST. A definitive 4xx means that exact operation
      // was rejected; access failures remain unknown after a prior timeout because auth can stop the
      // retry before the server exposes an already accepted receipt.
      const authoritativeRejection = Number.isFinite(status)
        && status >= 400 && status < 500 && ![408, 425, 429].includes(status)
        && (initialRequest || ![401, 403].includes(status))
      if (authoritativeRejection && clearDeletionRecovery(intent)) {
        const rejectionMessage = runDeletionErrorMessage(error)
        if (fromDialog) {
          loadRuns()
          deletionFallbacksRef.current.delete(intent.operationId)
          setDeleteDialog(current => current?.runId === intent.runId
            ? { ...current, invalidated: true } : current)
          setDeleteDialogError(rejectionMessage)
          setDeleteDialogBusy(false)
        } else {
          setDeletionNotice({ kind: 'error', text: rejectionMessage })
          requestAnimationFrame(() => runsMainRef.current?.focus?.({ preventScroll: true }))
          const listRead = await loadRuns()
          if (![404, 410].includes(status) && listRead?.ok) {
            focusAfterDeletion(intent)
          } else {
            deletionFallbacksRef.current.delete(intent.operationId)
          }
        }
      } else {
        persistDeletionPhase(intent, 'unknown')
        setDeletionNotice({ kind: 'error', text:
          'Deletion outcome is not confirmed. Retry only the exact saved deletion before changing this run.' })
        if (fromDialog) leaveDeleteDialogForRecovery(intent)
      }
    } finally {
      if (deletionRequestRef.current.get(intent.operationId) === request) {
        deletionRequestRef.current.delete(intent.operationId)
        setDeletionBusyFor(intent.runId, false)
      }
      if (fromDialog) setDeleteDialogBusy(false)
      deleteConfirmLockRef.current = false
    }
  }
  const confirmRunDeletion = async () => {
    if (!deleteDialog || deleteDialog.invalidated || deleteConfirmLockRef.current) return
    const current = (runs || []).find(run => run.run_id === deleteDialog.runId)
    if (!current || deletionGenerationOf(current) !== deleteDialog.expectedGeneration
        || current.seq !== deleteDialog.expectedSeq
        || !RUN_GENERATION_RE.test(deleteDialog.expectedGeneration || '')
        || !Number.isSafeInteger(deleteDialog.expectedSeq) || deleteDialog.expectedSeq < -1) {
      setDeleteDialogError('The run changed before confirmation. Cancel and reopen Delete from the refreshed card.')
      return
    }
    deleteConfirmLockRef.current = true
    const operationId = createIdempotencyKey().toLowerCase()
    const intent = createRunDeletionIntent(
      deleteDialog.runId, deleteDialog.expectedGeneration, deleteDialog.expectedSeq, operationId)
    if (!intent || !saveRunDeletionIntent(intent)) {
      deleteConfirmLockRef.current = false
      const restored = loadRunDeletionIntent(deleteDialog.runId)
      if (restored.kind === 'active' || restored.kind === 'corrupt') {
        updateDeletionRecovery({ runId: deleteDialog.runId, ...restored })
      }
      setDeleteDialogError(
        'Deletion was not submitted because this tab could not preserve an exact recovery record.')
      return
    }
    const fallback = deleteDialogFocusRef.current || {}
    deletionFallbacksRef.current.set(operationId, fallback)
    updateDeletionRecovery({ runId: intent.runId, kind: 'active', intent })
    setDeleteDialog(currentDialog => currentDialog
      ? { ...currentDialog, operationId } : currentDialog)
    await runDeletionRequest(intent, { initialRequest: true, fromDialog: true })
  }
  const retryRunDeletion = recovery => {
    if (recovery.kind !== 'active') return
    runDeletionRequest(recovery.intent)
  }
  const pendingDeletionRecoveries = [...deletionRecoveries.values()]
    .filter(recovery => recovery.kind === 'active' && recovery.intent.phase !== 'unknown'
      && !recovery.storageUnavailable)
  const pendingDeletionKey = pendingDeletionRecoveries
    .map(recovery => `${recovery.intent.operationId}:${recovery.intent.phase}`).sort().join('|')
  usePoll(() => Promise.all(pendingDeletionRecoveries.map(recovery =>
    runDeletionRequest(recovery.intent))), RUN_DELETION_POLL_MS, [pendingDeletionKey], {
    pauseHidden: true, enabled: pendingDeletionRecoveries.length > 0,
  })
  // super-task CRUD (the buckets themselves; per-run assignment is assignToSuper above).
  const createSuper = async (name) => { await createSupertask(name); await loadSupers() }
  const renameSuper = async (id, name) => { await renameSupertask(id, name); await loadSupers() }
  const removeSuper = async (s) => {
    if (!confirm(`Delete super-task "${s.name}"? Runs in it become unassigned (the runs themselves are kept).`)) return false
    await deleteSupertask(s.id)
    if (stFilter === s.id) setStFilter(ALL)
    await Promise.all([loadSupers(), loadRuns()]); return true
  }

  // TreeNode lives at MODULE scope (below) so its component identity is stable across the 2.5s runs
  // poll; defined inline it remounted the whole subtree every poll, wiping the inline-rename input.
  const chooseProject = (id) => { setSel(id); setProjectsOpen(false) }
  const treeCtx = { byParent, expanded, sel, setSel: chooseProject, onDrop, toggle, renaming,
                    finishProjectRename, startProjectRename, projectBusy, projectError,
                    count, addProject, removeProject }
  const mutationNotice = listMutation?.busy
    ? <div className="notice" role="status">{listMutation.label}</div>
    : listMutation?.error
      ? <div className="notice resource-error" role="alert">{listMutation.error}{' '}
          <button type="button" className="btn sm" onClick={clearListMutation}>Dismiss</button>
        </div>
      : null

  return (
    <main ref={runsMainRef} className="app" data-route-main tabIndex={-1} aria-busy={navigationBusy}>
      <h1 className="sr-only">Runs</h1>
      <div className="topbar home-head"><span className="brand"><span className="dot">◉</span> LoopLab</span>
        <span className="muted home-subtitle">autonomous R&D — live runs</span>
        <button ref={projectsToggleRef} className="btn sm ghost projects-toggle" disabled={navigationBusy} onClick={() => setProjectsOpen(true)}
                aria-expanded={projectsOpen} aria-controls="projects-drawer">
          <OpIcon name="folder" className="t-ic" /> Projects
        </button>
        <button className="btn sm primary new-run-cta" disabled={navigationBusy}
                onClick={() => window.dispatchEvent(new CustomEvent('ll:new-run', { cancelable: true }))}>
          ＋ New run
        </button>
        <span className="spacer" style={{ flex: 1 }} />
        <div className="seg">
          <button aria-pressed={view === 'list'} className={view === 'list' ? 'on' : ''} onClick={() => setView('list')}><OpIcon name="list" className="t-ic" /> List</button>
          <button aria-pressed={view === 'map'} className={view === 'map' ? 'on' : ''} onClick={() => setView('map')}><OpIcon name="map" className="t-ic" /> Map</button>
          <button aria-pressed={view === 'compare'} className={view === 'compare' ? 'on' : ''}
            disabled={compareRuns.length < 2}
            title={compareRuns.length < 2 ? 'Select at least two runs from List' : 'Compare selected runs'}
            onClick={() => setView('compare')}>Compare · {compareRuns.length}</button>
        </div>
        {/* Slash remains a power-user shortcut; New run above is the first-use primary action. */}
        <span className="muted home-new-hint" style={{ fontSize: 11 }}>type <code className="cmd-hint">/new</code> in the bar below to start a run</span>
        <span className="spacer" style={{ flex: 1 }} />
        <div className="home-actions">
          <DensityToggle />
          <ThemeSwitcher />
          <EnergyToggle />
          <button type="button" className="btn sm ghost" disabled={navigationBusy} title="Read the experimental bounded portfolio preview"
                  aria-label="Open Research Atlas preview" onClick={() => onResearchAtlas?.()}>
            <OpIcon name="compass" className="t-ic" /> Atlas preview
          </button>
          <button className="btn sm ghost" disabled={navigationBusy} title="settings" onClick={() => onSettings && onSettings()}><OpIcon name="gear" className="t-ic" /> Settings</button>
        </div>
      </div>
      {missingStartOverRecoveries.map(item => <div key={item.runId}
        className="notice run-start-over-list-recovery"
        role={item.kind === 'corrupt' ? 'alert' : 'status'}>
        <span><b>Start over recovery</b> · <code>{item.runId}</code>{runListAuthoritative
          ? ' is temporarily absent from the run list.'
          : ' is not loaded in the run list yet.'} {item.kind === 'corrupt'
            ? 'Its saved recovery evidence is invalid.'
            : 'Its exact request is still preserved in this tab.'}</span>
        <button type="button" className="btn sm primary" onClick={() => onOpen(item.runId)}>
          Open recovery
        </button>
      </div>)}
      {deletionNotice && <div className={`notice run-deletion-notice ${deletionNotice.kind}`}
        role={deletionNotice.kind === 'error' ? 'alert' : 'status'}>
        <span>{deletionNotice.text}</span>
        <button type="button" className="btn sm" onClick={() => setDeletionNotice(null)}>Dismiss</button>
      </div>}
      {missingDeletionRecoveries.map(recovery => {
        const active = recovery.kind === 'active'
        const urgent = !active || recovery.intent.phase === 'unknown' || recovery.storageUnavailable
        const busy = deletionBusy.has(recovery.runId)
        return <div key={recovery.runId}
          ref={node => node
            ? deletionRecoveryRefs.current.set(recovery.runId, node)
            : deletionRecoveryRefs.current.delete(recovery.runId)}
          className={`notice run-deletion-list-recovery${urgent ? ' attention' : ''}`}
          role={urgent ? 'alert' : 'status'} tabIndex={-1} aria-busy={busy}>
          <span><b>Deletion recovery</b> · <code>{recovery.runId}</code> {runDeletionProgress(recovery)}</span>
          {active && <button type="button" className="btn sm"
            disabled={busy} onClick={() => retryRunDeletion(recovery)}>
            {busy ? 'Checking…'
              : recovery.intent.phase === 'unknown' ? 'Retry exact deletion' : 'Check exact deletion'}
          </button>}
        </div>
      })}

      <div className={'runlayout' + (projectsOpen ? ' projects-open' : '')}>
        {projectsOpen && <button className="project-backdrop" disabled={projectBusy} aria-disabled={navigationBusy || undefined}
                                 onClick={() => { if (!navigationBusy) setProjectsOpen(false) }}
                                 aria-label="Close projects" />}
        <aside ref={projectsDialogRef} className="psidebar" id="projects-drawer" aria-label="Projects"
               role={compactNav && projectsOpen && !projModal ? 'dialog' : undefined}
               aria-modal={compactNav && projectsOpen && !projModal ? 'true' : undefined}
               tabIndex={compactNav ? -1 : undefined}
               aria-hidden={compactNav && (!projectsOpen || !!projModal) ? 'true' : undefined}
               inert={compactNav && (!projectsOpen || !!projModal) ? '' : undefined}>
          {compactNav && projectsOpen && mutationNotice}
          <div inert={listBusy ? '' : undefined}>
            <div className="psidebar-h">
              <b>Projects</b>
              <button ref={projectsCloseRef} className="btn sm ghost projects-close" disabled={projectBusy} onClick={() => setProjectsOpen(false)}
                      aria-label="Close projects">×</button>
              <button className="btn sm" disabled={projectBusy} onClick={event => addProject(null, event.currentTarget)}>＋ New</button>
            </div>
            <button ref={projectsAllRef} type="button" className={'ptree-row pseudo' + (sel === ALL ? ' sel' : '')}
                 disabled={projectBusy} onClick={() => chooseProject(ALL)} aria-pressed={sel === ALL}>
              <span className="ptw">▦</span><span className="pname">All runs</span><span className="pcount">{count(ALL)}</span>
            </button>
            <button type="button" className={'ptree-row pseudo' + (sel === UNASSIGNED ? ' sel' : '')}
                 disabled={projectBusy} onClick={() => chooseProject(UNASSIGNED)} aria-pressed={sel === UNASSIGNED}
                 onDragOver={e => { if (!projectBusy) e.preventDefault() }} onDrop={() => { if (!projectBusy) onDrop(UNASSIGNED) }}>
              <span className="ptw">○</span><span className="pname">Unassigned</span><span className="pcount">{count(UNASSIGNED)}</span>
            </button>
            <nav className="ptree" aria-label="Project folders">
              {(byParent[null] || []).map(p => <TreeNode key={p.id} p={p} depth={0} ctx={treeCtx} />)}
              {projectsState === 'ready' && !proj.projects.length && <div className="muted" style={{ padding: 10, fontSize: 12 }}>No projects yet. Create one to organize runs.</div>}
            </nav>
          </div>
        </aside>

        <div className={'runlist' + (view === 'map' ? ' map-list-shell' : '')}>
          <div className="crumbs">
            <button type="button" className="crumb" disabled={navigationBusy} onClick={() => chooseProject(ALL)}>All runs</button>
            {breadcrumb.map(p => <React.Fragment key={p.id}><span className="sep">/</span>
              <button type="button" className="crumb" disabled={navigationBusy} onClick={() => chooseProject(p.id)}>{p.name}</button></React.Fragment>)}
            {sel === UNASSIGNED && <><span className="sep">/</span><span className="crumb">Unassigned</span></>}
            <span style={{ flex: 1 }} />
            {scope && <div className="view-toggle crumb-report">
              <button className={'vt report' + (showReport ? ' on' : '')} disabled={navigationBusy} title={`cross-run report for ${scope.label}`}
                onClick={() => setShowReport(true)}><OpIcon name="doc" size={12} /> Report<span className="vt-scope"> · {scope.label}</span></button>
            </div>}
          </div>
          {runs && <div className="portfolio-viewbar">
            <label>Saved view
              <select className="sel" aria-label="Saved portfolio view" value={activeSavedView}
                onChange={event => event.target.value
                  ? applySavedView(event.target.value) : setActiveSavedView('')}>
                <option value="">Custom / unsaved</option>
                {savedViews.map(saved => <option key={saved.name} value={saved.name}>
                  {saved.name}{saved.name === activeSavedView && activeViewDirty ? ' · modified' : ''}
                </option>)}
              </select>
            </label>
            <button className="btn sm" onClick={event => {
              savedViewReturnRef.current = event.currentTarget; setViewModal(true)
            }}>Save current view</button>
            <button className="btn sm ghost" disabled={!activeView} onClick={deleteSavedView}>Delete</button>
            {activeViewDirty && <span className="muted" role="status">Modified</span>}
          </div>}
          {viewMessage && <div className="notice resource-warning portfolio-message" role="alert">
            {viewMessage} <button className="btn xs" onClick={() => setViewMessage('')}>Dismiss</button>
          </div>}
          {runs && !!scoped.length && <div className="runbar">
            <OpIcon name="search" className="t-ic" />
            <input className="text runbar-q" aria-label="Filter runs" placeholder="filter runs…" value={query}
                   onChange={e => setQuery(e.target.value)} />
            <select className="sel" aria-label="Filter by status" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
              <option value="all">all status</option>
               <option value="running">running</option>
               <option value="finalizing">finalizing</option>
               <option value="paused">paused</option>
               <option value="approval">approval needed</option>
               <option value="stalled">stalled</option>
               <option value="unknown">ownership unknown</option>
              <option value="finished">finished</option>
            </select>
            <select className="sel" aria-label="Filter by task" value={taskFilter} onChange={e => setTaskFilter(e.target.value)}>
              <option value={ALL}>all tasks</option>
              {tasks.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
            <div className="runbar-super-control">
              <select className="sel" aria-label="Filter by super-task" value={stFilter} onChange={e => setStFilter(e.target.value)}>
                <option value={ALL}>all super-tasks</option>
                <option value={UNASSIGNED}>— no super-task —</option>
                {superdata.supertasks.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <button className="btn sm ghost" disabled={navigationBusy} aria-label="Create or manage super-tasks"
                title="create / manage super-tasks" onClick={event => openSuperTasks(event.currentTarget)}><OpIcon name="target" className="t-ic" /> ＋</button>
            </div>
            <span className="runbar-spacer" />
            <div className="runbar-sort-control">
              <span className="muted runbar-count">{visible.length}/{scoped.length}</span>
              <select className="sel" aria-label="Sort runs by" value={sortKey} onChange={e => {
                setSortKey(e.target.value)
                if (e.target.value === 'metric') setSortDir('asc')
              }}>
                <option value="time">time</option>
                <option value="name">name</option>
                <option value="metric" disabled={!metricSortAvailable}>best metric{metricSortAvailable ? '' : ' (select one task)'}</option>
                <option value="task">task</option>
                <option value="nodes">nodes</option>
                <option value="phase">phase</option>
              </select>
              <button className="btn sm ghost"
                      aria-label={`Sort ${sortKey === 'metric' ? (sortDir === 'asc' ? 'best first' : 'worst first') : (sortDir === 'asc' ? 'ascending' : 'descending')}`}
                      title={sortKey === 'metric' ? (sortDir === 'asc' ? 'best first' : 'worst first') : (sortDir === 'asc' ? 'ascending' : 'descending')}
                      onClick={() => setSortDir(d => d === 'asc' ? 'desc' : 'asc')}>
                {sortKey === 'metric' ? (sortDir === 'asc' ? 'best' : 'worst') : (sortDir === 'asc' ? '↑' : '↓')}
              </button>
            </div>
          </div>}
          {compareIds.size > 0 && view !== 'compare' && <div className="compare-selection" role="status">
            <b>{compareRuns.length}/8 selected</b>
            <span className="muted">{compareRuns.length < 2
              ? 'Select one more run to compare.' : 'Ready to compare run details.'}</span>
            <button className="btn sm primary" disabled={compareRuns.length < 2}
              onClick={() => setView('compare')}>Compare runs</button>
            <button className="btn sm ghost" onClick={() => setCompareIds(new Set())}>Clear</button>
          </div>}
          <ResourceNotice state={runsState} label="Runs" retry={loadRuns} />
          <ResourceNotice state={projectsState} label="Projects" retry={loadProjects} />
          {!stModal && <ResourceNotice state={superState} label="Super-tasks" retry={loadSupers} />}
          {(!compactNav || !projectsOpen) && mutationNotice}
          {runsState === 'ready' && runs && !scoped.length && <div className="notice resource-empty">No runs here.
            {sel === ALL
              ? <button className="btn sm primary" disabled={navigationBusy}
                  onClick={() => window.dispatchEvent(new CustomEvent('ll:new-run', { cancelable: true }))}>
                  Start a new run
                </button>
              : <span>Drag a run onto this project, or use its <b>Move</b> menu.</span>}</div>}
          {runs && !!scoped.length && !visible.length && <div className="notice" role="status">
            {runsState === 'stale' ? 'No runs in the last loaded data match the filters.' : 'No runs match the filters.'}
            {hasActiveFilters && <button type="button" className="btn sm" disabled={navigationBusy} onClick={clearFilters}>Clear filters</button>}
          </div>}
          {view === 'map' && ['ready', 'stale'].includes(projectsState) && runs && mapRuns.length > 0 && <div className="map-stage">
            <LazyBoundary label="run map" resetKey={`map:${sel}`}>
              <MapView onOpen={id => { if (!navigationBusy) onOpen(id) }} runs={mapRuns} projects={proj.projects}
                collapsed={mapCollapsed} onToggle={toggleMapCluster}
                scopeLabel={scope?.label || (sel === ALL ? 'All runs' : sel === UNASSIGNED ? 'Unassigned' : (projName[sel] || sel))} />
            </LazyBoundary>
          </div>}
          {view === 'compare' && compareRuns.length > 1 && <LazyBoundary label="run comparison"
            resetKey={compareRuns.map(run => run.run_id).join(':')}>
            <RunCompare runs={compareRuns} columns={compareColumns}
              names={{ projects: projName, supertasks: stName }}
              onColumns={setCompareColumns} onRemove={toggleCompare} />
          </LazyBoundary>}
          {view === 'list' && runs && displayedRuns.map(r => {
            const deletionRecovery = deletionRecoveries.get(r.run_id)
            if (deletionRecovery) return <RunDeletionCard key={r.run_id} run={r}
              recovery={deletionRecovery} busy={deletionBusy.has(r.run_id)}
              onRetry={retryRunDeletion}
              setRef={node => node
                ? deletionRecoveryRefs.current.set(r.run_id, node)
                : deletionRecoveryRefs.current.delete(r.run_id)} />
            const startOverLocked = getRunAccess(r.run_id).mode === 'start-over'
            const runGenerationAvailable = RUN_GENERATION_RE.test(r.generation || '')
            const openMenuRun = runMenu?.run_id === r.run_id ? runMenu : null
            const menuGenerationChanged = !!openMenuRun
              && openMenuRun.generation !== r.generation
            const menuMutationLocked = startOverLocked || !runGenerationAvailable
              || !RUN_GENERATION_RE.test(openMenuRun?.generation || '') || menuGenerationChanged
            const menuMutationLockReason = startOverLocked
              ? 'Start over is unresolved · open this run to recover it'
              : !runGenerationAvailable || !RUN_GENERATION_RE.test(openMenuRun?.generation || '')
                ? 'Rename and organization are unavailable without a current run generation'
                : 'This run changed while the menu was open · close and reopen it'
            const deletionGenerationAvailable = RUN_GENERATION_RE.test(deletionGenerationOf(r))
              && Number.isSafeInteger(r.seq) && r.seq >= -1
            const menuDeletionIdentityChanged = !!openMenuRun
              && (deletionGenerationOf(openMenuRun) !== deletionGenerationOf(r)
                || openMenuRun.seq !== r.seq)
            const menuDeleteLocked = startOverLocked || !deletionGenerationAvailable
              || !RUN_GENERATION_RE.test(deletionGenerationOf(openMenuRun))
              || !Number.isSafeInteger(openMenuRun?.seq) || openMenuRun.seq < -1
              || menuDeletionIdentityChanged
            const menuDeleteLockReason = startOverLocked
              ? 'Start over is unresolved · open this run to recover it'
              : !deletionGenerationAvailable
                  || !RUN_GENERATION_RE.test(deletionGenerationOf(openMenuRun))
                  || !Number.isSafeInteger(openMenuRun?.seq) || openMenuRun.seq < -1
                ? 'Refresh the Runs list before deleting this run'
                : 'This run changed while the menu was open · close and reopen it before deleting'
            return (
            <div className={'run-card' + (compareIds.has(r.run_id) ? ' compare-selected' : '')}
                 key={r.run_id} data-run-id={r.run_id}
                 draggable={!navigationBusy && !startOverLocked && runGenerationAvailable}
                 onPointerDownCapture={() => { compareDragGuardRef.current = null }}
                 onPointerUp={() => { compareDragGuardRef.current = null }}
                 onPointerCancel={() => { compareDragGuardRef.current = null }}
                 onDragStart={event => {
                   if (compareDragGuardRef.current === r.run_id) {
                     event.preventDefault()
                     compareDragGuardRef.current = null
                     return
                   }
                   if (!runGenerationAvailable) { event.preventDefault(); return }
                   setDragRun({
                     run_id: r.run_id, generation: r.generation,
                     project_id: r.project_id ?? null,
                   })
                 }}
                 onDragEnd={() => {
                   compareDragGuardRef.current = null
                   setDragRun(null)
                 }}>
              <label className="compare-toggle" draggable={false}
                onPointerDown={() => { compareDragGuardRef.current = r.run_id }}>
                <input type="checkbox" className="compare-check" draggable={false}
                  checked={compareIds.has(r.run_id)}
                  aria-label={`Select ${r.label || r.run_id} for comparison`}
                  onChange={() => toggleCompare(r.run_id)} />
              </label>
              {(() => {
                // A zombie (not finished, but no engine holds the lock) reads as "search" from phase
                // alone — surface it as "stalled" so the list matches the run header's badge.
                const status = effectiveRunStatus(r)
                const stalled = status === 'stalled'
                return <span className={'pill phase ' + status}
                             title={stalled ? 'engine stopped unexpectedly — open the run to resume'
                                : status === 'unknown' ? 'engine ownership could not be verified; inspect before acting'
                                : status === 'finalizing' ? 'wrapping up report, lessons, and cost'
                                : status === 'paused' ? 'paused intentionally' : undefined}>{status}</span>
              })()}
              <a className="run-card-main" href={`#/run/${encodeURIComponent(r.run_id)}`}
                   draggable={false}
                   aria-disabled={navigationBusy || undefined}
                   onClick={event => {
                     if (navigationBusy) { event.preventDefault(); return }
                     followClientRoute(event, () => onOpen(r.run_id))
                   }}
                   aria-label={`Open run ${r.label || r.run_id}`}>
                <div><b>{r.label || r.run_id}</b> <span className="muted">· {r.label ? r.run_id + ' · ' : ''}{r.task_id}</span>
                  {startOverLocked && <span className="pill warn" style={{ marginLeft: 6 }}>Start over recovery</span>}
                  {r.project_id && projName[r.project_id] && <span className="pill" style={{ marginLeft: 6 }}><OpIcon name="folder" className="t-ic" /> {projName[r.project_id]}</span>}
                  {r.supertask_id && stName[r.supertask_id] && <span className="pill st-pill" style={{ marginLeft: 6 }}><OpIcon name="target" className="t-ic" /> {stName[r.supertask_id]}</span>}</div>
                <div className="goal">{r.goal}</div>
              </a>
              <div className="run-card-metrics" style={{ textAlign: 'right' }}>
                <div>best <b>{fmt(r.best_confirmed ?? r.best_metric)}</b></div>
                <div className="muted">{r.nodes} nodes · {r.direction}</div>
                {r.mtime && <div className="muted run-when"
                  title={`started ${fmtDate(r.created)} · updated ${fmtDate(r.mtime)}`}>
                  {fmtAgo(r.mtime)}</div>}
              </div>
              <div className="run-actions">
                <button className="ic dots" disabled={navigationBusy} aria-label={`Actions for run ${r.label || r.run_id}`}
                        aria-haspopup="menu" aria-expanded={!!openMenuRun}
                        onClick={e => {
                          e.stopPropagation()
                          if (openMenuRun) closeRunMenu(false)
                          else { runMenuTriggerRef.current = e.currentTarget; setRunMenu({ ...r }) }
                        }}>⋮</button>
                {openMenuRun && <RunMenu r={openMenuRun} projects={proj.projects} supertasks={superdata.supertasks}
                  anchor={runMenuTriggerRef.current}
                  onOpen={onOpen} onMove={moveRun} onSetSuper={assignToSuper} onManageSupers={() => openSuperTasks(runMenuTriggerRef.current)}
                  onRename={openRunRename} onDelete={openRunDeletion} onReconcile={reconcileAll}
                  onClose={closeRunMenu} onBusyChange={setMenuBusy}
                  mutationLocked={menuMutationLocked}
                  mutationLockReason={menuMutationLockReason}
                  deleteLocked={menuDeleteLocked} deleteLockReason={menuDeleteLockReason} />}
              </div>
            </div>
            )
          })}
          {view === 'list' && displayedRuns.length < visible.length && <div className="run-list-more">
            <button type="button" className="btn sm"
              onClick={() => setListLimit(listLimit + LIST_PAGE_SIZE)}>Show more</button>
            <span className="muted">{displayedRuns.length}/{visible.length}</span>
          </div>}
        </div>
      </div>

      {deleteDialog && <RunDeleteDialog target={deleteDialog}
        currentRun={deleteDialogCurrentRun} busy={deleteDialogBusy} error={deleteDialogError}
        onClose={() => closeDeleteDialog(true)} onConfirm={confirmRunDeletion} />}

      {projModal && <PromptModal
        title={projModal.parent_id ? 'New sub-project' : 'New project'}
        label={projModal.parent_id ? `Inside “${projName[projModal.parent_id]}”` : 'Group runs into a project folder.'}
        placeholder="e.g. baseline sweep" confirm="Create"
        onSubmit={submitProject} onReconcile={refresh} onClose={closeProjectModal} />}

      {runRename && <PromptModal
        title="Rename run" label={`Display name for ${runRename.run_id} (clear it to fall back to the id).`}
        placeholder={runRename.run_id} initial={runRename.label || ''} confirm="Save" allowEmpty
        blocked={runRenameBlocked}
        blockedMessage="This run changed while Rename was open. Cancel and reopen Rename from the refreshed card."
        onSubmit={submitRunRename} onReconcile={loadRuns} onClose={closeRunRename} />}

      {viewModal && <PromptModal title="Save portfolio view"
        label="Name this scope, filter, sort, layout, and comparison selection."
        placeholder="e.g. active baselines" initial={activeView?.name || ''} confirm="Save view" maxLength={48}
        onSubmit={savePortfolioView} onClose={closeViewModal} />}

      {stModal && <SuperTaskModal supertasks={superdata.supertasks} state={superState} onRetry={loadSupers}
        onCreate={createSuper} onRename={renameSuper} onDelete={removeSuper}
        onReconcile={reconcileAll} onClose={closeSuperTasks} />}

      {showReport && scope && <LazyBoundary label="scope report" mode="overlay" resetKey={scope.label}
        onClose={() => setShowReport(false)}>
        <ScopeReport scope={scope}
          onOpen={(id) => { setShowReport(false); onOpen(id) }} onClose={() => setShowReport(false)} />
      </LazyBoundary>}
    </main>
  )
}
