import React, { lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { get, fmt, fmtDate, fmtAgo, listProjects, createProject, patchProject, deleteProject, assignRun, renameRun,
  createIdempotencyKey, submitRunDeletion, getRunMemoryAttribution, purgeRunMemory,
  listSupertasks, createSupertask, renameSupertask, deleteSupertask, assignSupertask,
  storageGet, storageSet } from './util.js'
import { useMediaQuery, usePoll } from './hooks.js'
import LazyBoundary from './LazyBoundary.jsx'
import ThemeSwitcher from './ThemeSwitcher.jsx'
import EnergyToggle from './EnergyToggle.jsx'
import DensityToggle from './DensityToggle.jsx'
import GlobalMenu from './GlobalMenu.jsx'
import { OpIcon } from './icons.jsx'
import {
  ALL_RUNS as ALL, SELECTION_MAX, UNASSIGNED_RUNS as UNASSIGNED, bestMetricCaveatLabel,
  bestMetricCaveatNotice, bestMetricCaveats, comparisonScope, filterRuns,
  indexProjects, effectiveRunStatus, metricComparable, projectRunCounts, scopeRuns, selectionNotice,
  sortRuns, sourceIncomplete, sourceIntegrityNotice,
} from './runIndex.js'
import { defaultCollapsedClusters } from './runMapModel.js'
import { DIALOG_PRIORITY, useDialogFocus } from './useDialogFocus.js'
import { followClientRoute, nextRovingIndex } from './accessibility.jsx'
import {
  decodePortfolioViews, MAX_PORTFOLIO_QUERY_LENGTH, MAX_PORTFOLIO_VIEW_NAME_LENGTH,
  normalizeCompareColumns, portfolioViewSignature, preparePortfolioViewSave,
} from './portfolioModel.js'
import { deadlineRequest } from './requestDeadline.js'
import { getRunAccess, listStartOverRunAccesses } from './runMode.js'
import { listRunStartOverRecoveries } from './runStartOverRecovery.js'
import {
  clearRunDeletionIntent, createRunDeletionIntent, listRunDeletionRecoveries,
  loadRunDeletionIntent, saveRunDeletionIntent,
} from './runDeletionRecovery.js'
import {
  bulkCascadeLabel, cascadeKeptNotice, cascadeLabel, cascadeOutcome, cascadeStores,
} from './memoryCascadeModel.js'
import {
  bulkDeletionPlan, bulkDeletionSummary, bulkOutcomeNotice, bulkProgressLabel,
} from './bulkDeleteModel.js'

const MapView = lazy(() => import('./MapView.jsx'))
const ScopeReport = lazy(() => import('./ScopeReport.jsx'))
const RunCompare = lazy(() => import('./RunCompare.jsx'))
const PortfolioConcepts = lazy(() => import('./PortfolioConcepts.jsx'))
// App writes `looplab` when the operator leaves through the LoopLab menu; `settings` is the value
// already persisted in older history entries and returns focus to the same control.
const returnsToGlobalMenu = control => control === 'looplab' || control === 'settings'
const SAVED_VIEWS_KEY = 'll.portfolioViews'
const COMPARE_COLUMNS_KEY = 'll.compareColumns'
const LIST_READ_TIMEOUT_MS = 12_000
const LIST_PAGE_SIZE = 200
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
const RUN_DELETION_TIMEOUT_MS = 12_000
// The cascade survey scans every cross-run store. Shorter than the deletion's own deadline on
// purpose: it is a preview, and a slow one must leave the dialog usable rather than hold it open.
const MEMORY_ATTRIBUTION_TIMEOUT_MS = 8_000
const RUN_DELETION_POLL_MS = 2_500
// How many `RUN_DELETION_POLL_MS` waits a BATCH gives one accepted-but-unfinished deletion before it
// stops and hands the operator the recovery record. Bounded rather than open-ended: the batch holds
// the operator's attention, and a deletion that is still working after this long is one they should
// be told about rather than watched indefinitely.
const BULK_DELETION_PENDING_POLLS = 8
const LIST_SORT_KEYS = new Set(['time', 'name', 'metric', 'task', 'nodes', 'phase'])
// The run list's four representations of ONE scoped run set. `map` is the KEY, `Lineage` is the label
// — see the view-toggle below for why the key must not follow the label.
const LIST_VIEWS = new Set(['list', 'map', 'concepts', 'compare'])
const LIST_STATUSES = new Set(['all', 'running', 'finalizing', 'paused', 'approval', 'stalled', 'unknown', 'finished'])
const TASK_SELECT_ALL = 'all'
const PORTFOLIO_VIEWS_LOCK = 'looplab:portfolio-views'
const PORTFOLIO_VIEWS_COORDINATION_DB = 'looplab-ui-coordination'
const PORTFOLIO_VIEWS_COORDINATION_STORE = 'locks'
const PORTFOLIO_VIEWS_COORDINATION_WAIT_MS = 2_000

const portfolioViewError = flag => Object.assign(new Error(flag), { [flag]: true })

const createPortfolioViewsCoordinator = () => {
  let indexedDb
  try { indexedDb = globalThis.indexedDB } catch { return null }
  if (!indexedDb?.open) return null
  let busy = false
  let closed = false
  let databasePromise = null
  const openDatabase = () => new Promise(resolve => {
    let request
    let settled = false
    let openTimer = null
    const finish = database => {
      if (settled) { database?.close(); return }
      settled = true
      clearTimeout(openTimer)
      resolve(database)
    }
    openTimer = setTimeout(() => finish(null), PORTFOLIO_VIEWS_COORDINATION_WAIT_MS)
    try { request = indexedDb.open(PORTFOLIO_VIEWS_COORDINATION_DB, 1) }
    catch { finish(null); return }
    request.onupgradeneeded = () => {
      try {
        const database = request.result
        if (!database.objectStoreNames.contains(PORTFOLIO_VIEWS_COORDINATION_STORE)) {
          database.createObjectStore(PORTFOLIO_VIEWS_COORDINATION_STORE)
        }
      } catch { try { request.transaction?.abort() } catch { /* The open will fail closed. */ } }
    }
    request.onerror = () => finish(null)
    request.onblocked = () => finish(null)
    request.onsuccess = () => {
      const database = request.result
      database.onversionchange = () => database.close()
      if (closed) { database.close(); finish(null); return }
      finish(database)
    }
  })
  const getDatabase = () => {
    if (!databasePromise) databasePromise = openDatabase()
    return databasePromise
  }
  return {
    async run(mutation) {
      if (closed) throw portfolioViewError('viewCoordination')
      if (busy) throw portfolioViewError('viewBusy')
      busy = true
      try {
        const database = await getDatabase()
        if (!database || closed) {
          databasePromise = null
          throw portfolioViewError('viewCoordination')
        }
        return await new Promise((resolve, reject) => {
          let transaction
          let result
          let failure = null
          let waitTimer = null
          const abortWith = error => {
            clearTimeout(waitTimer)
            failure = error || portfolioViewError('viewCoordination')
            try { transaction.abort() } catch { reject(failure) }
          }
          try {
            // Read/write transactions over the same object store are serialized across tabs. The
            // localStorage mutation runs synchronously while this transaction owns that browser-wide
            // turn, providing a real mutex even on insecure HTTP/LAN origins without Web Locks.
            transaction = database.transaction(PORTFOLIO_VIEWS_COORDINATION_STORE, 'readwrite')
            const store = transaction.objectStore(PORTFOLIO_VIEWS_COORDINATION_STORE)
            const guard = store.get(PORTFOLIO_VIEWS_LOCK)
            waitTimer = setTimeout(() => abortWith(portfolioViewError('viewBusy')),
              PORTFOLIO_VIEWS_COORDINATION_WAIT_MS)
            guard.onerror = () => abortWith(portfolioViewError('viewCoordination'))
            guard.onsuccess = () => {
              clearTimeout(waitTimer)
              if (closed) { abortWith(portfolioViewError('viewCoordination')); return }
              try {
                result = mutation()
                if (result && typeof result.then === 'function') {
                  throw portfolioViewError('viewCoordination')
                }
              } catch (error) { abortWith(error) }
            }
            transaction.onerror = () => {
              if (!failure) failure = portfolioViewError('viewCoordination')
            }
            transaction.onabort = () => {
              clearTimeout(waitTimer)
              reject(failure || portfolioViewError('viewCoordination'))
            }
            transaction.oncomplete = () => {
              clearTimeout(waitTimer)
              resolve(result)
            }
          } catch {
            databasePromise = null
            reject(portfolioViewError('viewCoordination'))
          }
        })
      } finally { busy = false }
    },
    close() {
      closed = true
      databasePromise?.then(database => database?.close())
    },
  }
}

const withPortfolioViewsLock = async (mutation, coordinator) => {
  const locks = typeof navigator !== 'undefined' ? navigator.locks : null
  if (!locks?.request) {
    if (coordinator) return coordinator.run(mutation)
    throw portfolioViewError('viewCoordination')
  }
  let callbackEntered = false
  try {
    const result = await locks.request(PORTFOLIO_VIEWS_LOCK, { ifAvailable: true }, async lock => {
      callbackEntered = true
      if (!lock) throw portfolioViewError('viewBusy')
      return mutation()
    })
    return result
  } catch (error) {
    // Older implementations may expose Web Locks but reject `ifAvailable` before entering the
    // callback. The IndexedDB coordinator provides the same cross-tab exclusion in that case.
    if (!callbackEntered) {
      if (coordinator) return coordinator.run(mutation)
      throw portfolioViewError('viewCoordination')
    }
    throw error
  }
}

const mutateStoredPortfolioViews = (transform, {
  onCommitted = null, coordinator = null, onSuperseded = null,
  supersededPreservesOutcome = null,
} = {}) => {
  let committedEncoding = null
  return withPortfolioViewsLock(() => {
    const current = decodePortfolioViews(storageGet(SAVED_VIEWS_KEY, '[]'))
    const next = transform(current)
    if (next && typeof next.then === 'function') throw portfolioViewError('viewCoordination')
    const encoded = JSON.stringify(next)
    if (!storageSet(SAVED_VIEWS_KEY, encoded)) throw portfolioViewError('storage')
    if (storageGet(SAVED_VIEWS_KEY, null) !== encoded) throw portfolioViewError('viewConflict')
    committedEncoding = encoded
    return next
  }, coordinator).then(next => {
    // The lock is released before this continuation. A queued peer may already have committed a
    // newer snapshot, and its storage event may even have rendered first. Never overwrite that UI
    // with this operation's now-stale `next`; accept success only if the operation-specific effect
    // is still present in the current authority, otherwise reconcile it and report conflict.
    const authoritativeRaw = storageGet(SAVED_VIEWS_KEY, null)
    if (authoritativeRaw !== committedEncoding) {
      const authoritative = decodePortfolioViews(authoritativeRaw)
      if (supersededPreservesOutcome?.(authoritative) === true) {
        onCommitted?.(authoritative)
        return authoritative
      }
      onSuperseded?.(authoritative)
      throw portfolioViewError('viewConflict')
    }
    onCommitted?.(next)
    return next
  })
}

const taskSelectValue = taskId => JSON.stringify(['task', String(taskId)])
const readTaskSelectValue = value => {
  if (value === TASK_SELECT_ALL) return { id: ALL, exact: false }
  try {
    const parsed = JSON.parse(value)
    if (Array.isArray(parsed) && parsed.length === 2 && parsed[0] === 'task'
        && typeof parsed[1] === 'string') return { id: parsed[1], exact: true }
  } catch { /* Options are generated locally; malformed values fail closed to All. */ }
  return { id: ALL, exact: false }
}

function mapNavigationSignature(projects, runs) {
  let hash = 0x811c9dc5
  const feed = value => {
    const text = String(value ?? '')
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index)
      hash = Math.imul(hash, 0x01000193)
    }
    hash ^= 0xff
    hash = Math.imul(hash, 0x01000193)
  }
  projects.forEach(project => {
    feed('project'); feed(project.id); feed(project.parent_id); feed(project.name)
  })
  runs.forEach(run => {
    feed('run'); feed(run.run_id); feed(run.project_id)
  })
  return `${projects.length}:${runs.length}:${(hash >>> 0).toString(36)}`
}

function normalizeListNavigation(value) {
  const saved = value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  const text = (candidate, fallback) => typeof candidate === 'string' ? candidate : fallback
  const task = text(saved.task, ALL)
  const compare = Array.isArray(saved.compare)
    ? [...new Set(saved.compare.filter(id => typeof id === 'string' && id).slice(0, SELECTION_MAX))] : []
  const mapCollapse = Array.isArray(saved.mapCollapse)
    ? saved.mapCollapse.filter(entry => Array.isArray(entry) && typeof entry[0] === 'string'
      && typeof entry[1] === 'boolean').slice(0, 500) : []
  const rawLimit = Number.isSafeInteger(saved.listLimit) ? saved.listLimit : LIST_PAGE_SIZE
  const rawScroll = typeof saved.scrollTop === 'number' && Number.isFinite(saved.scrollTop)
    ? saved.scrollTop : 0
  const mapViewport = saved.mapViewport && typeof saved.mapViewport === 'object'
    && !Array.isArray(saved.mapViewport)
    && Number.isFinite(saved.mapViewport.x) && Number.isFinite(saved.mapViewport.y)
    && Number.isFinite(saved.mapViewport.zoom)
    ? {
        x: Math.min(Math.max(saved.mapViewport.x, -1_000_000), 1_000_000),
        y: Math.min(Math.max(saved.mapViewport.y, -1_000_000), 1_000_000),
        zoom: Math.min(Math.max(saved.mapViewport.zoom, 0.15), 1.6),
        signature: typeof saved.mapViewport.signature === 'string'
          ? saved.mapViewport.signature.slice(0, 128) : '',
      }
    : null
  return {
    project: text(saved.project, ALL),
    query: text(saved.query, ''),
    task,
    // Legacy navigation used the raw `__all__` sentinel. Only the explicit flag distinguishes a
    // real task carrying that id; every other non-sentinel string remains exact for compatibility.
    taskExact: task !== ALL || (saved.task === ALL && saved.taskExact === true),
    status: LIST_STATUSES.has(saved.status) ? saved.status : 'all',
    supertask: text(saved.supertask, ALL),
    sort: LIST_SORT_KEYS.has(saved.sort) ? saved.sort : 'time',
    direction: saved.direction === 'asc' ? 'asc' : 'desc',
    view: LIST_VIEWS.has(saved.view) ? saved.view : 'list',
    compare,
    mapCollapse,
    mapViewport,
    activeSavedView: text(saved.activeSavedView, ''),
    listLimit: Math.min(Math.max(rawLimit, LIST_PAGE_SIZE), 10_000),
    scrollTop: Math.max(rawScroll, 0),
  }
}

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

function UnavailableFilterNotice({ id, state, children, actionLabel, disabled, onAction,
  controlRef, fallbackRef, mainRef }) {
  const noticeRef = useRef(null)
  useLayoutEffect(() => () => {
    const owner = noticeRef.current
    // A successful background refresh can remove this warning while its button owns focus. Hand
    // focus to the now-truthful select instead of letting the browser fall back to <body>.
    if (!owner?.contains(document.activeElement)) return
    focusAfterTransientAction(owner,
      () => [controlRef.current, fallbackRef.current, mainRef.current])
  }, [controlRef, fallbackRef, mainRef])
  return <div ref={noticeRef} id={id} className="notice resource-warning"
    role={state === 'ready' ? 'alert' : 'status'} aria-atomic="true">
    <span>{children}</span>
    <button type="button" className="btn sm" disabled={disabled}
      onClick={event => onAction(event.currentTarget)}>{actionLabel}</button>
  </div>
}

// The ONE place a mutation alert reflects server text. Every other branch is client-owned copy,
// because a generic failure carries nothing the operator can act on. These codes are the exception:
// the fence conflicts describe WHICH identity moved under the request, and only the server knows.
const FENCE_CONFLICT_CODES = new Set([
  'run_generation_changed', 'run_generation_unavailable', 'invalid_run_generation',
  'run_organization_changed', 'run_organization_unavailable', 'invalid_expected_organization',
])

// Reflected text is coerced and bounded on the same terms as `commandErrorMessage` in api.js — a
// malformed body can put a non-string (or a very long one) in either field, and joining those raw
// renders `[object Object]` into an alert. An envelope carrying neither field falls back to
// client-owned copy rather than to the empty string, which is what an unguarded join produced: a
// contentless alert exactly where the operator needed to be told their view had moved.
const fenceConflictMessage = error => {
  const parts = [error?.message, error?.remediation]
    .filter(part => typeof part === 'string' && part.trim())
    .map(part => part.trim())
  return parts.length
    ? parts.join(' ').slice(0, 500)
    : 'The run or project changed under this request; refresh before retrying.'
}

// Exported for the reflection test: the coercion/bound/fallback rules are the point of the helper,
// and a source-regex can only see that they are written, not that they hold.
export const __testFenceConflictMessage = fenceConflictMessage

const mutationMessage = (error, timedOut = false) => timedOut
  ? 'Save timed out; its outcome is unknown.'
  : error?.viewName
    ? 'Use a view name of 1–48 characters without control characters.'
  : error?.viewState
    ? 'Current filters or selections cannot be saved exactly. Shorten the filter to 240 characters; scope IDs support 500 characters and compared run IDs 255. Nothing was saved.'
  : error?.viewCapacity
    ? 'All 12 saved-view slots are in use. Enter an existing name to replace it, or cancel and delete one; existing views were kept.'
  : error?.viewExists
    ? 'A saved view with this name appeared in another tab. Save again to review and confirm replacing it.'
  : error?.viewBusy
    ? 'Saved views are being changed in another tab. Nothing was changed; try again.'
  : error?.viewConflict
    ? 'Saved views changed during this write. Current filters were kept; review the list before retrying.'
  : error?.viewCoordination
    ? 'This page cannot safely coordinate saved views. Nothing changed; allow site storage, open LoopLab through localhost or HTTPS, or use a current browser.'
  : error?.storage
    ? 'Browser storage is blocked or full; this view was not saved.'
  : FENCE_CONFLICT_CODES.has(error?.code)
    ? fenceConflictMessage(error)
  : error?.status === 409
    ? 'Conflict; current input or selection kept.'
    : error?.status === 503
      ? 'Unavailable; current input or selection kept.'
      : 'Save failed; current input or selection kept.'

const localMutationError = error => error?.storage || error?.viewName || error?.viewState
  || error?.viewCapacity || error?.viewExists || error?.viewBusy || error?.viewConflict
  || error?.viewCoordination

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
        if (localMutationError(settlement.error)) {
          setState({ message, cause: settlement.error }); return false
        }
        if (typeof reconcile === 'function') {
          const check = await settleWithin(reconcile, LIST_RECONCILE_TIMEOUT_MS)
          message += mutationReconcileSuffix(reconciliationStatus(check))
        } else {
          message += ' Reload this view before retrying.'
        }
        setState({ message, cause: settlement.error }); return false
      }
      const outcome = settlement.value
      // A caller may own a stronger reconciliation contract (for example useListMutation below).
      // Keep its explicit unknown/failure result authoritative instead of closing the menu as success.
      if (outcome === false) { setState(null); return false }
      setState(null); return true
    }
    catch (error) {
      let message = mutationMessage(error)
      if (localMutationError(error)) {
        setState({ message, cause: error }); return false
      }
      if (typeof reconcile === 'function') {
        const check = await settleWithin(reconcile, LIST_RECONCILE_TIMEOUT_MS)
        message += mutationReconcileSuffix(reconciliationStatus(check))
      } else {
        message += ' Reload this view before retrying.'
      }
      setState({ message, cause: error }); return false
    }
    finally { lock.current = false }
  }
  return [state === true, typeof state === 'object' && state ? state.message : '', run, setState,
    typeof state === 'object' && state ? state.cause : null]
}

const focusSoon = target => requestAnimationFrame(() => target?.isConnected && target.focus({ preventScroll: true }))

const transientActionOwnsFocus = owner => {
  if (!owner || typeof document === 'undefined') return
  const active = document.activeElement
  return active === owner || !active || active === document.body
    || active === document.documentElement || !active.isConnected
}

const focusAfterTransientAction = (owner, resolveTargets) => requestAnimationFrame(() => {
  if (!transientActionOwnsFocus(owner)) return
  const resolved = typeof resolveTargets === 'function' ? resolveTargets() : resolveTargets
  const target = (Array.isArray(resolved) ? resolved : [resolved]).find(candidate =>
    candidate?.isConnected && candidate.getClientRects?.().length
      && !candidate.matches?.(':disabled'))
  target?.focus({ preventScroll: true })
})

const listMutationMessage = (kind, error) => {
  // Same fence conflicts, same reflection rules — this was a second hand-maintained copy of both the
  // code list and the raw join, so a hardening applied to one silently missed the other.
  if (FENCE_CONFLICT_CODES.has(error?.code)) return fenceConflictMessage(error)
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

function PromptModal({ title, label, description = '', placeholder, initial = '', confirm = 'Create', allowEmpty = false,
  maxLength, blocked = false, blockedMessage = '',
  confirmationForValue = null, onSubmit, onReconcile, onClose }) {
  const reactId = React.useId().replace(/:/g, '')
  const inputId = `prompt-${reactId}`
  const descriptionId = description ? `${inputId}-description` : undefined
  const errorId = `${inputId}-error`
  const inputRef = useRef(null)
  const errorRef = useRef(null)
  const [v, setV] = useState(initial)
  const [busy, error, mutate, clearError, errorCause] = useMutation()
  const ok = !blocked && (allowEmpty || !!v.trim())
  const confirmation = typeof confirmationForValue === 'function'
    ? confirmationForValue(v.trim()) : null
  const fieldError = !!error && errorCause?.viewName
  const editCanResolveError = fieldError || errorCause?.viewCapacity || errorCause?.viewExists
  useEffect(() => {
    if (error) focusSoon(fieldError ? inputRef.current : errorRef.current)
  }, [error, fieldError])
  const go = async () => {
    if (ok && await mutate(() => onSubmit(v.trim(), confirmation), onReconcile)) onClose()
  }
  return <Modal title={title} onClose={onClose} busy={busy}>
    {label && <label htmlFor={inputId} className="muted"
      style={{ display: 'block', marginBottom: description ? 4 : 8 }}>{label}</label>}
    {description && <div id={descriptionId} className="muted" style={{ marginBottom: 8 }}>{description}</div>}
    <input ref={inputRef} id={inputId} className="text" autoFocus readOnly={busy || blocked}
           aria-label={label || title} aria-describedby={descriptionId}
           aria-invalid={fieldError || undefined} aria-errormessage={fieldError ? errorId : undefined}
           placeholder={placeholder} maxLength={maxLength} value={v} onChange={e => {
             setV(e.target.value)
             if (error && editCanResolveError) clearError(null)
           }}
           onKeyDown={e => { if (e.key === 'Enter') go(); if (e.key === 'Escape' && !busy) onClose() }} />
    {confirmation?.message && <div className="notice resource-warning" role="status" aria-live="polite">
      {confirmation.message}
    </div>}
    {blocked && blockedMessage && <div className="flag" role="alert">{blockedMessage}</div>}
    {error && <div ref={errorRef} id={errorId} className="flag" role="alert"
      tabIndex={fieldError ? undefined : -1}>{error}</div>}
    <div className="modal-actions">
      <button className="btn sm ghost" disabled={busy} onClick={onClose}>Cancel</button>
      <button className="btn sm primary" disabled={!ok || busy} onClick={go}>
        {busy ? 'Saving…' : confirmation?.confirm || confirm}
      </button>
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
    // Present only when a cascade was requested AND the deletion succeeded. Carried through rather
    // than validated field-by-field: it reports an outcome, and nothing is fenced on it.
    memory: value.memory && typeof value.memory === 'object' ? value.memory : null,
    // A `pending` receipt says the operation is unfinished; `retryable` says whether pressing again
    // can change that. Only an explicit `false` is read as "it cannot" — an older server that sends
    // no flag, or any other value, keeps the retrying behaviour rather than inventing a dead end.
    retryable: value.retryable === false ? false : true,
    message: typeof value.message === 'string' ? value.message : '',
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
    // "Refresh it before deleting" was true of a LIVE engine and false of the case that actually
    // reaches an operator: a run whose engine died mid-wrap-up is owned by nobody, so no amount of
    // refreshing moves it and the advice is a loop. Prefer the server's own remediation, which
    // names the command that clears it (`looplab finalize <run_dir>`); the fallback keeps both
    // readings rather than asserting the transient one.
    const remedy = typeof error?.remediation === 'string' ? error.remediation.trim() : ''
    if (remedy) return remedy
    return 'This run has an unfinished wrap-up. If its engine is still running, wait and refresh. '
      + 'If the engine has stopped, only completing the wrap-up clears this — run '
      + '`looplab finalize <run_dir>`, then delete.'
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

function RunMemoryCascade({ report, checked, disabled, onToggle, bulk = 0 }) {
  // The survey is a preview, so a slow or failed read must not block the deletion — the checkbox
  // simply stays off and says it does not know yet. Consent to a number nobody could show is not
  // consent, so an unknown survey never arrives pre-checked.
  const unavailable = report && report.available === false
  const nothing = !!report && report.available !== false && !(report.deletable | 0)
  const kept = cascadeKeptNotice(report)
  // A batch states the rule rather than a count — see `bulkCascadeLabel` — so it needs no survey and
  // is never disabled for the absence of one.
  const blocked = bulk ? disabled : (disabled || !report || unavailable || nothing)
  return <div className="run-delete-cascade">
    <label className="run-delete-cascade-row">
      <input type="checkbox" checked={checked} disabled={blocked}
        onChange={event => onToggle(event.target.checked)} />
      <span>{bulk ? bulkCascadeLabel(bulk)
        : report ? cascadeLabel(report) : 'Checking this run’s cross-run memory…'}</span>
    </label>
    {kept && <p className="run-delete-cascade-kept">{kept}</p>}
    {checked && <ul className="run-delete-cascade-stores">
      {cascadeStores(report).filter(store => store.deletable > 0).map(store =>
        <li key={store.store}>{store.deletable} {store.store}</li>)}
    </ul>}
  </div>
}

function RunDeleteDialog({ target, currentRun, busy, error, onClose, onConfirm,
  memoryReport, cascade, onCascadeToggle }) {
  const dialogRef = useRef(null)
  const errorRef = useRef(null)
  useDialogFocus(dialogRef, busy ? null : onClose, true,
    { priority: DIALOG_PRIORITY.DESTRUCTIVE })
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
        <RunMemoryCascade report={memoryReport} checked={cascade} disabled={busy}
          onToggle={onCascadeToggle} />
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

function RunBulkDeleteDialog({ dialog, state, onClose, onConfirm, onCascadeToggle }) {
  const dialogRef = useRef(null)
  const running = state?.running === true
  useDialogFocus(dialogRef, running ? null : onClose, true,
    { priority: DIALOG_PRIORITY.DESTRUCTIVE })
  const { ready, blocked } = dialog.plan
  const outcome = !running && state ? bulkOutcomeNotice(state) : null
  return <div className="overlay run-delete-overlay"
    onMouseDown={event => { if (!running && event.target === event.currentTarget) onClose() }}>
    <section ref={dialogRef} className="modal run-delete-dialog run-bulk-delete-dialog"
      role="alertdialog" aria-modal="true" aria-labelledby="run-bulk-delete-title"
      aria-describedby="run-bulk-delete-warning" aria-busy={running} tabIndex={-1}>
      <div className="modal-h"><b id="run-bulk-delete-title">
        {bulkDeletionSummary(dialog.plan)}</b></div>
      <div className="modal-b">
        <p className="run-delete-copy">
          Each run is deleted through its own exact transaction, one after another. This removes
          their events, experiments, traces, chat, reports and review access.
        </p>
        {/* The LIST, not a count. Twenty runs is exactly the size at which a number stops being
            something the operator can check against what they meant to select. */}
        {ready.length > 0 && <ul className="run-bulk-delete-list">
          {ready.map(target => <li key={target.runId}
            className={state?.done?.includes(target.runId) ? 'done' : ''}>
            <code>{target.runId}</code>{target.label ? <span> — {target.label}</span> : null}
          </li>)}
        </ul>}
        {blocked.length > 0 && <div className="run-bulk-delete-blocked">
          <b>{blocked.length} cannot be deleted right now:</b>
          <ul>{blocked.map(item => <li key={item.runId}>
            <code>{item.runId}</code> — {item.reason}</li>)}</ul>
        </div>}
        <p id="run-bulk-delete-warning" className="run-delete-warning">This cannot be undone.</p>
        <RunMemoryCascade report={null} checked={dialog.cascade} disabled={running}
          onToggle={onCascadeToggle} bulk={ready.length} />
        {running && <div className="flag" role="status">{bulkProgressLabel(state)}</div>}
        {outcome && <div className={`flag run-delete-error`} role="alert">{outcome.text}</div>}
        <div className="modal-actions">
          <button type="button" className="btn sm" data-dialog-initial-focus disabled={running}
            onClick={onClose}>{state && !running ? 'Close' : 'Cancel'}</button>
          <button type="button" className="btn sm danger"
            disabled={running || !ready.length || (state && !running && !state.stoppedAt)}
            onClick={onConfirm}>
            {running ? 'Deleting…' : `Delete ${ready.length} permanently`}</button>
        </div>
      </div>
    </section>
  </div>
}

function RunDeletionCard({ run, recovery, busy, onRetry, setRef }) {
  const active = recovery.kind === 'active'
  const repairRequired = active && recovery.intent.serverPhase === 'quarantine_ambiguous'
  const urgent = !active || recovery.intent.phase === 'unknown' || repairRequired
    || recovery.storageUnavailable
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
        : recovery.intent.phase === 'unknown' ? 'Retry exact deletion'
          : repairRequired ? 'Check after repair' : 'Check exact deletion'}</button>}
  </div>
}

// Per-run "⋮" dropdown: open / rename / move (project) / assign (super-task) / delete.
function RunMenu({ r, projects, supertasks, onOpen, onMove, onSetSuper, onManageSupers, onRename,
  onDelete, onReconcile, onClose, onBusyChange, mutationLocked = false,
  mutationLockReason = 'Resolve the existing run operation before changing this run', anchor = null,
  positionKey = '', deleteLocked = mutationLocked, deleteLockReason = mutationLockReason }) {
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
  }, [anchor, positionKey, projects.length, supertasks.length, mutationLocked, deleteLocked, busy, error])
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
      <div className="mi-label run-menu-context" title={r.label || r.run_id} aria-hidden="true">
        Run · <b>{r.label || r.run_id}</b>
      </div>
      <div className="mi-sep" />
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

export default function RunList({ onOpen, onGlobalNavigate,
  initialNavigationState = null, restoreFocusRunId = null, restoreFocusControl = null,
  onNavigationStateChange = null, onNavigationRestored = null }) {
  const initialNavigationRef = useRef()
  if (initialNavigationRef.current === undefined) {
    initialNavigationRef.current = normalizeListNavigation(initialNavigationState)
  }
  const initialNavigation = initialNavigationRef.current
  const compactNav = useMediaQuery('(max-width: 900px)')
  const [runs, runsState, loadRuns] = useResource(
    signal => get('/api/runs', { signal, cache: 'no-store' }), null)
  const [proj, projectsState, loadProjects] = useResource(
    signal => listProjects({ signal }), { projects: [], assignments: {} })
  const [sel, setSel] = useState(() => initialNavigation.project)
  const [expanded, setExpanded] = useState(() => new Set())
  const [renaming, setRenaming] = useState(null)   // project id being renamed (inline)
  const [projectBusy, projectError, saveProjectRename, clearProjectError] = useMutation()
  const [listMutation, mutateList, clearListMutation] = useListMutation()
  const [menuBusy, setMenuBusy] = useState(false)
  const projectRenameReturnRef = useRef(null)
  const runsMainRef = useRef(null)
  // Return-focus target for every LoopLab destination. Was the Settings button; the LoopLab menu is
  // now the single control the operator left the list FROM, so it is where focus comes back to.
  const globalMenuButtonRef = useRef(null)
  const projectsAllRef = useRef(null)
  const [dragRun, setDragRun] = useState(null)
  const compareDragGuardRef = useRef(null)
  const compareViewButtonRef = useRef(null)
  const compareHeadingRef = useRef(null)
  const compareEntryFocusRef = useRef(null)
  const compareSurfaceFocusRef = useRef(null)
  const filterInputRef = useRef(null)
  const taskFilterRef = useRef(null)
  const superFilterRef = useRef(null)
  const setCompareHeadingRef = useCallback(node => {
    compareHeadingRef.current = node
    const owner = compareEntryFocusRef.current
    if (!node || !owner) return
    compareEntryFocusRef.current = null
    focusAfterTransientAction(owner,
      () => [node, compareViewButtonRef.current, runsMainRef.current])
  }, [])
  const [view, setView] = useState(() => initialNavigation.view)
  const [mapCollapseOverrides, setMapCollapseOverrides] = useState(
    () => new Map(initialNavigation.mapCollapse))
  const [mapViewport, setMapViewport] = useState(() => initialNavigation.mapViewport)
  const [projModal, setProjModal] = useState(null) // {parent_id} → show create-project popup
  const projectModalReturnRef = useRef(null)
  const [runMenu, setRunMenu] = useState(null)     // exact run-list snapshot whose ⋮ menu is open
  const runMenuTriggerRef = useRef(null)
  const runModalReturnFocusRef = useRef(null)
  const [runRename, setRunRename] = useState(null) // run object being renamed (popup)
  const [deleteDialog, setDeleteDialog] = useState(null)
  const [deleteDialogBusy, setDeleteDialogBusy] = useState(false)
  const [deleteDialogError, setDeleteDialogError] = useState('')
  const [deleteCascade, setDeleteCascade] = useState(false)
  const [memoryRetryBusy, setMemoryRetryBusy] = useState(false)
  const [bulkDeleteDialog, setBulkDeleteDialog] = useState(null)
  const [bulkDeleteState, setBulkDeleteState] = useState(null)
  // The batch reads the run list between deletions, so it needs the LATEST rows, not the ones its
  // closure captured when the operator pressed the button.
  const runsRef = useRef(null)
  const deletionRecoveriesRef = useRef(null)
  const [deleteMemoryReport, setDeleteMemoryReport] = useState(null)
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
  const [sortKey, setSortKey] = useState(() => initialNavigation.sort)
  const [sortDir, setSortDir] = useState(() => initialNavigation.direction)
  const [query, setQuery] = useState(() => initialNavigation.query)
  const [taskFilter, setTaskFilter] = useState(() => initialNavigation.task)
  const [taskFilterExact, setTaskFilterExact] = useState(() => initialNavigation.taskExact)
  const [statusFilter, setStatusFilter] = useState(() => initialNavigation.status)
  const [stFilter, setStFilter] = useState(() => initialNavigation.supertask)
  const [superdata, superState, loadSupers] = useResource(
    signal => listSupertasks({ signal }), { supertasks: [], assignments: {} })
  const [stModal, setStModal] = useState(false)             // manage-super-tasks popup open?
  const [showReport, setShowReport] = useState(false)       // cross-run scope-report panel open?
  const [compareIds, setCompareIds] = useState(() => new Set(initialNavigation.compare))
  const [compareColumns, setCompareColumns] = useState(
    () => normalizeCompareColumns(storageGet(COMPARE_COLUMNS_KEY)))
  const [savedViews, setSavedViews] = useState(
    () => decodePortfolioViews(storageGet(SAVED_VIEWS_KEY, '[]')))
  const [activeSavedView, setActiveSavedView] = useState(() => initialNavigation.activeSavedView)
  const [viewModal, setViewModal] = useState(false)
  const [viewMessage, setViewMessage] = useState('')
  const [staleSavedViewName, setStaleSavedViewName] = useState('')
  const [listLimit, setListLimit] = useState(() => initialNavigation.listLimit)
  const savedViewsRef = useRef(savedViews)
  const activeSavedViewRef = useRef(activeSavedView)
  const savedViewSelectRef = useRef(null)
  const saveViewButtonRef = useRef(null)
  const deleteSavedViewRef = useRef(null)
  const portfolioCoordinatorRef = useRef(null)
  const commitSavedViewsState = next => {
    savedViewsRef.current = next
    setSavedViews(next)
  }
  const commitActiveSavedViewState = next => {
    activeSavedViewRef.current = next
    setActiveSavedView(next)
  }
  const reconcileSavedViewsState = (next, announce = false) => {
    const previous = savedViewsRef.current
    if (JSON.stringify(previous) === JSON.stringify(next)) return false
    const activeName = activeSavedViewRef.current
    if (activeName) {
      const before = previous.find(item => item.name === activeName)
      const after = next.find(item => item.name === activeName)
      if (!after || portfolioViewSignature(before) !== portfolioViewSignature(after)) {
        const deleteOwnedFocus = document.activeElement === deleteSavedViewRef.current
        // Keep the visible candidate intact. Clearing only the association prevents an external
        // update from being silently overwritten as though this tab still owned that saved view.
        commitActiveSavedViewState('')
        setStaleSavedViewName(activeName)
        if (deleteOwnedFocus) {
          focusAfterTransientAction(deleteSavedViewRef.current,
            () => [savedViewSelectRef.current, saveViewButtonRef.current])
        }
      }
    }
    commitSavedViewsState(next)
    if (announce) setViewMessage('Saved views changed in another tab. Current filters were kept.')
    return true
  }
  const runListRef = useRef(null)
  const listScrollTopRef = useRef(initialNavigation.scrollTop)
  useEffect(() => {
    const coordinator = createPortfolioViewsCoordinator()
    portfolioCoordinatorRef.current = coordinator
    return () => {
      if (portfolioCoordinatorRef.current === coordinator) portfolioCoordinatorRef.current = null
      coordinator?.close()
    }
  }, [])
  useEffect(() => {
    const syncSavedViews = event => {
      if (event && event.key !== SAVED_VIEWS_KEY && event.key !== null) return
      // A queued storage event is only an invalidation signal: its newValue may already be older
      // than this tab's own newer commit. Re-read the authoritative value at handling time.
      const next = decodePortfolioViews(storageGet(SAVED_VIEWS_KEY, '[]'))
      reconcileSavedViewsState(next, true)
    }
    window.addEventListener('storage', syncSavedViews)
    // Subscribe before the authoritative reread so a peer write between the state initializer and
    // this passive effect cannot leave the mounted list stale indefinitely.
    syncSavedViews()
    return () => window.removeEventListener('storage', syncSavedViews)
  }, [])
  const navigationRestoreStartedRef = useRef(false)
  const [projectsOpen, setProjectsOpen] = useState(false)   // compact-screen Projects drawer
  const projectsToggleRef = useRef(null)
  const projectsCloseRef = useRef(null)
  const projectsDialogRef = useRef(null)
  const previousCompactNavRef = useRef(compactNav)
  const projectsBreakpointFocusRef = useRef(null)
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
  useEffect(() => { runsRef.current = runs }, [runs])
  // Mirrored for the SAME reason `runs` is: `runBulkDeletion` runs across many awaits, and the
  // recovery map it must consult is the one the batch itself has been WRITING — `updateDeletionRecovery`
  // adds an active record per run, and a non-4xx failure leaves it. Reading the render-time state
  // instead let the post-batch plan re-list the run that stopped the batch as `ready`, so the next
  // press re-stopped on it (`saveRunDeletionIntent` refuses while an unresolved intent exists) and
  // never reached the runs behind it — a wedged batch, recoverable only by reopening the dialog.
  useEffect(() => { deletionRecoveriesRef.current = deletionRecoveries }, [deletionRecoveries])
  useEffect(() => {
    deletionMountedRef.current = true
    return () => {
      deletionMountedRef.current = false
      for (const request of deletionRequestRef.current.values()) request.controller.abort()
      deletionRequestRef.current.clear()
    }
  }, [])
  // The cascade preview. `deadlineRequest` returns a HANDLE, not a promise — awaiting it directly
  // throws inside the effect and the section renders as a permanent "Checking…", which is exactly
  // the defect the card-trace fetches shipped with. Read `.promise`, abort through `.controller`.
  const deleteDialogRunId = deleteDialog?.runId || ''
  useEffect(() => {
    if (!deleteDialogRunId) { setDeleteMemoryReport(null); return undefined }
    let live = true
    const request = deadlineRequest(
      signal => getRunMemoryAttribution(deleteDialogRunId, { signal }),
      MEMORY_ATTRIBUTION_TIMEOUT_MS)
    request.promise.then(
      value => { if (live) setDeleteMemoryReport(value && typeof value === 'object' ? value : null) },
      // A failed survey leaves the checkbox disabled rather than guessing. Offering a cascade whose
      // extent we could not read would be asking for consent to an unknown deletion.
      () => { if (live) setDeleteMemoryReport({ available: false, deletable: 0, kept: 0 }) })
    return () => { live = false; request.controller.abort() }
  }, [deleteDialogRunId])
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
      phase, serverPhase, undefined, intent.deleteMemory === true)
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
  const deletionFocusOwned = (runId, allowLost = false) => {
    if (typeof document === 'undefined') return false
    const active = document.activeElement
    if (!active || active === document.body || !active.isConnected) return allowLost
    const surface = deletionRecoveryRefs.current.get(runId)
    return !!surface && (surface === active || surface.contains(active))
  }
  const focusDeletionRecovery = (runId, shouldFocus = () => true) => requestAnimationFrame(() => {
    if (!shouldFocus()) return
    ;(deletionRecoveryRefs.current.get(runId) || runsMainRef.current)
      ?.focus?.({ preventScroll: true })
  })
  const focusAfterDeletion = (intent, shouldFocus = () => true) => requestAnimationFrame(() => {
    const fallback = deletionFallbacksRef.current.get(intent.operationId)
    deletionFallbacksRef.current.delete(intent.operationId)
    if (!deletionMountedRef.current || !shouldFocus()) return
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
  useDialogFocus(projectsDialogRef, navigationBusy ? null : () => setProjectsOpen(false),
    compactNav && projectsOpen, { priority: DIALOG_PRIORITY.NAVIGATION })
  useEffect(() => {
    const rememberProjectsFocus = event => {
      const target = event.target
      projectsBreakpointFocusRef.current = projectsDialogRef.current?.contains(target)
        || projectsToggleRef.current?.contains(target) ? target : null
    }
    document.addEventListener('focusin', rememberProjectsFocus)
    return () => document.removeEventListener('focusin', rememberProjectsFocus)
  }, [])
  useEffect(() => {
    const wasCompact = previousCompactNavRef.current
    if (wasCompact === compactNav) return
    previousCompactNavRef.current = compactNav
    setProjectsOpen(false)

    const previousFocus = projectsBreakpointFocusRef.current
    const focusBelongedToProjects = previousFocus && (
      projectsDialogRef.current?.contains(previousFocus)
      || projectsToggleRef.current?.contains(previousFocus)
    )
    if (!focusBelongedToProjects) return

    const preservedDesktopFocus = !compactNav
      && previousFocus !== projectsCloseRef.current
      && projectsDialogRef.current?.contains(previousFocus)
      && previousFocus.isConnected
      ? previousFocus
      : null
    const target = compactNav
      ? projectsToggleRef.current
      : preservedDesktopFocus || projectsAllRef.current
    const focusTarget = target?.isConnected && target.getClientRects().length
      && !target.matches?.(':disabled') ? target : runsMainRef.current
    focusTarget?.focus?.({ preventScroll: true })
  }, [compactNav])
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

  const { byParent, byId: projectById, subtree } = useMemo(
    () => indexProjects(proj.projects), [proj.projects])
  const projName = useMemo(() => Object.fromEntries(proj.projects.map(p => [p.id, p.name])), [proj.projects])

  // A restored project id is only trustworthy once it exists in an authoritative or last-good
  // project tree. Applying it to the empty initial placeholder creates a misleading direct-only
  // scope (and often a false "No runs here"). Keep the saved intent in `sel`, withhold actionable
  // results until it can be verified, and let the operator explicitly choose All runs if needed.
  const customProjectSelected = sel !== ALL && sel !== UNASSIGNED
  const projectScopeVerified = !customProjectSelected
    || (['ready', 'stale'].includes(projectsState) && !!projectById[sel])
  const projectScopeBlocked = customProjectSelected && !projectScopeVerified
  const projectScopeMissing = projectScopeBlocked && projectsState === 'ready'
  const projectScopePending = projectScopeBlocked && !projectScopeMissing

  const scoped = useMemo(() => projectScopeBlocked ? [] : scopeRuns(runs || [], sel, proj.projects),
    [projectScopeBlocked, runs, sel, proj.projects])
  const projectCounts = useMemo(() => projectRunCounts(runs || [], proj.projects),
    [runs, proj.projects])
  const count = id => projectCounts.get(id) || 0

  // Distinct task ids across all loaded runs — populates the task filter dropdown.
  const tasks = useMemo(
    () => Array.from(new Set((runs || []).map(r => r.task_id).filter(Boolean))).sort(),
    [runs])
  // Restored navigation and saved views may outlive the flat registries that populate these
  // selects. A controlled select without a matching option visually falls back to its first option
  // even though React state (and filterRuns) keeps the missing id. Preserve the exact flat-id scope,
  // but always render its truth and require an explicit operator action before widening to All.
  const taskFilterKnown = !taskFilterExact || tasks.includes(taskFilter)
  const taskFilterUnavailable = taskFilterExact && !taskFilterKnown
  const taskFilterOptionLabel = runsState === 'ready'
    ? `${taskFilter} — no current runs`
    : runsState === 'stale'
      ? `${taskFilter} — not in last loaded runs`
      : `${taskFilter} — selected task unverified`
  const superFilterKnown = stFilter === ALL || stFilter === UNASSIGNED
    || superdata.supertasks.some(task => task.id === stFilter)
  const superFilterUnavailable = stFilter !== ALL && stFilter !== UNASSIGNED && !superFilterKnown
  const superFilterOptionLabel = superState === 'ready'
    ? `${stFilter} — removed super-task`
    : superState === 'stale'
      ? `${stFilter} — not in last loaded super-tasks`
      : superState === 'loading'
        ? `${stFilter} — checking selected super-task`
        : `${stFilter} — super-task registry unavailable`

  // List and Map consume this exact same derived result set.  Map no longer performs an independent
  // fetch, so switching representation cannot silently reset scope or show stale assignments.
  const filtered = useMemo(() => projectScopeBlocked ? [] : filterRuns(runs || [], {
    project: sel, projects: proj.projects, query, task: taskFilter,
    taskExact: taskFilterExact,
    supertask: stFilter, status: statusFilter,
  }), [projectScopeBlocked, runs, sel, proj.projects, query, taskFilter, taskFilterExact,
    stFilter, statusFilter])
  const visible = useMemo(() => sortRuns(filtered, sortKey, sortDir), [filtered, sortKey, sortDir])
  const displayedRuns = visible.slice(0, listLimit)
  const displayedRunOrder = displayedRuns.map(run => run.run_id).join('\u001f')
  const renderedDeletionIds = new Set(view === 'list'
    ? displayedRuns.filter(run => deletionRecoveries.has(run.run_id)).map(run => run.run_id)
    : [])
  const missingDeletionRecoveries = [...deletionRecoveries.values()]
    .filter(recovery => !renderedDeletionIds.has(recovery.runId))
  const mapRuns = useMemo(() => visible.filter(run => !deletionRecoveries.has(run.run_id)),
    [visible, deletionRecoveries])
  const mapViewportSignature = useMemo(
    () => mapNavigationSignature(proj.projects, mapRuns), [proj.projects, mapRuns])
  const compareRuns = useMemo(() => {
    const byId = new Map((runs || []).map(run => [run.run_id, run]))
    return [...compareIds].filter(id => !deletionRecoveries.has(id))
      .map(id => byId.get(id)).filter(Boolean)
  }, [runs, compareIds, deletionRecoveries])
  const visibleCompareControl = (attribute, runId) => [...(
    runsMainRef.current?.querySelectorAll(`[${attribute}]`) || [])]
    .find(control => control.getAttribute(attribute) === String(runId)
      && control.getClientRects().length && !control.matches(':disabled'))
  const compareCheckboxFor = runId => visibleCompareControl('data-compare-run-id', runId)
  const compareRemoveFor = runId => visibleCompareControl('data-compare-remove-id', runId)
  const metricSortAvailable = taskFilterExact && metricComparable(filtered)
  const hasActiveFilters = !!query.trim() || taskFilterExact
    || statusFilter !== 'all' || stFilter !== ALL
  const listCriteriaKey = JSON.stringify([
    sel, query, taskFilter, taskFilterExact, statusFilter, stFilter, sortKey, sortDir,
  ])
  const previousListCriteriaKeyRef = useRef(listCriteriaKey)
  const clearFilters = focusOwner => {
    setQuery(''); setTaskFilter(ALL); setTaskFilterExact(false)
    setStatusFilter('all'); setStFilter(ALL)
    focusAfterTransientAction(focusOwner, () => [filterInputRef.current, runsMainRef.current])
  }
  const useAllForUnavailableFilter = (clear, controlRef, focusOwner) => {
    clear()
    focusAfterTransientAction(focusOwner,
      () => [controlRef.current, filterInputRef.current, runsMainRef.current])
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
    if (view !== 'compare') {
      compareEntryFocusRef.current = null
      compareSurfaceFocusRef.current = null
      return
    }
    if (!runs || (!projectScopeBlocked && compareRuns.length >= 2)) return
    const owner = compareEntryFocusRef.current || compareSurfaceFocusRef.current
    compareEntryFocusRef.current = null
    compareSurfaceFocusRef.current = null
    setView('list')
    if (owner) {
      const returnRunId = compareRuns[0]?.run_id || [...compareIds][0]
      focusAfterTransientAction(owner,
        () => [compareCheckboxFor(returnRunId), filterInputRef.current, runsMainRef.current])
    }
  }, [runs, view, projectScopeBlocked, compareRuns, compareIds])
  useEffect(() => {
    if (projectsState === 'ready' && sel !== ALL && sel !== UNASSIGNED
        && !proj.projects.some(project => project.id === sel)) {
      setViewMessage('Saved project no longer exists. Switched to All runs.')
      setSel(ALL)
    }
  }, [projectsState, proj.projects, sel])
  useEffect(() => {
    if (previousListCriteriaKeyRef.current === listCriteriaKey) return
    previousListCriteriaKeyRef.current = listCriteriaKey
    setListLimit(LIST_PAGE_SIZE)
  }, [listCriteriaKey])
  const portfolioState = {
    project: sel, query, task: taskFilter, status: statusFilter, supertask: stFilter,
    sort: sortKey, direction: sortDir, view, compare: [...compareIds], columns: compareColumns,
    ...(taskFilterExact && taskFilter === ALL ? { taskExact: true } : {}),
  }
  const captureNavigationState = useCallback(() => ({
    project: sel,
    query,
    task: taskFilter,
    ...(taskFilterExact && taskFilter === ALL ? { taskExact: true } : {}),
    status: statusFilter,
    supertask: stFilter,
    sort: sortKey,
    direction: sortDir,
    view,
    compare: [...compareIds],
    mapCollapse: [...mapCollapseOverrides],
    mapViewport,
    activeSavedView,
    listLimit,
    scrollTop: initialNavigationState && !navigationRestoreStartedRef.current
      ? listScrollTopRef.current
      : runListRef.current?.scrollTop ?? listScrollTopRef.current,
  }), [sel, query, taskFilter, taskFilterExact, statusFilter, stFilter, sortKey, sortDir, view,
    compareIds, mapCollapseOverrides, mapViewport, activeSavedView, listLimit,
    initialNavigationState])
  const publishNavigationState = useCallback((options = null) => {
    const snapshot = captureNavigationState()
    onNavigationStateChange?.(snapshot, options)
    return snapshot
  }, [captureNavigationState, onNavigationStateChange])
  const publishNavigationStateRef = useRef(publishNavigationState)
  publishNavigationStateRef.current = publishNavigationState
  const captureNavigationStateRef = useRef(captureNavigationState)
  captureNavigationStateRef.current = captureNavigationState
  const navigationCallbacksRef = useRef({ onNavigationStateChange, onNavigationRestored })
  navigationCallbacksRef.current = { onNavigationStateChange, onNavigationRestored }
  useLayoutEffect(() => { publishNavigationState() }, [publishNavigationState])
  useEffect(() => {
    const persistCurrentNavigation = () => publishNavigationStateRef.current()
    window.addEventListener('pagehide', persistCurrentNavigation)
    window.addEventListener('beforeunload', persistCurrentNavigation)
    return () => {
      window.removeEventListener('pagehide', persistCurrentNavigation)
      window.removeEventListener('beforeunload', persistCurrentNavigation)
    }
  }, [])

  const openRun = useCallback((id, href = null) => {
    onOpen?.(id, publishNavigationState(), href)
  }, [onOpen, publishNavigationState])
  const openGlobal = useCallback(hash => {
    onGlobalNavigate?.(hash, publishNavigationState())
  }, [onGlobalNavigate, publishNavigationState])

  useLayoutEffect(() => {
    if (!initialNavigationState) return
    const active = document.activeElement
    if (!active || active === document.body || active === document.documentElement) {
      const controlTarget = !restoreFocusRunId && returnsToGlobalMenu(restoreFocusControl)
        && globalMenuButtonRef.current?.isConnected && globalMenuButtonRef.current.getClientRects().length
        && !globalMenuButtonRef.current.matches(':disabled')
        ? globalMenuButtonRef.current : null
      ;(controlTarget || runsMainRef.current)?.focus({ preventScroll: true })
    }
  }, [initialNavigationState, restoreFocusControl, restoreFocusRunId])

  const navigationResourcesSettled = runsState !== 'loading'
    && projectsState !== 'loading' && superState !== 'loading'
  useLayoutEffect(() => {
    if (!initialNavigationState || navigationRestoreStartedRef.current
        || !navigationResourcesSettled || !runListRef.current) return undefined
    navigationRestoreStartedRef.current = true
    const list = runListRef.current
    list.scrollTop = initialNavigation.scrollTop
    listScrollTopRef.current = list.scrollTop
    let frame = null
    let attempts = 0
    const finish = () => {
      const snapshot = captureNavigationStateRef.current()
      navigationCallbacksRef.current.onNavigationStateChange?.(snapshot)
      navigationCallbacksRef.current.onNavigationRestored?.(snapshot)
    }
    const restoreFocus = () => {
      const active = document.activeElement
      const visibleExternalFocus = active && active !== document.body
        && active !== document.documentElement && !runsMainRef.current?.contains(active)
        && active.getClientRects?.().length
      const visibleRunsControlFocus = active && active !== runsMainRef.current
        && runsMainRef.current?.contains(active) && active.getClientRects?.().length
      if (visibleExternalFocus || visibleRunsControlFocus) { finish(); return }
      const runTarget = restoreFocusRunId
        ? [...(runsMainRef.current?.querySelectorAll('[data-run-open-id]') || [])]
          .find(element => element.dataset.runOpenId === String(restoreFocusRunId)
            && element.getClientRects().length && element.getAttribute('aria-disabled') !== 'true'
            && !element.matches(':disabled'))
        : null
      const controlTarget = !restoreFocusRunId && returnsToGlobalMenu(restoreFocusControl)
        && globalMenuButtonRef.current?.isConnected && globalMenuButtonRef.current.getClientRects().length
        && !globalMenuButtonRef.current.matches(':disabled')
        ? globalMenuButtonRef.current : null
      const target = runTarget || controlTarget
      const controlExpected = !restoreFocusRunId && returnsToGlobalMenu(restoreFocusControl)
      if (!target && (restoreFocusRunId || controlExpected) && attempts < 20) {
        attempts += 1
        frame = requestAnimationFrame(restoreFocus)
        return
      }
      ;(target || runsMainRef.current)?.focus({ preventScroll: true })
      finish()
    }
    frame = requestAnimationFrame(restoreFocus)
    return () => { if (frame != null) cancelAnimationFrame(frame) }
  }, [initialNavigationState, initialNavigation.scrollTop, navigationResourcesSettled,
    restoreFocusControl, restoreFocusRunId])

  const activeView = savedViews.find(item => item.name === activeSavedView)
  const activeViewDirty = !!activeView
    && portfolioViewSignature(activeView) !== portfolioViewSignature(portfolioState)
  const applySavedView = name => {
    const saved = savedViews.find(item => item.name === name)
    if (!saved) { commitActiveSavedViewState(''); return }
    setSel(saved.project); setQuery(saved.query); setTaskFilter(saved.task)
    setTaskFilterExact(saved.task !== ALL || saved.taskExact === true)
    setStatusFilter(saved.status); setStFilter(saved.supertask)
    setSortKey(saved.sort); setSortDir(saved.direction); setView(saved.view)
    setCompareIds(new Set(saved.compare)); setCompareColumns(saved.columns)
    commitActiveSavedViewState(saved.name); setStaleSavedViewName(''); setViewMessage('')
  }
  const savePortfolioView = async (name, confirmation) => {
    let savedView = null
    await mutateStoredPortfolioViews(current => {
      const authoritative = current.find(item => item.name === name)
      const authoritativeActive = current.find(item => item.name === activeSavedView)
      const replaceRequested = confirmation?.replaceName === name
      const recreateRequested = confirmation?.recreateName === name
      const confirmedReplace = replaceRequested && authoritative
        && confirmation.expectedSignature === portfolioViewSignature(authoritative)
      const confirmedRecreate = recreateRequested && !authoritative
      const implicitUpdate = activeView?.name === name && authoritative
        && portfolioViewSignature(activeView) === portfolioViewSignature(authoritative)
      if (replaceRequested && !confirmedReplace) {
        commitSavedViewsState(current)
        if (!authoritative) setStaleSavedViewName(name)
        if (activeSavedView && (!authoritativeActive
            || portfolioViewSignature(activeView) !== portfolioViewSignature(authoritativeActive))) {
          commitActiveSavedViewState('')
        }
        throw portfolioViewError('viewConflict')
      }
      if (recreateRequested && !confirmedRecreate) {
        commitSavedViewsState(current)
        throw portfolioViewError('viewConflict')
      }
      if (activeView?.name === name && !authoritative && !confirmedReplace && !confirmedRecreate) {
        commitSavedViewsState(current); commitActiveSavedViewState('')
        setStaleSavedViewName(name)
        throw portfolioViewError('viewConflict')
      }
      const prepared = preparePortfolioViewSave(current, name, portfolioState, {
        replaceName: confirmedReplace || implicitUpdate ? name : '',
      })
      if (!prepared.ok) {
        commitSavedViewsState(current)
        if (activeSavedView && (!authoritativeActive
            || portfolioViewSignature(activeView) !== portfolioViewSignature(authoritativeActive))) {
          commitActiveSavedViewState('')
        }
        const flag = prepared.code === 'name' ? 'viewName'
          : prepared.code === 'state' ? 'viewState'
            : prepared.code === 'capacity' ? 'viewCapacity' : 'viewExists'
        throw portfolioViewError(flag)
      }
      savedView = prepared.view
      return prepared.views
    }, {
      onCommitted: next => {
        commitSavedViewsState(next); commitActiveSavedViewState(savedView.name)
        setStaleSavedViewName(''); setViewMessage('')
      },
      coordinator: portfolioCoordinatorRef.current,
      onSuperseded: reconcileSavedViewsState,
      supersededPreservesOutcome: authoritative => {
        const persisted = authoritative.find(item => item.name === savedView.name)
        return !!persisted
          && portfolioViewSignature(persisted) === portfolioViewSignature(savedView)
      },
    })
    return true
  }
  const closeViewModal = () => {
    setViewModal(false); focusSoon(saveViewButtonRef.current)
  }
  const deleteSavedView = async event => {
    if (!activeView) return
    const focusOwner = event?.currentTarget || deleteSavedViewRef.current
    const deletedName = activeView.name
    const expectedSignature = portfolioViewSignature(activeView)
    if (!confirm(`Delete saved view "${deletedName}"?`)) return
    try {
      await mutateStoredPortfolioViews(current => {
        const authoritative = current.find(item => item.name === deletedName)
        if (!authoritative || portfolioViewSignature(authoritative) !== expectedSignature) {
          commitSavedViewsState(current); commitActiveSavedViewState('')
          focusAfterTransientAction(focusOwner,
            () => [savedViewSelectRef.current, saveViewButtonRef.current])
          throw portfolioViewError('viewConflict')
        }
        return current.filter(item => item.name !== deletedName)
      }, {
        onCommitted: next => {
          commitSavedViewsState(next); commitActiveSavedViewState(''); setViewMessage('')
          focusAfterTransientAction(focusOwner,
            () => [savedViewSelectRef.current, saveViewButtonRef.current])
        },
        coordinator: portfolioCoordinatorRef.current,
        onSuperseded: reconcileSavedViewsState,
        supersededPreservesOutcome: authoritative =>
          !authoritative.some(item => item.name === deletedName),
      })
    } catch (error) {
      setViewMessage(error?.storage
        ? 'Browser storage is blocked or full; this saved view was not deleted.'
        : error?.viewBusy
          ? 'Saved views are being changed in another tab. Nothing was deleted; try again.'
          : error?.viewCoordination
            ? 'This page cannot safely coordinate saved views. Nothing was deleted; allow site storage, open LoopLab through localhost or HTTPS, or use a current browser.'
            : 'This saved view changed or was removed in another tab. Nothing was deleted; review the list and confirm again.')
    }
  }
  const savedViewConfirmation = name => {
    const existing = savedViews.find(item => item.name === name)
    if (existing && existing.name !== activeView?.name) return {
      replaceName: existing.name,
      expectedSignature: portfolioViewSignature(existing),
      confirm: 'Replace view',
      message: `“${existing.name}” already exists. Replace it with the current filters and layout?`,
    }
    if (name && !existing && staleSavedViewName === name) return {
      recreateName: name,
      confirm: 'Recreate view',
      message: `“${name}” was removed in another tab. Create it again with the current filters and layout?`,
    }
    return null
  }
  const openComparison = focusOwner => {
    compareEntryFocusRef.current = focusOwner
    setView('compare')
  }
  const clearComparison = focusOwner => {
    const returnRunId = compareRuns[0]?.run_id || [...compareIds][0]
    setCompareIds(new Set())
    focusAfterTransientAction(focusOwner,
      () => [compareCheckboxFor(returnRunId), filterInputRef.current, runsMainRef.current])
  }
  const toggleCompare = (runId, focusOwner = null) => {
    const next = new Set(compareIds)
    if (next.delete(runId)) {
      if (view === 'compare') {
        const index = compareRuns.findIndex(run => run.run_id === runId)
        const remaining = compareRuns.filter(run => run.run_id !== runId)
        if (remaining.length < 2) {
          setView('list')
          focusAfterTransientAction(focusOwner,
            () => [compareCheckboxFor(runId), filterInputRef.current, runsMainRef.current])
        } else {
          const neighborId = compareRuns[index + 1]?.run_id
            || compareRuns[index - 1]?.run_id || remaining[0]?.run_id
          focusAfterTransientAction(focusOwner,
            () => [compareRemoveFor(neighborId), compareHeadingRef.current,
              compareViewButtonRef.current, runsMainRef.current])
        }
      }
      setCompareIds(next); return
    }
    // No comparison-shaped cap here. Selecting is not comparing: an operator picking twenty runs —
    // to compare the first few, or to act on all of them — used to be refused at the ninth checkbox.
    // `SELECTION_MAX` exists only because the set round-trips through the saved view and the URL.
    if (next.size >= SELECTION_MAX) {
      setViewMessage(`Selection is limited to ${SELECTION_MAX} runs.`)
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
    const path = []; const seen = new Set(); let cur = projectById[sel]
    // Project files can be edited outside the UI. A malformed parent cycle must not freeze Runs.
    while (cur && !seen.has(cur.id)) {
      seen.add(cur.id); path.unshift(cur); cur = projectById[cur.parent_id]
    }
    return path
  }, [sel, projectById])

  // The scope a cross-run report would cover, by the CURRENT view: an explicit super-task / task filter
  // is the user's intent to scope by it; otherwise the open folder. null = nothing reportable (All runs,
  // no filter) → hide the Report button.
  const scope = useMemo(() => {
    if (stFilter !== ALL && stFilter !== UNASSIGNED) return { type: 'supertask', id: stFilter, label: (stName[stFilter] || stFilter) }
    if (taskFilterExact) return { type: 'task', id: taskFilter, label: 'task ' + taskFilter }
    if (projectScopeBlocked) return null
    if (sel !== ALL && sel !== UNASSIGNED) return { type: 'project', id: sel, label: (projName[sel] || sel) }
    return null
  }, [projectScopeBlocked, stFilter, taskFilter, taskFilterExact, sel, stName, projName])

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
    // Every open starts with the cascade OFF and the survey unread. A checkbox that remembered its
    // last answer would carry consent from one run to another.
    setDeleteCascade(false); setDeleteMemoryReport(null)
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
  // Returns a VERDICT — 'deleted' | 'pending' | 'blocked' | 'rejected' | 'unknown' — for the batch
  // runner, which has to decide whether to submit the next run. Every existing caller ignores it,
  // and must: for a single deletion the notice and the recovery record already say everything.
  const finishRunDeletionReceipt = async (intent, receipt, {
    fromDialog = false, shouldRestoreFocus = () => false, quiet = false,
  } = {}) => {
    if (receipt.status === 'pending') {
      persistDeletionPhase(intent, 'pending', receipt.phase)
      // 'blocked' is 'pending' that a retry cannot move — the server said `retryable: false`, which
      // it only does when nothing in its own process will ever touch what stands in the way. Polling
      // it is the operator watching a progress line for a state machine that has stopped, so the
      // batch takes the server's own sentence and stops on it instead.
      if (receipt.retryable === false) {
        if (!quiet) setDeletionNotice({ kind: 'error', text: receipt.message
          || 'This deletion cannot continue until its storage is resolved by hand.' })
        if (fromDialog) leaveDeleteDialogForRecovery(intent)
        return { outcome: 'blocked', reason: receipt.message
          || 'it cannot continue until its storage is resolved by hand' }
      }
      if (fromDialog) leaveDeleteDialogForRecovery(intent)
      return { outcome: 'pending', reason:
        'the server accepted it and is still working — it has a saved recovery' }
    }
    const listRead = await loadRuns()
    if (!listRead?.ok) {
      persistDeletionPhase(intent, 'pending', receipt.phase)
      const unread = receipt.status === 'succeeded'
        ? 'Deletion is confirmed, but the current run list could not be refreshed. The exact recovery remains locked.'
        : 'The deletion operation is resolved, but the current run list could not be refreshed.'
      if (!quiet) setDeletionNotice({ kind: 'error', text: unread })
      if (fromDialog) leaveDeleteDialogForRecovery(intent)
      return { outcome: 'unknown', reason: 'the refreshed run list could not be read' }
    }
    const exactTargetStillVisible = (listRead.value || []).some(run =>
      run.run_id === intent.runId
      && deletionGenerationOf(run) === intent.expectedGeneration
      && run.seq === intent.expectedSeq)
    if (receipt.status === 'succeeded' && exactTargetStillVisible) {
      persistDeletionPhase(intent, 'pending', receipt.phase)
      if (!quiet) setDeletionNotice({ kind: 'error', text:
        'Deletion is confirmed, but the deleted generation is still present in the refreshed list. Recovery remains locked.' })
      if (fromDialog) leaveDeleteDialogForRecovery(intent)
      return { outcome: 'unknown', reason:
        'the deleted generation is still present in the refreshed list' }
    }
    if (receipt.status === 'succeeded') {
      setCompareIds(current => {
        if (!current.has(intent.runId)) return current
        const next = new Set(current); next.delete(intent.runId); return next
      })
    }
    const restoreFocus = fromDialog || shouldRestoreFocus()
    const cleared = clearDeletionRecovery(intent)
    // When a cascade ran, its outcome IS the deletion's outcome as far as the operator is concerned
    // — and a partly-failed purge has to say so here, because the run's card is already gone and
    // this notice is the last surface that can carry the retry.
    const cascade = cascadeOutcome(receipt.memory, intent.runId)
    if (!quiet) setDeletionNotice(cleared
      ? (cascade || { kind: 'status', text: `Run “${intent.runId}” was permanently deleted.` })
      : { kind: 'error', text:
          'The deletion operation is resolved, but this tab could not clear its recovery record safely.' })
    if (fromDialog) {
      setDeleteDialog(null); setDeleteDialogBusy(false); setDeleteDialogError('')
      deleteDialogFocusRef.current = null
    }
    if (cleared) {
      if (restoreFocus) focusAfterDeletion(intent, fromDialog ? undefined : shouldRestoreFocus)
      else deletionFallbacksRef.current.delete(intent.operationId)
    } else if (restoreFocus) {
      focusDeletionRecovery(intent.runId, fromDialog ? undefined : shouldRestoreFocus)
    }
    return receipt.status === 'succeeded' && cleared
      ? { outcome: 'deleted', reason: '', memory: receipt.memory }
      : { outcome: 'unknown', reason: 'its recovery record could not be cleared safely' }
  }
  // Also returns the verdict, for the same reason `finishRunDeletionReceipt` does. `undefined` means
  // the request was never made (a duplicate operation id, or an abort) — distinct from 'unknown',
  // which means it WAS made and the outcome is not established.
  const runDeletionRequest = async (intent, {
    initialRequest = false, fromDialog = false, quiet = false,
  } = {}) => {
    if (!intent || deletionRequestRef.current.has(intent.operationId)) return undefined
    const focusContext = fromDialog ? 'dialog'
      : deletionFocusOwned(intent.runId) ? 'recovery' : ''
    const shouldRestoreFocus = () => focusContext === 'dialog'
      || (focusContext === 'recovery' && deletionFocusOwned(intent.runId, true))
    // GET observes a receipt but cannot roll a pending transaction forward. Every continuation uses
    // the exact persisted POST identity, which is idempotent and resumes the server state machine.
    const timed = deadlineRequest(signal => submitRunDeletion(
      intent.runId, intent.expectedGeneration, intent.expectedSeq,
      intent.operationId, { signal, deleteMemory: intent.deleteMemory === true }),
    RUN_DELETION_TIMEOUT_MS)
    const request = { controller: timed.controller, intent }
    deletionRequestRef.current.set(intent.operationId, request)
    setDeletionBusyFor(intent.runId, true)
    if (fromDialog) setDeleteDialogBusy(true)
    else if (focusContext === 'recovery') {
      focusDeletionRecovery(intent.runId, shouldRestoreFocus)
    }
    let verdict
    try {
      const rawReceipt = await timed.promise
      if (deletionRequestRef.current.get(intent.operationId) !== request) return undefined
      const receipt = exactRunDeletionReceipt(rawReceipt, intent)
      if (!receipt) {
        const protocol = new Error('Invalid deletion receipt')
        protocol.code = 'delete_protocol_error'
        throw protocol
      }
      verdict = await finishRunDeletionReceipt(
        intent, receipt, { fromDialog, shouldRestoreFocus, quiet })
    } catch (error) {
      if (deletionRequestRef.current.get(intent.operationId) !== request
          || error?.name === 'AbortError') return undefined
      const status = Number(error?.status)
      // Every continuation is the same idempotent POST. A definitive 4xx means that exact operation
      // was rejected; access failures remain unknown after a prior timeout because auth can stop the
      // retry before the server exposes an already accepted receipt.
      const authoritativeRejection = Number.isFinite(status)
        && status >= 400 && status < 500 && ![408, 425, 429].includes(status)
        && (initialRequest || ![401, 403].includes(status))
      const restoreFocus = shouldRestoreFocus()
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
          if (!quiet) setDeletionNotice({ kind: 'error', text: rejectionMessage })
          const listRead = await loadRuns()
          const followFocus = restoreFocus && shouldRestoreFocus()
          if (followFocus && ![404, 410].includes(status) && listRead?.ok) {
            focusAfterDeletion(intent, shouldRestoreFocus)
          } else {
            deletionFallbacksRef.current.delete(intent.operationId)
            if (followFocus) requestAnimationFrame(() => {
              if (shouldRestoreFocus()) runsMainRef.current?.focus?.({ preventScroll: true })
            })
          }
        }
        verdict = { outcome: 'rejected', reason: rejectionMessage }
      } else {
        persistDeletionPhase(intent, 'unknown')
        if (!quiet) setDeletionNotice({ kind: 'error', text:
          'Deletion outcome is not confirmed. Retry only the exact saved deletion before changing this run.' })
        if (fromDialog) leaveDeleteDialogForRecovery(intent)
        verdict = { outcome: 'unknown', reason:
          'the outcome is not confirmed — only the exact saved deletion may be retried' }
      }
    } finally {
      if (deletionRequestRef.current.get(intent.operationId) === request) {
        deletionRequestRef.current.delete(intent.operationId)
        setDeletionBusyFor(intent.runId, false)
      }
      if (fromDialog) setDeleteDialogBusy(false)
      deleteConfirmLockRef.current = false
    }
    // THE RETURN. It was missing, and the batch — the one caller that reads it — therefore saw
    // `undefined` for every run, including the ones the server had just deleted. `verdict?.outcome
    // === 'deleted'` was never true, so no run ever entered `state.done`, every run took the stop
    // branch, and `verdict?.reason || 'the deletion did not complete'` produced the notice the
    // operator acted on: "Nothing was deleted" about a batch whose receipt read `phase: succeeded`
    // and whose directory was gone. A destructive operation that under-reports what it did is worse
    // than one that fails loudly, so this line is the whole of that defect.
    return verdict
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
    // The cascade travels ON the persisted intent, so a recovery retry does what the operator
    // agreed to in this dialog rather than what a later retry happens to send.
    const intent = createRunDeletionIntent(
      deleteDialog.runId, deleteDialog.expectedGeneration, deleteDialog.expectedSeq, operationId,
      undefined, undefined, undefined, deleteCascade === true)
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
  // THE BATCH. A queue of the existing one-run transaction, drained strictly in order.
  //
  // Sequential is not a simplification: each deletion's receipt is validated against a REFRESHED run
  // list, and two deletions in flight would race that refresh — one's confirmation read observing
  // the other's half-applied state. And the fence for run N+1 must be read AFTER run N landed, which
  // is why the plan is recomputed from `runsRef` on every iteration rather than captured up front.
  const runBulkDeletion = async () => {
    const dialog = bulkDeleteDialog
    if (!dialog || bulkDeleteState?.running) return
    const cascade = dialog.cascade === true
    const state = { running: true, total: dialog.plan.ready.length, done: [], current: '',
      blocked: dialog.plan.blocked, stoppedAt: null, memoryFailures: [] }
    setBulkDeleteState({ ...state })
    for (const target of dialog.plan.ready) {
      // Re-resolve against the CURRENT list: the row we planned from is one deletion older.
      const fresh = (runsRef.current || []).find(run => run.run_id === target.runId)
      if (!fresh) {
        state.stoppedAt = { runId: target.runId, outcome: 'not-submitted',
          reason: 'it left the run list before its turn' }
        break
      }
      const generation = deletionGenerationOf(fresh)
      if (!RUN_GENERATION_RE.test(generation)
          || !Number.isSafeInteger(fresh.seq) || fresh.seq < -1) {
        state.stoppedAt = { runId: target.runId, outcome: 'not-submitted',
          reason: 'its exact deletion identity is unavailable' }
        break
      }
      state.current = fresh.label || target.runId
      setBulkDeleteState({ ...state })
      const intent = createRunDeletionIntent(
        target.runId, generation, fresh.seq, createIdempotencyKey().toLowerCase(),
        undefined, undefined, undefined, cascade)
      if (!intent || !saveRunDeletionIntent(intent)) {
        // Same refusal as the single dialog: without a durable recovery record this tab cannot
        // identify the operation it is about to start, so it does not start it.
        state.stoppedAt = { runId: target.runId, outcome: 'not-submitted', reason:
          'this tab could not preserve an exact recovery record' }
        break
      }
      updateDeletionRecovery({ runId: intent.runId, kind: 'active', intent })
      let verdict = await runDeletionRequest(intent, { initialRequest: true, quiet: true })
      // A `pending` verdict is the server ACCEPTING the deletion (202) and still working on it —
      // not a refusal. Stopping the batch on it degraded bulk delete to one run per press, with an
      // error-toned notice about nothing, on any deployment where deletions routinely go async.
      // The single-run flow already waits; the batch does the same, bounded. The POST carries the
      // persisted operation identity and is idempotent, so re-issuing it RESUMES the same server
      // state machine rather than starting a second deletion.
      for (let attempt = 0; verdict?.outcome === 'pending'
            && attempt < BULK_DELETION_PENDING_POLLS; attempt++) {
        await new Promise(resolve => setTimeout(resolve, RUN_DELETION_POLL_MS))
        verdict = await runDeletionRequest(intent, { quiet: true }) || verdict
      }
      if (verdict?.outcome === 'deleted') {
        state.done.push(target.runId)
        // A cascade that only partly ran is the ONE thing `quiet` must not swallow. The run is gone,
        // its card has left the list, and `cascadeOutcome`'s retry handle is the only way back to
        // the half-purged store — a batch that dropped it would leave the operator with no notice,
        // no affordance and no way to learn it happened.
        if (verdict.memory && verdict.memory.ok === false) {
          state.memoryFailures.push({ runId: target.runId, memory: verdict.memory })
        }
        setBulkDeleteState({ ...state })
        continue
      }
      // Still pending after the budget above is a real stop — but it is an UNFINISHED deletion with
      // a saved recovery record, not a rejected one, and the notice has to say which. The OUTCOME
      // travels with the stop for the same reason: `undefined`/'unknown' means this tab never
      // established what happened to THIS run, so the notice may not claim it still exists.
      state.stoppedAt = { runId: target.runId, outcome: verdict?.outcome || 'unknown',
        reason: verdict?.outcome === 'pending'
          ? 'the server is still working on it; its recovery record is saved and can be resumed'
          : (verdict?.reason || 'the deletion did not complete') }
      break
    }
    // STOP AT THE FIRST FAILURE, deliberately. Whatever refused one deletion — an active engine, a
    // stale fence, an unavailable lock — is far more likely to be a property of the moment than of
    // that one run, and grinding through nineteen more refusals would bury the reason under
    // nineteen identical notices.
    state.running = false
    state.current = ''
    setBulkDeleteState({ ...state })
    setDeletionNotice(bulkOutcomeNotice(state))
    // …and the batch drops the plan it has now spent, so a second press re-reads the CURRENT list
    // instead of walking runs it already deleted (which reported "Nothing was deleted" about a
    // batch that deleted eight).
    setBulkDeleteDialog(current => current
      ? { ...current, plan: bulkDeletionPlan([...compareIds].filter(id => !state.done.includes(id)),
          runsRef.current || [], deletionRecoveriesRef.current || deletionRecoveries) }
      : current)
    setCompareIds(current => {
      const next = new Set(current)
      for (const runId of state.done) next.delete(runId)
      return next
    })
    if (!state.stoppedAt) closeBulkDelete()
  }
  const closeBulkDelete = () => {
    if (bulkDeleteState?.running) return
    setBulkDeleteDialog(null)
    requestAnimationFrame(() => runsMainRef.current?.focus?.({ preventScroll: true }))
  }
  const openBulkDelete = () => {
    setBulkDeleteState(null)
    setBulkDeleteDialog({
      plan: bulkDeletionPlan([...compareIds], runs || [], deletionRecoveries),
      cascade: false,
    })
  }
  const retryMemoryPurge = async (runId, identity) => {
    if (!runId || memoryRetryBusy) return
    setMemoryRetryBusy(true)
    const request = deadlineRequest(
      signal => purgeRunMemory(runId, identity, { signal }), RUN_DELETION_TIMEOUT_MS)
    try {
      const memory = await request.promise
      // Reuse the one outcome vocabulary. A second phrasing here would let the retry report success
      // in words the first attempt never used, for the same result.
      setDeletionNotice(cascadeOutcome(memory, runId)
        || { kind: 'status', text: 'The memory purge finished.' })
    } catch {
      // Carry the identity forward: the run is gone, so this notice is the ONLY place it still
      // exists, and a retry that dropped it could not succeed on the next press either.
      setDeletionNotice({ kind: 'error', retryRunId: runId, retryIdentity: identity, text:
        'The memory purge did not finish. Nothing further was removed; this can be retried.' })
    } finally {
      if (deletionMountedRef.current) setMemoryRetryBusy(false)
    }
  }
  const pendingDeletionRecoveries = [...deletionRecoveries.values()]
    .filter(recovery => recovery.kind === 'active' && recovery.intent.phase !== 'unknown'
      && recovery.intent.serverPhase !== 'quarantine_ambiguous'
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
      <div className="topbar home-head">
        {/* The mark is the menu. It was a wordmark here and a second button carrying the same word
            at the far RIGHT of this bar, among the theme/density toggles — two spellings of one
            name, and the menu filed with the display switches rather than with what it opens.
            The claim ledger and Settings used to be two loose buttons in that same corner, which is why
            cross-run memory, authored knowledge and the host's GPUs once hid in a RUN's menu. */}
        <GlobalMenu current="list" disabled={navigationBusy}
          buttonRef={globalMenuButtonRef} onNavigate={openGlobal} />
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
        {/* Named, because `Lineage` is now the label of TWO different surfaces — this one and the
            run workspace's DAG. The workspace toggle is a `role="toolbar" aria-label="Run workspace
            controls"`, so a screen reader announces its scope; this group announced a bare
            "Lineage, button" with nothing to tell the two apart. */}
        <div className="seg" role="group" aria-label="Run list views">
          {/* LINEAGE, not "Map", and CONCEPTS beside it — because the two answer different questions
              and calling one of them "the map" is why the second one was missing for so long. This
              view draws which run descends from which, inside which project: ancestry, i.e. lineage.
              The concept tree is the other map, of what the lab has STUDIED, and it is the button
              after this one. Same rename as the run-level view took today, where `Search` became
              `Lineage` while its internal key stayed `dag`.

              The KEY stays `map`. It is persisted in saved views, in the restored navigation state
              and in shared links (`normalizeNavigation` above admits it, `captureNavigationState`
              writes it), so renaming the key would silently retire every saved scope and every
              bookmark that names this view. A label is not a reason to break those. Whoever comes
              next wanting to "finish" the rename: the label is the whole rename. */}
          <button aria-pressed={view === 'list'} className={view === 'list' ? 'on' : ''} onClick={() => setView('list')}><OpIcon name="list" className="t-ic" /> List</button>
          <button aria-pressed={view === 'map'} className={view === 'map' ? 'on' : ''} onClick={() => setView('map')}><OpIcon name="map" className="t-ic" /> Lineage</button>
          <button aria-pressed={view === 'concepts'} className={view === 'concepts' ? 'on' : ''}
            disabled={projectScopeBlocked}
            title={projectScopeBlocked
              ? 'Restore the saved project or use All runs first'
              : 'Concepts across the runs this list is showing'}
            onClick={() => setView('concepts')}><OpIcon name="gitbranch" className="t-ic" /> Concepts</button>
          <button ref={compareViewButtonRef} aria-pressed={view === 'compare'} className={view === 'compare' ? 'on' : ''}
            disabled={projectScopeBlocked || compareRuns.length < 2}
            title={projectScopeBlocked ? 'Restore the saved project or use All runs first'
              : compareRuns.length < 2 ? 'Select at least two runs from List' : 'Compare selected runs'}
            onClick={event => openComparison(event.currentTarget)}>Compare · {compareRuns.length}</button>
        </div>
        {/* Slash remains a power-user shortcut; New run above is the first-use primary action. */}
        <span className="muted home-new-hint" style={{ fontSize: 11 }}>type <code className="cmd-hint">/new</code> in the bar below to start a run</span>
        <span className="spacer" style={{ flex: 1 }} />
        <div className="home-actions">
          <DensityToggle />
          <ThemeSwitcher />
          <EnergyToggle />
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
        <button type="button" className="btn sm primary" data-run-open-id={item.runId}
          onClick={() => openRun(item.runId)}>
          Open recovery
        </button>
      </div>)}
      {deletionNotice && <div className={`notice run-deletion-notice ${deletionNotice.kind}`}
        role={deletionNotice.kind === 'error' ? 'alert' : 'status'}>
        <span>{deletionNotice.text}</span>
        {deletionNotice.retryRunId && <button type="button" className="btn sm"
          disabled={memoryRetryBusy}
          onClick={() => retryMemoryPurge(deletionNotice.retryRunId, deletionNotice.retryIdentity)}>
          {memoryRetryBusy ? 'Finishing…' : 'Finish memory purge'}</button>}
        <button type="button" className="btn sm" onClick={() => setDeletionNotice(null)}>Dismiss</button>
      </div>}
      {missingDeletionRecoveries.map(recovery => {
        const active = recovery.kind === 'active'
        const repairRequired = active && recovery.intent.serverPhase === 'quarantine_ambiguous'
        const urgent = !active || recovery.intent.phase === 'unknown' || repairRequired
          || recovery.storageUnavailable
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
              : recovery.intent.phase === 'unknown' ? 'Retry exact deletion'
                : repairRequired ? 'Check after repair' : 'Check exact deletion'}
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

        <div ref={runListRef} className={'runlist' + (view === 'map' ? ' map-list-shell' : '')}
          onScroll={event => {
            listScrollTopRef.current = event.currentTarget.scrollTop
            publishNavigationState({ persist: false })
          }}>
          <div className="crumbs">
            <button type="button" className="crumb" disabled={navigationBusy} onClick={() => chooseProject(ALL)}>All runs</button>
            {breadcrumb.map(p => <React.Fragment key={p.id}><span className="sep">/</span>
              <button type="button" className="crumb" disabled={navigationBusy} onClick={() => chooseProject(p.id)}>{p.name}</button></React.Fragment>)}
            {projectScopePending && <><span className="sep">/</span>
              <span className="crumb" title={`Saved project ${sel}`}>Saved project unavailable</span></>}
            {projectScopeMissing && <><span className="sep">/</span>
              <span className="crumb" title={`Saved project ${sel}`}>Saved project removed</span></>}
            {sel === UNASSIGNED && <><span className="sep">/</span><span className="crumb">Unassigned</span></>}
            <span style={{ flex: 1 }} />
            {scope && <div className="view-toggle crumb-report">
              <button className={'vt report' + (showReport ? ' on' : '')} disabled={navigationBusy} title={`cross-run report for ${scope.label}`}
                onClick={() => setShowReport(true)}><OpIcon name="doc" size={12} /> Report<span className="vt-scope"> · {scope.label}</span></button>
            </div>}
          </div>
          {runs && <div className="portfolio-viewbar">
            <label>Saved view
              <select ref={savedViewSelectRef} className="sel" aria-label="Saved portfolio view" value={activeSavedView}
                onChange={event => event.target.value
                  ? applySavedView(event.target.value) : commitActiveSavedViewState('')}>
                <option value="">Custom / unsaved</option>
                {savedViews.map(saved => <option key={saved.name} value={saved.name}>
                  {saved.name}{saved.name === activeSavedView && activeViewDirty ? ' · modified' : ''}
                </option>)}
              </select>
            </label>
            <button ref={saveViewButtonRef} className="btn sm" onClick={() => setViewModal(true)}>
              Save current view
            </button>
            <button ref={deleteSavedViewRef} className="btn sm ghost" disabled={!activeView}
              onClick={deleteSavedView}>Delete</button>
            {activeViewDirty && <span className="muted" role="status">Modified</span>}
          </div>}
          {viewMessage && <div className="notice resource-warning portfolio-message" role="alert">
            {viewMessage} <button className="btn xs" onClick={() => setViewMessage('')}>Dismiss</button>
          </div>}
          {runs && !projectScopeBlocked && view !== 'compare' && <div className="runbar">
            <OpIcon name="search" className="t-ic" />
            <input ref={filterInputRef} className="text runbar-q" aria-label="Filter runs" placeholder="filter runs…" value={query}
                   maxLength={MAX_PORTFOLIO_QUERY_LENGTH} onChange={e => setQuery(e.target.value)} />
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
            <select ref={taskFilterRef} className="sel" aria-label="Filter by task"
              aria-describedby={taskFilterUnavailable ? 'run-task-filter-warning' : undefined}
              value={taskFilterExact ? taskSelectValue(taskFilter) : TASK_SELECT_ALL}
              onChange={event => {
                const next = readTaskSelectValue(event.target.value)
                setTaskFilter(next.id); setTaskFilterExact(next.exact)
              }}>
              <option value={TASK_SELECT_ALL}>all tasks</option>
              {taskFilterUnavailable
                && <option value={taskSelectValue(taskFilter)}>{taskFilterOptionLabel}</option>}
              {tasks.map(t => <option key={t} value={taskSelectValue(t)}>{t}</option>)}
            </select>
            <div className="runbar-super-control">
              <select ref={superFilterRef} className="sel" aria-label="Filter by super-task"
                aria-describedby={superFilterUnavailable ? 'run-super-filter-warning' : undefined}
                value={stFilter} onChange={e => setStFilter(e.target.value)}>
                <option value={ALL}>all super-tasks</option>
                <option value={UNASSIGNED}>— no super-task —</option>
                {superFilterUnavailable
                  && <option value={stFilter}>{superFilterOptionLabel}</option>}
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
          {!projectScopeBlocked && compareIds.size > 0 && view !== 'compare' && <div className="compare-selection" role="status">
            <b>{compareRuns.length} selected</b>
            <span className="muted">{selectionNotice(comparisonScope(compareRuns))}</span>
            <button className="btn sm primary" disabled={compareRuns.length < 2}
              onClick={event => openComparison(event.currentTarget)}>
              Compare{comparisonScope(compareRuns).omitted
                ? ` first ${comparisonScope(compareRuns).shown.length}` : ' runs'}</button>
            <button className="btn sm danger" disabled={listBusy || !!bulkDeleteDialog}
              onClick={openBulkDelete}>Delete {compareRuns.length}…</button>
            <button className="btn sm ghost"
              onClick={event => clearComparison(event.currentTarget)}>Clear</button>
          </div>}
          <ResourceNotice state={runsState} label="Runs" retry={loadRuns} />
          <ResourceNotice state={projectsState} label="Projects" retry={loadProjects} />
          {!stModal && <ResourceNotice state={superState} label="Super-tasks" retry={loadSupers} />}
          {taskFilterUnavailable && <UnavailableFilterNotice id="run-task-filter-warning"
            state={runsState} actionLabel="Use all tasks" disabled={navigationBusy}
            controlRef={taskFilterRef} fallbackRef={filterInputRef} mainRef={runsMainRef}
            onAction={focusOwner => useAllForUnavailableFilter(() => {
              setTaskFilter(ALL); setTaskFilterExact(false)
            }, taskFilterRef, focusOwner)}>
            {runsState === 'ready'
              ? <><b>Selected task “{taskFilter}” has no current runs.</b> Its exact filter is still active.</>
              : runsState === 'stale'
                ? <><b>Selected task “{taskFilter}” is absent from the last loaded runs.</b> Its exact filter stays active until Runs refresh succeeds.</>
                : <><b>Selected task “{taskFilter}” cannot be verified yet.</b> Its exact filter is still active.</>}
          </UnavailableFilterNotice>}
          {superFilterUnavailable && <UnavailableFilterNotice id="run-super-filter-warning"
            state={superState} actionLabel="Use all super-tasks" disabled={navigationBusy}
            controlRef={superFilterRef} fallbackRef={filterInputRef} mainRef={runsMainRef}
            onAction={focusOwner => useAllForUnavailableFilter(
              () => setStFilter(ALL), superFilterRef, focusOwner)}>
            {superState === 'ready'
              ? <><b>Selected super-task “{stFilter}” no longer exists.</b> Its exact filter is still active.</>
              : superState === 'stale'
                ? <><b>Selected super-task “{stFilter}” is absent from the last loaded registry.</b> Its exact filter stays active until Super-tasks refresh succeeds.</>
                : superState === 'loading'
                  ? <><b>Checking selected super-task “{stFilter}”.</b> Its exact filter is still active.</>
                  : <><b>Selected super-task “{stFilter}” cannot be verified.</b> The registry is unavailable; its exact filter is still active.</>}
          </UnavailableFilterNotice>}
          {projectScopePending && <div className="notice resource-warning" role="status">
            <span><b>Saved project cannot be verified yet.</b> Runs stay hidden to preserve this scope.</span>
            <button type="button" className="btn sm" disabled={navigationBusy}
              onClick={() => chooseProject(ALL)}>Use All runs</button>
          </div>}
          {projectScopeMissing && <div className="notice resource-warning" role="alert">
            <span><b>Saved project no longer exists.</b> Switching to All runs.</span>
          </div>}
          {(!compactNav || !projectsOpen) && mutationNotice}
          {!projectScopeBlocked && ['ready', 'stale'].includes(runsState) && runs && !scoped.length
            && <div className="notice resource-empty">
            {runsState === 'stale' ? 'No runs in the last loaded data here.' : 'No runs here.'}
            {sel === ALL
              ? <button className="btn sm primary" disabled={navigationBusy}
                  onClick={() => window.dispatchEvent(new CustomEvent('ll:new-run', { cancelable: true }))}>
                  Start a new run
                </button>
              : <span>Drag a run onto this project, or use its <b>Move</b> menu.</span>}</div>}
          {runs && !!scoped.length && !visible.length
            && !taskFilterUnavailable && !superFilterUnavailable
            && <div className="notice" role="status">
            {runsState === 'stale' ? 'No runs in the last loaded data match the filters.' : 'No runs match the filters.'}
            {hasActiveFilters && <button type="button" className="btn sm" disabled={navigationBusy}
              onClick={event => clearFilters(event.currentTarget)}>Clear filters</button>}
          </div>}
          {view === 'map' && (!['ready', 'stale'].includes(projectsState) || projectScopeBlocked) && runs
            && <div className="notice resource-warning" role="status">
              <span>Lineage needs the project list before it can place runs reliably.</span>
              <button type="button" className="btn sm" onClick={() => setView('list')}>Show List</button>
            </div>}
          {view === 'map' && ['ready', 'stale'].includes(projectsState) && !projectScopeBlocked
            && runs && mapRuns.length > 0 && <div className="map-stage">
            {/* `label` is rendered verbatim by LazyBoundary — "Loading run lineage…" and
                "run lineage could not be opened." — so it is operator-visible text and follows the
                LABEL, not the `map` route key beside it. */}
            <LazyBoundary label="run lineage" resetKey={`map:${sel}`}>
              <MapView onOpen={id => { if (!navigationBusy) openRun(id) }} runs={mapRuns} projects={proj.projects}
                collapsed={mapCollapsed} onToggle={toggleMapCluster}
                initialViewport={mapViewport?.signature === mapViewportSignature
                  ? { x: mapViewport.x, y: mapViewport.y, zoom: mapViewport.zoom } : null}
                onViewportChange={next => setMapViewport(current => current
                  && current.x === next.x && current.y === next.y && current.zoom === next.zoom
                  && current.signature === mapViewportSignature
                  ? current : { ...next, signature: mapViewportSignature })}
                scopeLabel={scope?.label || (sel === ALL ? 'All runs' : sel === UNASSIGNED ? 'Unassigned' : (projName[sel] || sel))} />
            </LazyBoundary>
          </div>}
          {/* Concepts folds the SAME `mapRuns` array the Lineage view draws and the List renders —
              no fetch of its own, so switching representation cannot silently change the population.
              It is also why this costs nothing on the 2.5s poll: the fold is pure JS over rows
              already in memory (measured 3.6ms over the real 46-run corpus), not a cross-run
              request on a timer. The scope-blocked guard mirrors Lineage's: while a saved project id
              cannot be verified the list withholds actionable results, and a concept tree built from
              a silently-empty scope would read as "this lab has studied nothing". */}
          {view === 'concepts' && projectScopeBlocked && runs
            && <div className="notice resource-warning" role="status">
              <span>Concepts needs a verified project scope before it can tell you which runs it covers.</span>
              <button type="button" className="btn sm" onClick={() => setView('list')}>Show List</button>
            </div>}
          {view === 'concepts' && !projectScopeBlocked && runs
            && <LazyBoundary label="concept tree" resetKey={`concepts:${sel}`}>
              <PortfolioConcepts runs={mapRuns} selectedRuns={compareRuns}
                scopeLabel={scope?.label || (sel === ALL ? 'All runs'
                  : sel === UNASSIGNED ? 'Unassigned' : (projName[sel] || sel))}
                onOpenRun={id => { if (!navigationBusy) openRun(id) }} />
            </LazyBoundary>}
          {/* The comparison — and ONLY the comparison — is bounded. It draws the first COMPARE_MAX of
              the selection and says how many it left out; the rest stay selected for whatever else
              the operator picked them for. */}
          {view === 'compare' && !projectScopeBlocked && compareRuns.length > 1 && <LazyBoundary label="run comparison"
            resetKey={comparisonScope(compareRuns).shown.map(run => run.run_id).join(':')}>
            {comparisonScope(compareRuns).omitted > 0 && <div className="notice compact" role="status">
              Comparing the first {comparisonScope(compareRuns).shown.length} of {compareRuns.length}
              {' '}selected runs. The other {comparisonScope(compareRuns).omitted} stay selected.
            </div>}
            <RunCompare runs={comparisonScope(compareRuns).shown} columns={compareColumns}
              names={{ projects: projName, supertasks: stName }}
              onColumns={setCompareColumns} onRemove={toggleCompare}
              headingRef={setCompareHeadingRef}
              onFocusCapture={event => { compareSurfaceFocusRef.current = event.target }}
              onOpen={(id, href) => openRun(id, href)} />
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
                  data-compare-run-id={r.run_id}
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
              <a className="run-card-main" data-run-open-id={r.run_id}
                   href={`#/run/${encodeURIComponent(r.run_id)}`}
                   draggable={false}
                   aria-disabled={navigationBusy || undefined}
                   onClick={event => {
                     if (navigationBusy) { event.preventDefault(); return }
                     followClientRoute(event, () => openRun(r.run_id))
                   }}
                   aria-label={`Open run ${r.label || r.run_id}`}>
                <div><b>{r.label || r.run_id}</b> <span className="muted">· {r.label ? r.run_id + ' · ' : ''}{r.task_id}</span>
                  {startOverLocked && <span className="pill warn" style={{ marginLeft: 6 }}>Start over recovery</span>}
                  {r.project_id && projName[r.project_id] && <span className="pill" style={{ marginLeft: 6 }}><OpIcon name="folder" className="t-ic" /> {projName[r.project_id]}</span>}
                  {r.supertask_id && stName[r.supertask_id] && <span className="pill st-pill" style={{ marginLeft: 6 }}><OpIcon name="target" className="t-ic" /> {stName[r.supertask_id]}</span>}</div>
                <div className="goal">{r.goal}</div>
              </a>
              <div className="run-card-metrics" style={{ textAlign: 'right' }}>
                {/* The receipt goes ABOVE the numbers it qualifies, not below them: `best` and
                    `nodes` are folded from the readable prefix, and on a truncated log they are a
                    different, entirely plausible run (measured: `nodes: 2, best 0.8077` for a run
                    whose own budget row says 81 nodes). `role="status"` so a screen reader reaches
                    it in the same order a sighted reader does. */}
                {sourceIncomplete(r) && <div className="pill warn" role="status"
                  title={sourceIntegrityNotice(r)}>incomplete record</div>}
                <div>best <b>{fmt(r.best_confirmed ?? r.best_metric)}</b></div>
                {/* WHAT KIND OF NUMBER that is — touching the value it qualifies, above the
                    `nodes · direction` line rather than at the end of the card, because an operator
                    scanning this column is deciding which configuration to reuse and a caveat they
                    reach after the number is one they read after acting on it. (The integrity
                    receipt sits ABOVE the value for the stronger version of the same rule: it
                    qualifies every field on the card, not just this one.) Both rungs that produce a
                    caveat are UNDERIVABLE in the browser — the row carries no violations, no
                    provenance and no node — so this is the server's word, printed, not re-derived.
                    `role="status"` matches the receipt so a screen reader reaches both in order. */}
                {bestMetricCaveats(r).map(slug => (
                  <div className="pill warn" role="status" key={slug}
                    title={bestMetricCaveatNotice({ best_metric_caveats: [slug] })}>
                    {bestMetricCaveatLabel(slug)}</div>))}
                <div className="muted">{r.nodes} nodes · {r.direction}</div>
                {/* A NUMBER guard, not a truth test: `mtime` is epoch seconds, so the falsy value is
                    a real timestamp (1970-01-01T00:00:00Z) and `0 && <div/>` renders a bare `0` into
                    the run card instead of rendering nothing. Unreachable from a healthy run today,
                    but this is the exact shape that put 120 stray zeros on the Card board. */}
                {typeof r.mtime === 'number' && <div className="muted run-when"
                  title={`started ${fmtDate(r.created)} · updated ${fmtDate(r.mtime)}`}>
                  {fmtAgo(r.mtime)}</div>}
              </div>
              <div className="run-actions">
                <button className="ic dots" disabled={navigationBusy} aria-label={`Actions for run ${r.label || r.run_id}`}
                        aria-haspopup="menu" aria-expanded={!!openMenuRun}
                        onClick={e => {
                          e.stopPropagation()
                          if (openMenuRun) closeRunMenu(false)
                          else {
                            runMenuTriggerRef.current = e.currentTarget
                            setRunMenu({ ...r })
                          }
                        }}>⋮</button>
                {openMenuRun && <RunMenu r={openMenuRun} projects={proj.projects} supertasks={superdata.supertasks}
                  anchor={runMenuTriggerRef.current} positionKey={displayedRunOrder}
                  onOpen={openRun} onMove={moveRun} onSetSuper={assignToSuper} onManageSupers={() => openSuperTasks(runMenuTriggerRef.current)}
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
        onClose={() => closeDeleteDialog(true)} onConfirm={confirmRunDeletion}
        memoryReport={deleteMemoryReport} cascade={deleteCascade}
        onCascadeToggle={setDeleteCascade} />}
      {bulkDeleteDialog && <RunBulkDeleteDialog dialog={bulkDeleteDialog} state={bulkDeleteState}
        onClose={closeBulkDelete} onConfirm={runBulkDeletion}
        onCascadeToggle={value => setBulkDeleteDialog(
          current => current ? { ...current, cascade: value === true } : current)} />}

      {projModal && <PromptModal
        title={projModal.parent_id ? 'New sub-project' : 'New project'}
        label={projModal.parent_id ? 'Sub-project name' : 'Project name'}
        description={projModal.parent_id
          ? `Inside “${projName[projModal.parent_id]}”.` : 'Group runs into a project folder.'}
        placeholder="e.g. baseline sweep" confirm="Create"
        onSubmit={submitProject} onReconcile={refresh} onClose={closeProjectModal} />}

      {runRename && <PromptModal
        title="Rename run" label={`Display name for ${runRename.run_id} (clear it to fall back to the id).`}
        placeholder={runRename.run_id} initial={runRename.label || ''} confirm="Save" allowEmpty
        blocked={runRenameBlocked}
        blockedMessage="This run changed while Rename was open. Cancel and reopen Rename from the refreshed card."
        onSubmit={submitRunRename} onReconcile={loadRuns} onClose={closeRunRename} />}

      {viewModal && <PromptModal title="Save portfolio view"
        label="View name" description="Saves the current scope, filter, sort, layout, and comparison selection. Use an existing name to review and replace that saved view."
        placeholder="e.g. active baselines" initial={activeView?.name || ''} confirm="Save view"
        maxLength={MAX_PORTFOLIO_VIEW_NAME_LENGTH}
        confirmationForValue={savedViewConfirmation}
        onSubmit={savePortfolioView} onClose={closeViewModal} />}

      {stModal && <SuperTaskModal supertasks={superdata.supertasks} state={superState} onRetry={loadSupers}
        onCreate={createSuper} onRename={renameSuper} onDelete={removeSuper}
        onReconcile={reconcileAll} onClose={closeSuperTasks} />}

      {showReport && scope && <LazyBoundary label="scope report" mode="overlay" resetKey={scope.label}
        onClose={() => setShowReport(false)}>
        <ScopeReport scope={scope}
          onOpen={(id) => { setShowReport(false); openRun(id) }} onClose={() => setShowReport(false)} />
      </LazyBoundary>}
    </main>
  )
}
